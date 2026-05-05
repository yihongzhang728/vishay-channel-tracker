# Vishay Distributor Channel Tracker

Tracks daily stock, price, and lead time on a frozen universe of ~600 Vishay parts
(top 100 most-stocked per segment: MOSFET / Diode / Optoelectronic / Resistor /
Capacitor / Inductor) via the DigiKey Product Information API. Builds a
median-based unit index, price index, and lead-time index per segment and
publishes an HTML dashboard via GitHub Pages.

## What it does

- **Day-0 setup (one-time):** picks the most-stocked Vishay parts per segment
  and freezes them as the tracked universe.
- **Every morning (10:00 UTC):** GitHub Actions polls DigiKey for current values,
  appends to `data/history.csv`, recomputes `data/indices.csv`, regenerates
  `docs/index.html`, and commits all three back to `main`.
- **Dashboard:** GitHub Pages serves `docs/index.html` at
  `https://<your-username>.github.io/<repo>/`.

## One-time setup

### 1. Get DigiKey API credentials (free, ~10 min)

1. Sign up at https://developer.digikey.com.
2. Create an Organization and a Production app.
3. Subscribe the app to **Product Information V4**.
4. Set the OAuth callback to anything (we use 2-legged client-credentials, no
   browser callback). `https://localhost` is fine.
5. Copy your **Client ID** and **Client Secret**.

### 2. Create the GitHub repo

```bash
# In this directory:
git init
git add .
git commit -m "Initial commit"
git branch -M main
# Create an empty repo on github.com first, then:
git remote add origin https://github.com/<your-username>/vishay-channel-tracker.git
git push -u origin main
```

### 3. Add secrets to the repo

In the repo on GitHub: **Settings → Secrets and variables → Actions → New
repository secret**. Add two:

- `DIGIKEY_CLIENT_ID`
- `DIGIKEY_CLIENT_SECRET`

### 4. Enable GitHub Pages

**Settings → Pages → Build and deployment**:

- Source: **Deploy from a branch**
- Branch: **main**, folder: **/docs**

After the first daily run commits a dashboard, your URL is shown on this page.

### 5. Build the universe (run once, locally)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export DIGIKEY_CLIENT_ID=...
export DIGIKEY_CLIENT_SECRET=...

python src/select_universe.py --n-per-segment 100
```

This writes `data/universe.csv`. Inspect it, commit, push:

```bash
git add data/universe.csv && git commit -m "Day-0 universe" && git push
```

### 6. Trigger the first run

In the repo: **Actions → Daily Vishay channel tracking → Run workflow**.
After it completes, the dashboard is live at your Pages URL.

## Day-to-day

Nothing. The cron runs every morning. Open the dashboard URL when you want
to see the trend.

To inspect raw data: `data/history.csv` is appended each day, `data/indices.csv`
is rebuilt each day. Both are diffable in git.

## Rebalancing the universe

Stock-on-hand changes over time, and parts go EOL. Every quarter or two, archive
the existing series and re-run selection:

```bash
mv data/universe.csv data/universe.archived.$(date +%Y%m%d).csv
mv data/history.csv  data/history.archived.$(date +%Y%m%d).csv
python src/select_universe.py --n-per-segment 100
git add data/ && git commit -m "Universe rebalance" && git push
```

A new day-0 baseline starts.

## Index methodology

- **Unit Index** = median across tracked parts of `(qty_t / qty_ref) × 100`.
  Day-0 = 100. Rises if DigiKey is restocking; falls if channel is drawing down.
- **Price Index** = same construction on unit price at the **qty-1000 price
  break** (held constant per part across days so break-tier shifts don't
  contaminate the signal).
- **Lead Time** = absolute median of manufacturer-quoted lead time in weeks
  (DigiKey `ManufacturerLeadWeeks` field). Not normalized — weeks are weeks.

Median (not mean) so a single part stockout doesn't dominate the segment.

## Caveats / what this is NOT

- **DigiKey only.** Vishay sells through Mouser, Arrow, Avnet, Newark, and direct
  to OEMs. DigiKey holds maybe ~25% of channel stock. Trend direction is more
  reliable than levels.
- **Stock as popularity proxy.** DigiKey doesn't expose a popularity score, so we
  rank by `QuantityAvailable` desc. Imperfect but defensible.
- **Free-tier rate limits.** 1000 calls/day. Polling 600 parts leaves ~400 calls
  of headroom for retries. If you push to 200/segment (=1200 parts), you'll
  exceed the daily budget.
- **Cross-check against 10-Q.** Vishay discloses distributor inventory in MD&A
  every quarter. Use it to validate the index.

## Adding a second distributor (later)

The data layer is segment-agnostic on source. To add Mouser:

1. Write `src/mouser_client.py` mirroring `digikey_client.py`.
2. Add a `source` column to `universe.csv` and `history.csv`.
3. In `compute_indices.py`, group by `(source, segment)` for source-level series,
   or by `segment` only for blended.

Mouser's free API has similar limits to DigiKey's.

## File map

```
.github/workflows/daily.yml   # Cron + commit pipeline
src/digikey_client.py         # OAuth + API wrapper
src/select_universe.py        # Day-0 part selection
src/fetch_daily.py            # Daily polling
src/compute_indices.py        # Aggregation
src/build_dashboard.py        # HTML dashboard generation
data/universe.csv             # Frozen tracked parts (committed)
data/history.csv              # Daily observations (committed, append-only)
data/indices.csv              # Computed indices (committed, rebuilt daily)
docs/index.html               # Dashboard (committed, served by Pages)
```
