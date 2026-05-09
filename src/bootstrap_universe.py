"""
Manual-seed bootstrap.

Reads data/manual_universe_seed.csv (columns: segment, manufacturer_part_number)
and turns it into a properly-formatted data/universe.csv with day-0 reference
values populated from DigiKey's API.

Workflow:
  1. You manually pick part numbers from DigiKey's website (filter by Vishay,
     sort by stock, copy manufacturer part numbers).
  2. Paste them into manual_universe_seed.csv with the segment label.
  3. Run this script. It will:
       - For each part number, call DigiKey's keyword_search to find the
         canonical product and its current stock/price/lead-time.
       - Pick the most-stocked package variation as the tracked DigiKey part.
       - Write universe.csv with all reference values frozen at runtime.
  4. From here, the daily-fetch + index pipeline works as designed.

Re-running bootstrap reuses cached entries unless --refresh is passed.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from digikey_client import DigiKeyClient, load_client_from_env

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("bootstrap_universe")


def lookup_part(client: DigiKeyClient, mpn: str) -> dict | None:
    """
    Resolve a manufacturer part number to a DigiKey product. Uses keyword
    search, takes the first result. Returns the parsed observation dict, or
    None if no match.
    """
    mpn = mpn.strip()
    if not mpn:
        return None
    try:
        result = client.keyword_search(
            keywords=mpn,
            limit=5,
            in_stock_only=False,  # capture even out-of-stock parts at day-0
        )
    except Exception as e:
        log.warning("Search failed for %r: %s", mpn, e)
        return None

    products = result.get("Products") or []
    if not products:
        log.warning("No DigiKey match for manufacturer part number %r", mpn)
        return None

    # Prefer products whose manufacturer name contains "Vishay" (a search like
    # "1N4007" returns matches across many manufacturers).
    vishay_products = [
        p for p in products
        if "vishay" in (((p.get("Manufacturer") or {}).get("Name")) or "").lower()
    ]
    chosen = (vishay_products or products)[0]

    # Also prefer products where mpn matches exactly (defensive)
    exact = [
        p for p in (vishay_products or products)
        if (p.get("ManufacturerProductNumber") or "").upper() == mpn.upper()
    ]
    if exact:
        chosen = exact[0]

    return chosen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=Path, default=Path("data/manual_universe_seed.csv"),
                    help="Input CSV with columns: segment, manufacturer_part_number")
    ap.add_argument("--output", type=Path, default=Path("data/universe.csv"))
    ap.add_argument("--refresh", action="store_true",
                    help="Re-fetch all parts even if already in universe.csv")
    args = ap.parse_args()

    if not args.seed.exists():
        log.error("Seed file not found: %s", args.seed)
        sys.exit(1)

    # Load existing universe if present (so re-running is incremental)
    existing: dict[tuple[str, str], dict] = {}
    if args.output.exists() and not args.refresh:
        with args.output.open() as f:
            for row in csv.DictReader(f):
                key = (row["segment"], row["manufacturer_part_number"].upper())
                existing[key] = row
        log.info("Loaded %d existing rows from %s", len(existing), args.output)

    # Read seed
    with args.seed.open() as f:
        seed_rows = [
            r for r in csv.DictReader(f)
            if (r.get("manufacturer_part_number") or "").strip()
        ]
    log.info("Seed file has %d non-empty rows", len(seed_rows))

    if not seed_rows:
        log.error("Seed file has no part numbers. Add some and re-run.")
        sys.exit(1)

    client = load_client_from_env()
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")

    out_rows: list[dict] = []
    n_ok = 0
    n_skip = 0
    n_fail = 0
    for i, seed in enumerate(seed_rows, 1):
        segment = (seed.get("segment") or "").strip()
        mpn = (seed.get("manufacturer_part_number") or "").strip()
        if not segment or not mpn:
            continue

        key = (segment, mpn.upper())
        if key in existing:
            out_rows.append(existing[key])
            n_skip += 1
            continue

        log.info("[%d/%d] %s | %s — looking up...", i, len(seed_rows), segment, mpn)
        product = lookup_part(client, mpn)
        if product is None:
            n_fail += 1
            continue

        obs = client.extract_observation(product)
        out_rows.append({
            "segment": segment,
            "digikey_part_number": obs.digikey_part_number,
            "manufacturer_part_number": obs.manufacturer_part_number or mpn,
            "manufacturer": obs.manufacturer,
            "category": obs.category,
            "ref_quantity_available": obs.quantity_available,
            "ref_unit_price_usd": f"{obs.unit_price_usd:.6f}",
            "ref_price_break_qty": obs.price_break_qty,
            "ref_lead_time_weeks": obs.lead_time_weeks if obs.lead_time_weeks is not None else "",
            "ref_product_status": obs.product_status,
            "selected_at_utc": timestamp,
        })
        n_ok += 1
        log.info("    -> %s | qty=%d | $%.4f @ break %d | LT=%s",
                 obs.digikey_part_number, obs.quantity_available,
                 obs.unit_price_usd, obs.price_break_qty,
                 obs.lead_time_weeks if obs.lead_time_weeks is not None else "?")

    if not out_rows:
        log.error("Nothing to write. Aborting.")
        sys.exit(1)

    # Write universe.csv
    fieldnames = [
        "segment", "digikey_part_number", "manufacturer_part_number",
        "manufacturer", "category",
        "ref_quantity_available", "ref_unit_price_usd", "ref_price_break_qty",
        "ref_lead_time_weeks", "ref_product_status", "selected_at_utc",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in out_rows:
            w.writerow(r)

    log.info("=== Done ===")
    log.info("  Resolved (new): %d", n_ok)
    log.info("  Reused (cached): %d", n_skip)
    log.info("  Failed lookup:  %d", n_fail)
    log.info("  Total in universe: %d", len(out_rows))

    by_seg: dict[str, int] = {}
    for r in out_rows:
        by_seg[r["segment"]] = by_seg.get(r["segment"], 0) + 1
    for seg in sorted(by_seg):
        log.info("    %s: %d parts", seg, by_seg[seg])
    log.info("Wrote %s", args.output)


if __name__ == "__main__":
    main()
