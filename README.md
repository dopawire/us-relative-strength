# US Relative-Strength — GICS Sectors & Industries

Ranks all **four GICS levels** (Sector, Industry Group, Industry,
Sub-Industry) by relative strength against the **S&P 500**, with every level
**equally weighted**. Adds a McClellan-style **Market Breadth** oscillator.
It covers every common stock on NASDAQ, NYSE and NYSE American (~5,200 names).

Everything uses free data: **Yahoo Finance** for prices and industry
profiles, **NASDAQ Trader** for the listings, **Wikipedia** for the S&P 1500
GICS constituents, and the **SEC** for CIKs. No API keys and no paid
subscriptions. This is the US port of `nse-relative-strength`, released
under the [MIT License](LICENSE).

## What it produces

A website (FastAPI backend plus vanilla JS frontend, served on `localhost:8001`) with:
- **Four tabs** (Sector / Industry Group / Industry / Sub-Industry). Each is
  an equal-weight RS ranking of its groups, and you can expand any group to
  see its stocks.
  - **Sector / Industry Group** rank every classified stock (~4,700).
    Stocks outside the S&P 1500 carry an **≈ approx** badge: their
    classification is mapped, not official GICS.
  - **Industry / Sub-Industry** are **100% official GICS**: only S&P 1500
    constituents (plus hand-checked overrides).
- **Per-stock columns:**
  - RS sparkline and `RS_STS%`
  - last price
  - EMA20/50/100/150/200 flags
  - the RS line vs its 21-day EMA
  - % off the 52-week high
  - ADR%
- **Filters:** search, price-above-EMA, %-off-high, ADR, RS ≥ N%, RS above
  EMA21, and a ★ watchlist. Filters combine, and the result can go to
  **TradingView export** (`NASDAQ:AAPL`, `NYSE:BRK.B`, …) or **CSV**.
- **Per-stock chart:** click any stock for its RS line with EMA21.
- **Daily deltas:** `RS_STS%` change vs the previous snapshot, per group and stock.
- **Market Breadth:** the oscillator itself, a per-sector breadth dashboard
  (11 GICS sectors), and **divergence alerts**.
- **Rotation tab:** daily RS lines per industry group vs the S&P 500.
- **New Listings tab:** IPOs waiting for their first 26-day ranking, ranked
  vs the S&P 500 since the listing-day close.
- A printable **daily digest** (`daily_report.html`) after every build.

## Method

- Daily closes per stock come from Yahoo; the benchmark is the S&P 500 (`^GSPC`).
- **Group index:** each stock is set to base 100 on day 0 of the window, and
  the group index is the plain average across its stocks each day.
- `RS[t] = group_index[t] / benchmark_norm[t]`, both base 100.
- `RS_STS% = PERCENTRANK.INC(RS_series, latest RS)` over the 26-day window.
- **Price floor:** stocks whose last price is under `min_price` (default $1)
  are left out of the rankings.
- **Gaps:** stocks without a close on every window day are left out of the view.
- **Data breaks:** a one-day move of ×10 or more, either way, is treated as a
  data break (typically a reverse split Yahoo adjusted late), not a trading
  move. Real +150% days stay in.

## Universe & classification (`refresh_universe.py`)

1. **Listings.** NASDAQ Trader's `nasdaqlisted.txt` and `otherlisted.txt`:
   - Kept: common stock, ordinary shares, ADRs and MLP common units.
   - Dropped: ETFs, test issues, warrants, rights, SPAC units, preferreds,
     notes and bonds, structured products, and closed-end funds.
   - Funds are spotted from Yahoo's business summary; BDCs stay, because
     they are operating companies.
   - SPAC shells are dropped using Yahoo's "Shell Companies" industry.
2. **Classification.** For each stock, the first rule that applies wins:
   - `us_classification_overrides.csv`: hand fixes, or `EXCLUDE` for
     non-equity that lists as "Common Stock".
   - **S&P 500 / 400 / 600** (Wikipedia): the real **GICS Sub-Industry**.
     The other three levels come from `gics_hierarchy.csv`, the GICS 2023
     tree (163 sub-industries). Wikipedia's sector column is ignored because
     it lags reclassifications.
   - **Everything else:** Yahoo's industry, mapped to a GICS Sub-Industry
     through `yahoo_to_gics.csv`.
     - Its `auto` rows are learned on every run: each Yahoo industry gets the
       most common GICS sub-industry among the S&P 1500 stocks that share it.
     - `manual` rows always win. Edit one and set `source` to `manual` to
       pin it.
   - Measured accuracy of the mapping, holding each S&P stock out and
     predicting it from the others: **sector 94%, industry group 88%,
     industry 81%, sub-industry 72%**.
   - So mapped stocks (`source = yahoo`, about 3,750) are ranked **only on the
     Sector and Industry Group tabs**, badged approximate. The Industry and
     Sub-Industry tabs contain only official GICS (`sp1500` / `override`, about
     1,500 stocks).
   - Official GICS is licensed by MSCI / S&P, and no free source covers the
     whole market. The SEC's SIC codes are official and free, but too coarse
     to group by (Visa, PayPal and Uber are all "Business Services, NEC").
