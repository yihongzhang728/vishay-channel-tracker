"""
Daily fetch script.

For every part in data/universe.csv, hit DigiKey's ProductDetails endpoint to
get current quantity, price (at the same break tier as day-0), lead time, and
status. Append one row per part to data/history.csv with the observation_date.

Designed to be re-run idempotently: if we already have an observation for
today's UTC date, skip writing a duplicate.
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
log = logging.getLogger("fetch_daily")

HISTORY_FIELDS = [
    "observation_date",   # YYYY-MM-DD UTC
    "segment",
    "digikey_part_number",
    "manufacturer_part_number",
    "category",
    "quantity_available",
    "unit_price_usd",
    "price_break_qty",
    "lead_time_weeks",
    "product_status",
    "fetch_status",       # "ok" | "error:<msg>"
]


def load_universe(path: Path) -> list[dict]:
    if not path.exists():
        raise FileNotFoundError(
            f"Universe file not found at {path}. Run select_universe.py first."
        )
    with path.open() as f:
        return list(csv.DictReader(f))


def already_have_today(history_path: Path, today: str) -> bool:
    if not history_path.exists():
        return False
    with history_path.open() as f:
        # Cheap check: scan the last ~10KB instead of full file
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 10_000))
        tail = f.read()
        return today in tail


def fetch_one_part(
    client: DigiKeyClient,
    universe_row: dict,
) -> dict:
    """Fetch current state for one tracked part. Returns a row dict for history.csv."""
    today = datetime.now(timezone.utc).date().isoformat()
    dk_pn = universe_row["digikey_part_number"]
    target_break = int(universe_row.get("ref_price_break_qty") or 1000) or 1000

    base_row = {
        "observation_date": today,
        "segment": universe_row["segment"],
        "digikey_part_number": dk_pn,
        "manufacturer_part_number": universe_row["manufacturer_part_number"],
        "category": universe_row.get("category", ""),
    }

    try:
        resp = client.product_details(dk_pn)
        obs = client.extract_observation(resp, target_break_qty=target_break)
        return {
            **base_row,
            "quantity_available": obs.quantity_available,
            "unit_price_usd": f"{obs.unit_price_usd:.6f}",
            "price_break_qty": obs.price_break_qty,
            "lead_time_weeks": obs.lead_time_weeks if obs.lead_time_weeks is not None else "",
            "product_status": obs.product_status,
            "fetch_status": "ok",
        }
    except Exception as e:  # pragma: no cover
        log.warning("Failed to fetch %s: %s", dk_pn, e)
        return {
            **base_row,
            "quantity_available": "",
            "unit_price_usd": "",
            "price_break_qty": "",
            "lead_time_weeks": "",
            "product_status": "",
            "fetch_status": f"error:{type(e).__name__}",
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", type=Path, default=Path("data/universe.csv"))
    ap.add_argument("--history", type=Path, default=Path("data/history.csv"))
    ap.add_argument("--force", action="store_true",
                    help="Append even if today's date already appears in history.")
    args = ap.parse_args()

    universe = load_universe(args.universe)
    log.info("Loaded universe: %d parts across %d segments",
             len(universe), len({r["segment"] for r in universe}))

    today = datetime.now(timezone.utc).date().isoformat()
    if not args.force and already_have_today(args.history, today):
        log.info("History already contains observations for %s. Skipping.", today)
        return

    client = load_client_from_env()

    write_header = not args.history.exists()
    args.history.parent.mkdir(parents=True, exist_ok=True)

    n_ok = 0
    n_err = 0
    with args.history.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        if write_header:
            w.writeheader()
        for i, row in enumerate(universe, 1):
            obs_row = fetch_one_part(client, row)
            w.writerow(obs_row)
            if obs_row["fetch_status"] == "ok":
                n_ok += 1
            else:
                n_err += 1
            if i % 50 == 0:
                log.info("Progress: %d/%d (ok=%d err=%d)", i, len(universe), n_ok, n_err)
                f.flush()

    log.info("Done. ok=%d err=%d", n_ok, n_err)
    if n_err > len(universe) * 0.1:
        log.warning("More than 10%% of fetches failed today.")
        sys.exit(2)


if __name__ == "__main__":
    main()
