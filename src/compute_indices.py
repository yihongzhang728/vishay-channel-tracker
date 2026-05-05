"""
Compute three indices per segment from the daily history:
  1. Unit (stock) Index — median across parts of (qty_t / qty_ref) * 100
  2. Price Index        — median across parts of (price_t / price_ref) * 100
  3. Lead-Time Index    — median lead-time weeks across parts (absolute, not relative)

Reference values are taken from data/universe.csv (the day-0 snapshot).
Median is used (not mean) so a single part stockout doesn't dominate the index.

Output: data/indices.csv with columns
  observation_date, segment, unit_index, price_index, lead_time_weeks_median,
  parts_with_data, parts_in_universe
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

import pandas as pd

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("compute_indices")


def compute(
    universe_path: Path, history_path: Path, output_path: Path
) -> pd.DataFrame:
    log.info("Loading universe from %s", universe_path)
    universe = pd.read_csv(universe_path)
    universe["ref_quantity_available"] = pd.to_numeric(
        universe["ref_quantity_available"], errors="coerce"
    )
    universe["ref_unit_price_usd"] = pd.to_numeric(
        universe["ref_unit_price_usd"], errors="coerce"
    )

    log.info("Loading history from %s", history_path)
    history = pd.read_csv(history_path)
    history = history[history["fetch_status"] == "ok"].copy()
    history["quantity_available"] = pd.to_numeric(
        history["quantity_available"], errors="coerce"
    )
    history["unit_price_usd"] = pd.to_numeric(
        history["unit_price_usd"], errors="coerce"
    )
    history["lead_time_weeks"] = pd.to_numeric(
        history["lead_time_weeks"], errors="coerce"
    )

    # Merge ref values onto each observation
    merged = history.merge(
        universe[
            [
                "digikey_part_number",
                "ref_quantity_available",
                "ref_unit_price_usd",
            ]
        ],
        on="digikey_part_number",
        how="inner",
    )

    # Relative ratios. Guard against zero refs.
    merged["rel_qty"] = merged["quantity_available"] / merged["ref_quantity_available"]
    merged.loc[merged["ref_quantity_available"] <= 0, "rel_qty"] = pd.NA

    merged["rel_price"] = merged["unit_price_usd"] / merged["ref_unit_price_usd"]
    merged.loc[merged["ref_unit_price_usd"] <= 0, "rel_price"] = pd.NA

    # Aggregate
    universe_counts = universe.groupby("segment")["digikey_part_number"].nunique()

    agg = (
        merged.groupby(["observation_date", "segment"])
        .agg(
            unit_index=("rel_qty", lambda s: 100 * s.dropna().median() if s.dropna().size else None),
            price_index=("rel_price", lambda s: 100 * s.dropna().median() if s.dropna().size else None),
            lead_time_weeks_median=("lead_time_weeks", lambda s: s.dropna().median() if s.dropna().size else None),
            parts_with_data=("digikey_part_number", "nunique"),
        )
        .reset_index()
    )
    agg["parts_in_universe"] = agg["segment"].map(universe_counts)

    agg = agg.sort_values(["segment", "observation_date"]).reset_index(drop=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    agg.to_csv(output_path, index=False, float_format="%.4f")
    log.info("Wrote %d rows to %s", len(agg), output_path)

    return agg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", type=Path, default=Path("data/universe.csv"))
    ap.add_argument("--history", type=Path, default=Path("data/history.csv"))
    ap.add_argument("--output", type=Path, default=Path("data/indices.csv"))
    args = ap.parse_args()

    compute(args.universe, args.history, args.output)


if __name__ == "__main__":
    main()