3. **Renames and delistings.**
   - Renames are detected by SEC CIK and written to `us_symbol_changes.csv`;
     the build stitches the old ticker's history onto the new one.
   - Delisted symbols are archived to `us_delisted.csv`.

Yahoo profiles are cached (`~/.cache/us-rs/.yh_profile_cache.json`), so
only new listings are fetched after the first run. Profiles are re-checked
every 90 days, at most 300 per run.

## Scripts

| Script | Purpose |
|---|---|
| `refresh_universe.py` | Rebuild `us_stock_master.csv`: listings, GICS classification, renames, delistings. |
| `build_rs.py` | Fetch Yahoo prices and rebuild `rs_data.json` (plus the `daily_report.html` digest). |
| `audit_data.py` | Data-integrity audit (caches, holiday calendar, fake bars, freshness, GICS paths). Exits non-zero on issues. |
| `run_daily.sh` | Runs `build_rs.py` and logs to `run.log`. |
| `run_server.sh` | Serves the website on `localhost:8001` (`US_RS_HOST=0.0.0.0` to expose it on the LAN). |
| `cron_us.sh` | Cron entry point: universe refresh, build and audit (plus optional Telegram alerts). |
| `install_cron.sh` | Installs the cron entries at 17:30 New York time, converted to the system time zone for both EST and EDT. |
| `export_static.py` | Builds a read-only static site into `site/`. |
| `publish_site.sh` | Commits and pushes the `rs_data.json` snapshot, which triggers the GitHub Pages deploy. |

## One-click launcher

```
bash us-rs.sh --install   # once: adds "US Relative Strength" to the app menu
```

Then open **US Relative Strength** from the app menu, or run `bash us-rs.sh`.
It starts the server in the background, updates prices if a session is
missing, and opens the dashboard at http://localhost:8001. Use
`bash us-rs.sh --stop` (or right-click the icon → Stop server) to stop it.

## Daily use

```
python3 refresh_universe.py   # 1. new listings / renames (fast after the first run)
bash run_daily.sh             # 2. fetch today's prices, rebuild data (~10 min)
bash run_server.sh            # 3. start the website → http://localhost:8001
```

### Automation (optional)

`bash install_cron.sh` installs stale-gated entries:
- **17:30 New York**, on US weekdays. On an IST machine that is 03:00 or
  04:00 Tuesday–Saturday, depending on DST.
- An **hourly catch-up**.
- An **@reboot catch-up**.

Each run is a no-op once the data covers the latest session. The script is
named `cron_us.sh` on purpose: the NSE project's `install_cron.sh` removes
every crontab line that mentions `cron_daily.sh`.

Telegram alerts work as in the NSE version, but the config lives in
`~/.config/us-rs/notify.env`.

## Build options

```
python3 build_rs.py               # full fetch + rebuild (default)
python3 build_rs.py --html-only   # rebuild from cache only (no fetching)
python3 build_rs.py --fast        # incremental top-up (what "Update Prices" uses)
python3 refresh_universe.py --dry-run      # report counts, change nothing
python3 refresh_universe.py --no-profiles  # classify from cached Yahoo profiles only
```

## Setup (first time)

```
python3 -m venv ~/.venvs/us-rs
~/.venvs/us-rs/bin/pip install -r requirements.txt -r backend/requirements.txt
~/.venvs/us-rs/bin/python -m playwright install chromium   # frontend smoke tests only
~/.venvs/us-rs/bin/python refresh_universe.py              # ~10 min: ~5,400 Yahoo profiles
~/.venvs/us-rs/bin/python build_rs.py                      # ~10-20 min: 2y of prices
```

The SEC asks for a contact in the User-Agent. Set
`US_RS_SEC_UA="your-name you@example.com"`; otherwise a placeholder is used.

## Tests

```
PY=~/.venvs/us-rs/bin/python
$PY -m pytest tests/ -q
NO_BROWSER=1 $PY -m pytest tests/ -q   # skip browser tests
$PY -m ruff check .
```

## Configuration & layout

- `config.toml`: window, benchmark, EMA periods, `min_price`,
  `corp_action_jump`, fetch ranges, and so on.
- `us_holidays.csv`: NYSE full-day holidays. Early-close days are normal
  sessions. **Add next year's dates by hand each December**; the audit
  fails from December 1 if they are missing.
- `gics_hierarchy.csv`, `yahoo_to_gics.csv`, `us_classification_overrides.csv`: see above.
- **Price caches** live in `~/.cache/us-rs/` (override with `US_RS_CACHE_DIR`).
- `rs_engine/` is the computation core, shared in design with the NSE project.
