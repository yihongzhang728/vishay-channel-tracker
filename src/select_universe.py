"""
One-time script: build the tracked-parts universe.

Strategy:
  - Resolve Vishay's manufacturer ID(s) from the Manufacturers endpoint (Vishay
    has multiple sub-brands like "Vishay Dale", "Vishay Beyschlag", etc.; we
    grab them all).
  - Resolve the DigiKey CategoryIds for each of the 6 Vishay segments.
  - For each segment, paginate KeywordSearch sorted by QuantityAvailable
    descending — DigiKey doesn't expose a "popularity" field, but stock-on-hand
    is a strong proxy: the parts they carry in highest volume are the ones with
    most demand.
  - Take the top N per segment (default 100), record the day-0 reference values
    (qty, price, lead time) so the index can be calculated relative to it.

Run this ONCE at project setup. The universe is then frozen — daily polling
hits these specific part numbers. To rebalance, archive the old universe and
re-run.
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
log = logging.getLogger("select_universe")

# Map our 6 Vishay segments to the keywords/category-name patterns we'll look up
# via the DigiKey Categories endpoint. These names are matched case-insensitively
# against the category tree at runtime (DigiKey occasionally tweaks naming).
SEGMENT_DEFINITIONS = {
    "MOSFET":     ["Transistors - FETs, MOSFETs - Single", "Transistors - FETs, MOSFETs - Arrays"],
    "Diode":      ["Diodes - Rectifiers - Single", "Diodes - Rectifiers - Arrays", "Diodes - Zener - Single", "TVS - Diodes"],
    "Optoelectronic": ["Optoisolators - Transistor, Photovoltaic Output", "Optoisolators - Logic Output", "LED Indication - Discrete", "Infrared, UV, Visible Emitters"],
    "Resistor":   ["Chip Resistor - Surface Mount", "Through Hole Resistors", "Resistor Networks, Arrays"],
    "Capacitor":  ["Ceramic Capacitors", "Aluminum Electrolytic Capacitors", "Tantalum Capacitors", "Film Capacitors"],
    "Inductor":   ["Fixed Inductors"],
}


def resolve_vishay_manufacturer_ids(client: DigiKeyClient) -> list[int]:
    log.info("Resolving Vishay manufacturer IDs...")
    mfrs = client.list_manufacturers()
    matches = [
        m for m in mfrs
        if "vishay" in (m.get("Name") or "").lower()
    ]
    if not matches:
        raise RuntimeError("No Vishay manufacturers found in DigiKey catalog.")
    ids = [int(m["Id"]) for m in matches]
    log.info("Found %d Vishay manufacturer entities: %s",
             len(matches), [m["Name"] for m in matches])
    return ids


def resolve_category_ids(client: DigiKeyClient, target_names: list[str]) -> dict[str, list[int]]:
    """
    Walk the category tree (Categories has nested ChildCategories) and find the
    leaf categories whose Name matches any of `target_names`.
    Returns a dict: {our_target_name -> [matching_digikey_category_ids]}.
    """
    log.info("Resolving DigiKey category IDs for %d target categories", len(target_names))
    categories = client.list_categories()

    found: dict[str, list[int]] = {name: [] for name in target_names}

    def walk(node: dict):
        node_name = (node.get("Name") or "").strip()
        for target in target_names:
            if target.lower() == node_name.lower():
                found[target].append(int(node["CategoryId"]))
        for child in node.get("ChildCategories", []) or []:
            walk(child)

    for top in categories:
        walk(top)

    for tgt, ids in found.items():
        if not ids:
            log.warning("No DigiKey CategoryId found for '%s' (will be skipped)", tgt)
        else:
            log.info("  '%s' -> CategoryIds %s", tgt, ids)
    return found


def collect_top_parts_for_segment(
    client: DigiKeyClient,
    segment: str,
    category_ids: list[int],
    vishay_mfr_ids: list[int],
    n_target: int,
) -> list[dict]:
    """Paginate keyword_search to gather up to n_target parts for this segment."""
    log.info("Collecting top %d parts for segment '%s'", n_target, segment)
    collected: list[dict] = []
    seen_dk_pns: set[str] = set()
    offset = 0
    page_size = 50

    while len(collected) < n_target:
        result = client.keyword_search(
            keywords="",
            manufacturer_ids=vishay_mfr_ids,
            category_ids=category_ids,
            limit=page_size,
            offset=offset,
            in_stock_only=True,
            sort_field="QuantityAvailable",
            sort_direction="Descending",
        )
        products = result.get("Products") or []
        if not products:
            log.info("  No more products at offset %d", offset)
            break

        for p in products:
            obs = client.extract_observation(p)
            if not obs.digikey_part_number or obs.digikey_part_number in seen_dk_pns:
                continue
            seen_dk_pns.add(obs.digikey_part_number)
            collected.append({
                "segment": segment,
                "digikey_part_number": obs.digikey_part_number,
                "manufacturer_part_number": obs.manufacturer_part_number,
                "manufacturer": obs.manufacturer,
                "category": obs.category,
                "ref_quantity_available": obs.quantity_available,
                "ref_unit_price_usd": obs.unit_price_usd,
                "ref_price_break_qty": obs.price_break_qty,
                "ref_lead_time_weeks": obs.lead_time_weeks if obs.lead_time_weeks is not None else "",
                "ref_product_status": obs.product_status,
            })
            if len(collected) >= n_target:
                break

        offset += page_size
        # DigiKey's KeywordSearch is capped at offset 2500 in some tiers
        if offset >= 2500:
            log.warning("  Hit pagination ceiling for segment '%s'", segment)
            break

    log.info("  Collected %d parts for segment '%s'", len(collected), segment)
    return collected


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-per-segment", type=int, default=100)
    ap.add_argument("--output", type=Path, default=Path("data/universe.csv"))
    args = ap.parse_args()

    client = load_client_from_env()
    vishay_ids = resolve_vishay_manufacturer_ids(client)

    # Flatten all target names across segments to a single category lookup
    all_target_names = sorted({n for names in SEGMENT_DEFINITIONS.values() for n in names})
    name_to_ids = resolve_category_ids(client, all_target_names)

    rows: list[dict] = []
    for segment, target_names in SEGMENT_DEFINITIONS.items():
        cat_ids: list[int] = []
        for n in target_names:
            cat_ids.extend(name_to_ids.get(n, []))
        if not cat_ids:
            log.error("No category IDs resolved for segment '%s' — skipping", segment)
            continue
        rows.extend(collect_top_parts_for_segment(
            client, segment, cat_ids, vishay_ids, args.n_per_segment
        ))

    if not rows:
        log.error("No parts collected. Aborting.")
        sys.exit(1)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys()) + ["selected_at_utc"]
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with args.output.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            r["selected_at_utc"] = timestamp
            w.writerow(r)

    log.info("Wrote %d rows to %s", len(rows), args.output)
    by_seg: dict[str, int] = {}
    for r in rows:
        by_seg[r["segment"]] = by_seg.get(r["segment"], 0) + 1
    for seg, count in by_seg.items():
        log.info("  %s: %d parts", seg, count)


if __name__ == "__main__":
    main()
