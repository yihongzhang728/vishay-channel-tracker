"""
Post daily Vishay channel update to Slack via incoming webhook.

Reads data/indices.csv and posts a compact summary table plus dashboard link.
Designed to be silent if SLACK_WEBHOOK_URL isn't set, so the workflow keeps
working even before Slack is configured.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("post_slack")

SEGMENT_ORDER = ["MOSFET", "Diode", "Optoelectronic", "Resistor", "Capacitor", "Inductor"]


def arrow(delta: float, threshold: float = 0.05) -> str:
    if pd.isna(delta):
        return ""
    if delta > threshold:
        return "▲"
    if delta < -threshold:
        return "▼"
    return "·"


def build_table(df: pd.DataFrame) -> str:
    """Compact monospace table: latest values + Δ vs day-1 + Δ vs week-ago."""
    if df.empty:
        return "(no data yet)"

    latest_date = df["observation_date"].max()
    latest_dt = pd.to_datetime(latest_date)

    latest = df[df["observation_date"] == latest_date].set_index("segment")

    # Prior day
    dates_sorted = sorted(df["observation_date"].unique())
    prior_day = (
        df[df["observation_date"] == dates_sorted[-2]].set_index("segment")
        if len(dates_sorted) >= 2 else None
    )

    # ~7 days ago — find the latest observation_date <= latest - 7 days
    week_target = (latest_dt - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
    week_dates = [d for d in dates_sorted if d <= week_target]
    week_ago = (
        df[df["observation_date"] == week_dates[-1]].set_index("segment")
        if week_dates else None
    )

    header = f"{'Segment':<15} {'Unit':>7} {'1d':>5} {'7d':>5}  {'Price':>7} {'1d':>5} {'7d':>5}  {'LT':>5}"
    sep = "─" * len(header)
    lines = [header, sep]

    for seg in SEGMENT_ORDER:
        if seg not in latest.index:
            continue
        r = latest.loc[seg]

        unit = r["unit_index"]
        price = r["price_index"]
        lt = r["lead_time_weeks_median"]

        u_1d = arrow(unit - prior_day.loc[seg, "unit_index"]) if prior_day is not None and seg in prior_day.index else ""
        p_1d = arrow(price - prior_day.loc[seg, "price_index"]) if prior_day is not None and seg in prior_day.index else ""
        u_7d = arrow(unit - week_ago.loc[seg, "unit_index"]) if week_ago is not None and seg in week_ago.index else ""
        p_7d = arrow(price - week_ago.loc[seg, "price_index"]) if week_ago is not None and seg in week_ago.index else ""

        line = (
            f"{seg:<15} "
            f"{unit:>7.1f} {u_1d:>5} {u_7d:>5}  "
            f"{price:>7.1f} {p_1d:>5} {p_7d:>5}  "
            f"{lt if not pd.isna(lt) else '?':>5}"
        )
        lines.append(line)

    return "\n".join(lines)


def post_to_slack(webhook_url: str, payload: dict) -> None:
    req = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        log.info("Slack response: %s %s", resp.status, body[:200])


def main():
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        log.info("SLACK_WEBHOOK_URL not set — skipping Slack post.")
        return

    dashboard = os.environ.get(
        "DASHBOARD_URL", "https://yihongzhang728.github.io/vishay-channel-tracker/"
    )

    indices_path = Path("data/indices.csv")
    if not indices_path.exists():
        log.info("No indices.csv yet — skipping.")
        return

    df = pd.read_csv(indices_path)
    if df.empty:
        log.info("indices.csv is empty — skipping.")
        return

    latest_date = df["observation_date"].max()
    n_segments = df[df["observation_date"] == latest_date]["segment"].nunique()
    n_parts = int(df[df["observation_date"] == latest_date]["parts_with_data"].sum())

    table = build_table(df)

    payload = {
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": f"Vishay Channel Tracker — {latest_date}",
                    "emoji": False,
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"{n_segments} segments · {n_parts} parts polled · "
                            f"<{dashboard}|Open dashboard →>"
                        ),
                    }
                ],
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```\n{table}\n```"},
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Δ uses median index. ▲ ≥+0.05 · ▼ ≤−0.05 · · flat. LT = median lead time, weeks.",
                    }
                ],
            },
        ]
    }

    try:
        post_to_slack(webhook, payload)
    except urllib.error.HTTPError as e:
        log.error("Slack HTTP error %s: %s", e.code, e.read().decode("utf-8", errors="replace"))
        # Don't fail the whole workflow on a Slack hiccup
        sys.exit(0)
    except Exception as e:
        log.error("Slack post failed: %s", e)
        sys.exit(0)


if __name__ == "__main__":
    main()
