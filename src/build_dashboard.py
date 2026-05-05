"""
Generate a single self-contained HTML dashboard from data/indices.csv.

Layout:
  - Top: summary cards per segment (latest unit index, price index, lead time)
  - Middle: 3 line charts (unit index, price index, lead time) with one trace
    per segment so cross-segment comparison is immediate
  - Bottom: 6 small-multiple panels, one per segment, showing all 3 series with
    dual y-axes (relative-100 left, lead-time weeks right)
  - Footer: Δ vs day-0 / 7-day / 30-day table

Output: docs/index.html (GitHub Pages serves this directory).
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("build_dashboard")

SEGMENT_ORDER = ["MOSFET", "Diode", "Optoelectronic", "Resistor", "Capacitor", "Inductor"]
COLORS = {
    "MOSFET":         "#1f77b4",
    "Diode":          "#ff7f0e",
    "Optoelectronic": "#2ca02c",
    "Resistor":       "#d62728",
    "Capacitor":      "#9467bd",
    "Inductor":       "#8c564b",
}


def _delta_pct(latest: float, earlier: float) -> str:
    if pd.isna(latest) or pd.isna(earlier) or earlier == 0:
        return ""
    pct = (latest - earlier) / earlier * 100
    return f"{pct:+.1f}%"


def _delta_abs(latest, earlier) -> str:
    if pd.isna(latest) or pd.isna(earlier):
        return ""
    return f"{latest - earlier:+.1f}"


def build_summary_table(df: pd.DataFrame) -> str:
    """Per-segment summary: latest values + Δ vs day-0, 7d, 30d."""
    latest_date = df["observation_date"].max()
    earliest_date = df["observation_date"].min()

    rows_html: list[str] = []
    for seg in SEGMENT_ORDER:
        sub = df[df["segment"] == seg].sort_values("observation_date")
        if sub.empty:
            continue
        latest = sub.iloc[-1]
        day0 = sub.iloc[0]

        latest_dt = pd.to_datetime(latest["observation_date"])
        d7 = sub[sub["observation_date"] <= (latest_dt - pd.Timedelta(days=7)).strftime("%Y-%m-%d")]
        d30 = sub[sub["observation_date"] <= (latest_dt - pd.Timedelta(days=30)).strftime("%Y-%m-%d")]
        d7_row = d7.iloc[-1] if not d7.empty else None
        d30_row = d30.iloc[-1] if not d30.empty else None

        rows_html.append(f"""
            <tr>
              <td><strong style="color:{COLORS[seg]}">●</strong> {seg}</td>
              <td>{latest['unit_index']:.1f}</td>
              <td class="dim">{_delta_pct(latest['unit_index'], day0['unit_index'])}</td>
              <td class="dim">{_delta_pct(latest['unit_index'], d7_row['unit_index']) if d7_row is not None else ''}</td>
              <td class="dim">{_delta_pct(latest['unit_index'], d30_row['unit_index']) if d30_row is not None else ''}</td>
              <td>{latest['price_index']:.1f}</td>
              <td class="dim">{_delta_pct(latest['price_index'], day0['price_index'])}</td>
              <td class="dim">{_delta_pct(latest['price_index'], d7_row['price_index']) if d7_row is not None else ''}</td>
              <td class="dim">{_delta_pct(latest['price_index'], d30_row['price_index']) if d30_row is not None else ''}</td>
              <td>{latest['lead_time_weeks_median']:.1f}</td>
              <td class="dim">{_delta_abs(latest['lead_time_weeks_median'], day0['lead_time_weeks_median'])}</td>
              <td class="dim">{_delta_abs(latest['lead_time_weeks_median'], d7_row['lead_time_weeks_median']) if d7_row is not None else ''}</td>
              <td class="dim">{_delta_abs(latest['lead_time_weeks_median'], d30_row['lead_time_weeks_median']) if d30_row is not None else ''}</td>
              <td class="dim">{int(latest['parts_with_data'])}/{int(latest['parts_in_universe'])}</td>
            </tr>
        """)

    return f"""
    <h3>Latest values & deltas</h3>
    <p class="meta">Tracking window: {earliest_date} → {latest_date}</p>
    <table class="summary">
      <thead>
        <tr>
          <th rowspan="2">Segment</th>
          <th colspan="4">Unit Index (stock)</th>
          <th colspan="4">Price Index (qty=1000 break)</th>
          <th colspan="4">Lead Time (weeks, median)</th>
          <th rowspan="2">Coverage</th>
        </tr>
        <tr>
          <th>Latest</th><th>Δ d0</th><th>Δ 7d</th><th>Δ 30d</th>
          <th>Latest</th><th>Δ d0</th><th>Δ 7d</th><th>Δ 30d</th>
          <th>Latest</th><th>Δ d0</th><th>Δ 7d</th><th>Δ 30d</th>
        </tr>
      </thead>
      <tbody>{''.join(rows_html)}</tbody>
    </table>
    """


def build_line_chart(df: pd.DataFrame, value_col: str, title: str, y_label: str) -> str:
    fig = go.Figure()
    for seg in SEGMENT_ORDER:
        sub = df[df["segment"] == seg].sort_values("observation_date")
        if sub.empty:
            continue
        fig.add_trace(go.Scatter(
            x=sub["observation_date"],
            y=sub[value_col],
            mode="lines+markers",
            name=seg,
            line=dict(color=COLORS[seg], width=2),
            marker=dict(size=4),
            hovertemplate=f"<b>{seg}</b><br>%{{x}}<br>{y_label}: %{{y:.2f}}<extra></extra>",
        ))
    if value_col in ("unit_index", "price_index"):
        fig.add_hline(y=100, line=dict(dash="dot", color="#888", width=1))
    fig.update_layout(
        title=title,
        xaxis_title="Date",
        yaxis_title=y_label,
        height=420,
        margin=dict(l=50, r=20, t=50, b=40),
        template="plotly_white",
        legend=dict(orientation="h", y=-0.18),
    )
    return fig.to_html(full_html=False, include_plotlyjs=False, div_id=f"chart-{value_col}")


def build_small_multiples(df: pd.DataFrame) -> str:
    """One panel per segment, dual y-axis: indices on left, lead time on right."""
    fig = make_subplots(
        rows=2, cols=3,
        subplot_titles=SEGMENT_ORDER,
        specs=[[{"secondary_y": True}] * 3] * 2,
        horizontal_spacing=0.08,
        vertical_spacing=0.18,
    )
    for i, seg in enumerate(SEGMENT_ORDER):
        r, c = (i // 3) + 1, (i % 3) + 1
        sub = df[df["segment"] == seg].sort_values("observation_date")
        if sub.empty:
            continue
        fig.add_trace(go.Scatter(
            x=sub["observation_date"], y=sub["unit_index"],
            name="Unit Index", line=dict(color="#1f77b4", width=2),
            showlegend=(i == 0),
            legendgroup="unit",
        ), row=r, col=c, secondary_y=False)
        fig.add_trace(go.Scatter(
            x=sub["observation_date"], y=sub["price_index"],
            name="Price Index", line=dict(color="#d62728", width=2),
            showlegend=(i == 0),
            legendgroup="price",
        ), row=r, col=c, secondary_y=False)
        fig.add_trace(go.Scatter(
            x=sub["observation_date"], y=sub["lead_time_weeks_median"],
            name="Lead Time (wks)",
            line=dict(color="#2ca02c", width=2, dash="dot"),
            showlegend=(i == 0),
            legendgroup="lead",
        ), row=r, col=c, secondary_y=True)

    fig.update_layout(
        height=720, template="plotly_white",
        title="By segment — indices (left axis) and lead time in weeks (right axis)",
        legend=dict(orientation="h", y=-0.10),
        margin=dict(l=40, r=20, t=70, b=40),
    )
    return fig.to_html(full_html=False, include_plotlyjs=False, div_id="chart-small-mult")


def render_html(df: pd.DataFrame, output_path: Path):
    if df.empty:
        log.warning("Indices dataframe is empty. Writing placeholder.")
        output_path.write_text("<html><body><h1>No data yet.</h1></body></html>")
        return

    summary_table = build_summary_table(df)
    unit_chart = build_line_chart(df, "unit_index", "Unit (stock) Index by segment", "Index (day-0 = 100)")
    price_chart = build_line_chart(df, "price_index", "Price Index by segment (qty 1000 break)", "Index (day-0 = 100)")
    lead_chart = build_line_chart(df, "lead_time_weeks_median", "Median Lead Time by segment", "Weeks")
    small_mult = build_small_multiples(df)

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Vishay Channel Tracker</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, sans-serif;
         max-width: 1400px; margin: 24px auto; padding: 0 16px; color: #222; }}
  h1 {{ margin-bottom: 4px; }}
  .meta {{ color: #777; font-size: 13px; margin-top: 0; }}
  table.summary {{ border-collapse: collapse; width: 100%; font-size: 13px; margin-top: 8px; }}
  table.summary th, table.summary td {{ padding: 6px 10px; border-bottom: 1px solid #eee; text-align: right; }}
  table.summary th {{ background: #f7f7f7; font-weight: 600; }}
  table.summary td:first-child, table.summary th:first-child {{ text-align: left; }}
  table.summary td.dim {{ color: #777; font-size: 12px; }}
  .chart-row {{ margin-top: 24px; }}
  .footer {{ color: #999; font-size: 12px; margin-top: 32px; border-top: 1px solid #eee; padding-top: 12px; }}
  details {{ margin-top: 8px; }}
  summary {{ cursor: pointer; color: #555; }}
</style>
</head>
<body>
<h1>Vishay Distributor Channel Tracker</h1>
<p class="meta">DigiKey-only · Median index across tracked parts per segment · Last refresh: {generated_at}</p>

{summary_table}

<div class="chart-row">{unit_chart}</div>
<div class="chart-row">{price_chart}</div>
<div class="chart-row">{lead_chart}</div>
<div class="chart-row">{small_mult}</div>

<details>
  <summary>Methodology</summary>
  <p>Tracked universe: top ~100 most-stocked Vishay parts per segment as of day-0,
     held fixed thereafter. Universe rebalance archives prior series and starts a
     new day-0 baseline.</p>
  <p>Unit Index = median across tracked parts in the segment of
     (current_quantity_available / day-0_quantity_available) × 100.
     Day-0 = 100 by construction.</p>
  <p>Price Index = same construction on the unit price at the qty-1000 price break
     (or nearest higher break if 1000 unavailable). Holding the break tier constant
     across days isolates list-price changes from break-tier changes.</p>
  <p>Lead Time shown is the absolute median of manufacturer-quoted lead time in weeks
     across tracked parts (DigiKey ManufacturerLeadWeeks field). Not normalized.</p>
  <p>Parts that fail to fetch on a given day are excluded from that day's median;
     the Coverage column shows fetched / universe size.</p>
</details>

<p class="footer">
  Source: DigiKey Product Information API v4. Single-distributor view — does not
  reflect Vishay's full channel inventory. For revenue-modeling work, ground-truth
  against the distributor inventory disclosure in Vishay's 10-Q MD&amp;A.
</p>
</body>
</html>
"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    log.info("Wrote dashboard to %s (%d bytes)", output_path, len(html))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--indices", type=Path, default=Path("data/indices.csv"))
    ap.add_argument("--output", type=Path, default=Path("docs/index.html"))
    args = ap.parse_args()

    if not args.indices.exists():
        log.error("Indices file not found at %s. Run compute_indices.py first.", args.indices)
        return
    df = pd.read_csv(args.indices)
    render_html(df, args.output)


if __name__ == "__main__":
    main()
