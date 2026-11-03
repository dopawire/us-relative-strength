#!/usr/bin/env python3
"""
Build a Relative-Strength view across all four GICS-style classification
levels (Sector, Industry Group, Industry, Sub-Industry), equally weighted,
exported as rs_data.json for the web frontend.

Data source: Yahoo Finance chart API (free, no key, no auth).
Universe / 4-level map: us_stock_master.csv (refresh_universe.py).

Method (mirrors RS.xlsx):
  * For each constituent stock, pull daily closes from Yahoo (BRK-B style symbols).
  * Build an EQUAL-WEIGHT index per group at each level:
        each stock normalised to base-100 on the first day of the window,
        then averaged across constituents at every date.
  * Benchmark = S&P 500 (^GSPC), also normalised to base-100.
  * Relative Strength series  RS[t] = group_index[t] / bench_norm[t].
  * RS_STS%  = PERCENTRANK(RS_series, latest RS)  -> 0..100%
               (same as Excel PERCENTRANK.INC used in the sheet).
  * Sparkline = bar chart of the RS series; RS_STS% cell gets a green gradient.

The price cache grows over time: the first run pulls ~2y per symbol; each
later run only tops up the last month and appends, so daily runs are fast.

Usage:
  python3 build_rs.py            # daily run: fetch prices -> rs_data.json
  python3 build_rs.py --limit 40 # quick test on first 40 stocks
  python3 build_rs.py --refresh  # refetch full history for every symbol
  python3 build_rs.py --html-only # rebuild outputs from cached prices only (offline)
"""

import os
import sys
import csv
import json
import time
import random
import hashlib
import argparse
import datetime as dt
import threading
import concurrent.futures as cf
from collections import defaultdict, Counter

import requests

# ---- rs_engine: computation core (re-exported here for compatibility) ----
from rs_engine.config import (CACHE_DIR, WINDOW, FULL_RANGE,
                              APPEND_RANGE, MIN_BARS, EMA_PERIODS, BENCHMARK_NAME, BENCH_YSYM,
                              YH_WORKERS, MKT_TZ, is_holiday, MIN_PRICE, standard_company,
                              latest_session)
from rs_engine.maths import (percentrank_inc, ema_flags, rs_ema_flag,
                             last_break_date, pct_off_high, adr_pct,
                             equal_weight_rs)
from rs_engine.breadth import (compute_breadth, compute_macro_breadth,
                               detect_breadth_divergence)
from rs_engine.rotation import compute_rotation
from rs_engine.snapshot import (load_previous_snapshot, seed_from_rs_data,
                                append_snapshot, build_ipo_watch)
from rs_engine.report import write_daily_report

HERE = os.path.dirname(os.path.abspath(__file__))
MASTER_CSV   = os.path.join(HERE, "us_stock_master.csv")    # universe + GICS-style levels
SYMBOL_CHANGES = os.path.join(HERE, "us_symbol_changes.csv")  # old,new,cik,date

# Cache file locations (CACHE_DIR itself lives in rs_engine.config).
PRICE_CACHE  = os.path.join(CACHE_DIR, ".yh_price_cache.json")   # {sym: {date: close}}
HIGH_CACHE   = os.path.join(CACHE_DIR, ".yh_high_cache.json")    # {sym: {date: intraday high}}
LOW_CACHE    = os.path.join(CACHE_DIR, ".yh_low_cache.json")     # {sym: {date: intraday low}}
VOL_CACHE    = os.path.join(CACHE_DIR, ".yh_volume_cache.json")  # {sym: {date: shares traded}}
PROVENANCE   = os.path.join(CACHE_DIR, ".yh_provenance.json")    # {date: source} for non-Yahoo bars
BUILD_LOCK   = os.path.join(CACHE_DIR, ".build.lock")
RS_DATA_JSON = os.path.join(HERE, "rs_data.json")


# Yahoo Finance chart API (free, no key). US symbols need no suffix (BRK-B style).
YH_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"
YH_HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36")}

# Internal level keys stay macro/sector/industry/basic (engine + frontend data
# shape); they carry the GICS levels Sector / Industry Group / Industry /
# Sub-Industry.
LEVELS = [("macro", "Sector"), ("sector", "Industry Group"),
          ("industry", "Industry"), ("basic", "Sub-Industry")]
# Official GICS only exists for S&P 1500 members (and hand-checked overrides);
# every other stock's classification is mapped from its Yahoo industry —
# right ~94% at sector / ~88% at industry group but only ~72% at sub-industry
# (leave-one-out on the S&P 1500).  So mapped stocks are ranked on the two
# upper levels only, badged approximate; Industry / Sub-Industry are 100% GICS.
OFFICIAL_SOURCES = {"sp1500", "override"}
OFFICIAL_ONLY_LEVELS = {"industry", "basic"}
LEVEL_NOTES = {k: "Official GICS only — S&P 1500 constituents (+ hand-checked); "
                  "other stocks are ranked on the Sector / Industry Group tabs"
               for k in OFFICIAL_ONLY_LEVELS}


# --------------------------------------------------------------------------- #
#  Setup helpers
# --------------------------------------------------------------------------- #
def read_universe():
    """Return list of dicts {name, sym, exch, macro, sector, industry, basic}.

    Source: us_stock_master.csv (symbol,company,exchange,cik,sector,
    industryGroup,industry,subIndustry,source) — refresh_universe.py.
    `sym` is the Yahoo symbol (BRK-B), also the cache key.  `approx` marks a
    classification mapped from Yahoo (not official GICS): such a stock keeps
    only its sector + industry group (OFFICIAL_ONLY_LEVELS are left blank).
    """
    rows = []
    with open(MASTER_CSV, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            sym = (r.get("symbol") or "").strip().upper()
            if not sym:
                continue
            rows.append({
                "name":     standard_company(r.get("company") or sym),
                "sym":      sym,
                "exch":     (r.get("exchange") or "").strip(),
                "cik":      (r.get("cik") or "").strip(),
                "macro":    (r.get("sector") or "").strip(),
                "sector":   (r.get("industryGroup") or "").strip(),
                "industry": (r.get("industry") or "").strip(),
                "basic":    (r.get("subIndustry") or "").strip(),
                "approx":   bool(r.get("subIndustry"))
                            and r.get("source") not in OFFICIAL_SOURCES,
            })
    for r in rows:
        if r["approx"]:
            for key in OFFICIAL_ONLY_LEVELS:
                r[key] = ""
    return rows


def primary_share_classes(universe, cache, vols, days=20):
    """One share class per company.  Dual-class companies (GOOG/GOOGL,
    BRK-A/BRK-B, FOX/FOXA…) list every class, and the classes move together —
    in an equal-weight index the company would count twice.  Classes are
    grouped by SEC CIK; the one with the highest average dollar volume over
    the last `days` sessions is kept (no volume data → the shortest symbol).
    Returns (universe without the other classes, {dropped_sym: kept_sym})."""
    def dollar_vol(sym):
        ser, vo = cache.get(sym) or {}, vols.get(sym) or {}
        ds = [d for d in sorted(vo) if d in ser][-days:]
        return sum(ser[d] * vo[d] for d in ds) / len(ds) if ds else 0.0

    by_cik = defaultdict(list)
    for u in universe:
        if u.get("cik"):
            by_cik[u["cik"]].append(u["sym"])
    dropped = {}
    for syms in by_cik.values():
        if len(syms) < 2:
            continue
        keep = max(syms, key=lambda s: (dollar_vol(s), -len(s), s))
        dropped.update({s: keep for s in syms if s != keep})
    return [u for u in universe if u["sym"] not in dropped], dropped


def read_symbol_changes():
    """Symbol renames recorded by refresh_universe.py, in file
    (chronological) order: [(old, new), ...]."""
    if not os.path.exists(SYMBOL_CHANGES):
        return []
    with open(SYMBOL_CHANGES, newline="", encoding="utf-8") as fh:
        return [((r.get("old") or "").strip().upper(), (r.get("new") or "").strip().upper())
                for r in csv.DictReader(fh) if r.get("old") and r.get("new")]


def stitch_renamed(stores, changes):
    """Prepend a renamed stock's pre-rename history (old symbol) to its new
    symbol in each {sym: {date: value}} store — Yahoo often starts the new
    ticker on the rename day, which would otherwise make an old company look
    like a fresh listing.  Only dates before the new series begins are added.
    Returns the number of bars added."""
    added = 0
    for old, new in changes:
        for store in stores:
            src, dst = store.get(old), store.get(new)
            if not src or not dst:
                continue
            first = min(dst)
            for d, v in src.items():
                if d < first:
                    dst[d] = v
                    added += 1
    return added


# --------------------------------------------------------------------------- #
#  Yahoo Finance historical fetch
# --------------------------------------------------------------------------- #
_tls = threading.local()


def write_json_atomic(path, obj):
    """Write JSON via tmp + os.replace so a crash mid-write (kill, power loss,
    disk hiccup) can never leave a truncated/corrupt cache behind."""
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def load_json_cache(path):
    """Load a JSON cache, surviving corruption: warn + return {} instead of
    crashing the whole build. The affected data is refetched on the next run."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: {path} is corrupt ({type(e).__name__}) — starting that "
              f"cache fresh; its data will be refetched.")
        return {}


def _session():
    """One requests.Session per worker thread (keep-alive, thread-safe)."""
    s = getattr(_tls, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(YH_HEADERS)
        _tls.s = s
    return s


def yahoo_closes(ysym, rng):
    """Fetch a Yahoo ticker over `rng`. Returns (closes, highs, lows, vols, err),
    each a {date: value} map. Highs feed the 52-week metric; highs+lows feed
    ADR%; volume picks the primary share class (primary_share_classes)."""
    url = YH_URL.format(sym=requests.utils.quote(ysym), rng=rng)
    for attempt in range(4):
        try:
            r = _session().get(url, timeout=25)
        except requests.RequestException:
            time.sleep(0.8 * (attempt + 1) + random.random())
            continue
        if r.status_code == 429:                      # rate-limited: back off
            time.sleep(1.5 * (attempt + 1) + random.random())
            continue
        if r.status_code != 200:
            return None, None, None, None, f"HTTP {r.status_code}"
        try:
            res = (r.json().get("chart") or {}).get("result")
        except ValueError:
            return None, None, None, None, "bad-json"
        if not res:
            return None, None, None, None, "empty"
        res = res[0]
        ts = res.get("timestamp") or []
        quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        cl = quote.get("close") or []
        hg = quote.get("high") or []
        lw = quote.get("low") or []
        vl = quote.get("volume") or []
        out, hi, lo, vo = {}, {}, {}, {}
        settled = latest_session()       # Yahoo serves today's bar LIVE during the session
        for i, (t, c) in enumerate(zip(ts, cl)):
            if c is None:
                continue
            day = dt.datetime.fromtimestamp(int(t), MKT_TZ).strftime("%Y-%m-%d")
            if day > settled:
                continue                 # in-progress session: never cache a partial bar
            out[day] = float(c)
            h = hg[i] if i < len(hg) else None
            lwv = lw[i] if i < len(lw) else None
            hi[day] = float(h) if h is not None else float(c)
            lo[day] = float(lwv) if lwv is not None else float(c)
            v = vl[i] if i < len(vl) else None
            if v is not None:
                vo[day] = int(v)
        return (out, hi, lo, vo, None) if out else (None, None, None, None, "empty")
    return None, None, None, None, "rate-limited"


INTRA_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
             "?range=5d&interval=5m")


def intraday_day(ysym, day):
    """Return (close, high, low) for `day` (ISO) from Yahoo 5-minute bars, or
    None.  Used to repair days whose DAILY bar is null while intraday bars
    exist (Yahoo's recurring gap pattern — Aug 28 2026, Jul 28 2026, ...).
    close = last bar of the day; high/low = intraday extremes (exact)."""
    try:
        r = _session().get(INTRA_URL.format(sym=requests.utils.quote(ysym)),
                           timeout=25)
        if r.status_code != 200:
            return None
        res = (r.json().get("chart") or {}).get("result")
        if not res:
            return None
        res = res[0]
        ts = res.get("timestamp") or []
        quote = ((res.get("indicators") or {}).get("quote") or [{}])[0]
        cl, hg, lw = (quote.get("close") or [], quote.get("high") or [],
                      quote.get("low") or [])
    except (requests.RequestException, ValueError, KeyError):
        return None
    bars = []
    for i, t in enumerate(ts):
        d = dt.datetime.fromtimestamp(int(t), MKT_TZ).strftime("%Y-%m-%d")
        if d == day and i < len(cl) and cl[i] is not None:
            bars.append((cl[i],
                         hg[i] if i < len(hg) and hg[i] is not None else cl[i],
                         lw[i] if i < len(lw) and lw[i] is not None else cl[i]))
    if not bars:
        return None
    return (bars[-1][0],
            max(b[1] for b in bars),
            min(b[2] for b in bars))


def fetch_prices(universe, cache, highs, lows, fast=False, vols=None):
    """Fetch daily closes + intraday highs/lows for every symbol + benchmark into
    `cache` ({sym:{date:close}}), `highs`, `lows` and `vols` (when given),
    benchmark under '__BENCH__'.

    By DEFAULT every run re-pulls the full FULL_RANGE window for every symbol, so any
    value Yahoo has since revised (splits, late corrections, back-filled gaps) is
    picked up and the output is reproducible. Runtime is dominated by the ~2.4k HTTP
    requests, not payload size, so this costs little over a top-up.

    `fast=True` restores the old incremental append (symbols already holding
    >= MIN_BARS of close AND high AND low history only pull APPEND_RANGE). That is
    quicker but leaves historical data un-revalidated, which silently drifts the
    market-breadth oscillator between runs."""
    def plan(sym):
        if not fast:
            return FULL_RANGE        # default: re-pull the whole window every run
        cl, hg, lw = cache.get(sym), highs.get(sym), lows.get(sym)
        need_full = (not cl or len(cl) < MIN_BARS
                     or not hg or len(hg) < MIN_BARS
                     or not lw or len(lw) < MIN_BARS
                     or (vols is not None and sym != "__BENCH__" and not vols.get(sym)))
        return FULL_RANGE if need_full else APPEND_RANGE

    jobs = [("__BENCH__", BENCH_YSYM)] + [(u["sym"], u["sym"]) for u in universe]
    done = ok = 0
    miss = []
    total = len(jobs)

    def work(item):
        key, ysym = item
        data, hi, lo, vo, err = yahoo_closes(ysym, plan(key))
        return key, data, hi, lo, vo, err

    def flush():
        for path, obj in ((PRICE_CACHE, cache), (HIGH_CACHE, highs), (LOW_CACHE, lows)):
            write_json_atomic(path, obj)
        if vols is not None:
            write_json_atomic(VOL_CACHE, vols)

    with cf.ThreadPoolExecutor(max_workers=YH_WORKERS) as ex:
        for key, data, hi, lo, vo, err in ex.map(work, jobs):
            done += 1
            if data:
                cache.setdefault(key, {}).update(data)   # merge/append
                if hi:
                    highs.setdefault(key, {}).update(hi)
                if lo:
                    lows.setdefault(key, {}).update(lo)
                if vo and vols is not None and key != "__BENCH__":
                    vols.setdefault(key, {}).update(vo)
                ok += 1
            else:
                miss.append((key, err))
            if done % 200 == 0:
                print(f"  {done}/{total} fetched ...")
                flush()
    flush()
    return ok, miss


def purge_flat_carries(cache, highs, lows, have):
    """Yahoo re-serves stale flat carries on some outage days (e.g. 2025-03-18:
    thousands of bars equal to the previous close while the index moved
    +1.8% — a data-pipeline failure day).  For any date where >90% of the
    bars are flat duplicates, drop the flat bars so they can't distort
    EMA/ADR metrics; genuine movers on the day are kept.  Returns purged dates."""
    fake = fake_flat_days(cache, have)
    removed = Counter()
    for s in have:
        ser = cache.get(s, {})
        ds = sorted(ser)
        for a, b in zip(ds, ds[1:]):
            if b in fake and ser[b] == ser[a]:     # flat vs the PREVIOUS bar
                del ser[b]
                highs.get(s, {}).pop(b, None)
                lows.get(s, {}).pop(b, None)
                removed[b] += 1
    purged = sorted(removed)
    if purged:
        write_json_atomic(PRICE_CACHE, cache)
        write_json_atomic(HIGH_CACHE, highs)
        write_json_atomic(LOW_CACHE, lows)
        print(f"Purged flat Yahoo carry-bars on {len(purged)} day(s): {purged}")
    return purged


def repair_intraday_gap(cache, highs, lows, have, thresh, today=None, prov=None):
    """Yahoo occasionally serves null DAILY bars for sessions the market was
    open, while its intraday bars exist (whole-market and index-only gaps).
    For each candidate weekday that lacks broad stock coverage (strictly
    PAST days — never a session in progress), repair every
    missing symbol from Yahoo 5-minute bars.  Also self-heals bogus benchmark
    bars (deviates >1% from the intraday close).  `prov` (optional dict) is tagged with
    {date: source}.  Returns the list of repaired dates."""
    if today is None:
        today = dt.datetime.now(MKT_TZ).date()
    bench = cache.get("__BENCH__", {})
    if not bench:
        return []
    # Candidates: weekdays after the last bench date, plus recent bench dates
    # whose stock coverage is thin (a stale/wrong bench bar must not silently
    # hide a gap).
    candidates = []
    d = dt.date.fromisoformat(max(bench)) + dt.timedelta(days=1)
    while d < today:
        if d.weekday() < 5:
            candidates.append(d.isoformat())
        d += dt.timedelta(days=1)
    for day in sorted(bench)[-7:]:
        if day not in candidates:
            candidates.append(day)
    repaired = []
    for day in candidates:
        if not (dt.date.fromisoformat(day) < today):
            continue                       # never a session in progress
        if is_holiday(day):
            continue                       # official NYSE holiday
        cov = sum(1 for s in have if day in cache.get(s, {}))
        bench_missing = day not in bench
        if cov >= thresh and not bench_missing:
            continue                       # healthy day — nothing to do

        probe = (intraday_day(BENCH_YSYM, day)
                 or (intraday_day(have[0], day) if have else None))
        if probe is None:
            continue                       # holiday, or no intraday bars at all
        i_close = probe[0]
        jobs = []
        if bench_missing or abs(bench[day] / i_close - 1) > 0.01:
            jobs.append(("__BENCH__", BENCH_YSYM))
        jobs += [(s, s) for s in have if day not in cache.get(s, {})]
        filled = 0
        for key, ysym in jobs:
            bar = intraday_day(ysym, day)
            if not bar:
                continue
            c, h, lwv = bar
            cache.setdefault(key, {})[day] = c
            highs.setdefault(key, {})[day] = h
            lows.setdefault(key, {})[day] = lwv
            filled += 1
        if filled:
            repaired.append(day)
            if prov is not None:
                prov[day] = "intraday"
            print(f"Repaired {day} from intraday bars: {filled} symbols "
                  f"(Yahoo daily bars were null).")
    if repaired:
        write_json_atomic(PRICE_CACHE, cache)
        write_json_atomic(HIGH_CACHE, highs)
        write_json_atomic(LOW_CACHE, lows)
    return repaired


def fake_flat_days(cache, have, sample=300):
    """Dates whose stock bars are >90% flat duplicates of each stock's
    PREVIOUS bar (sorted by date): Yahoo's holiday artifacts and stale-carry
    outage days (2025-03-18).  One pass over a sample of symbols.  Real
    trading days virtually never have >90% flat closes."""
    flat, total = Counter(), Counter()
    for s in have[:sample]:
        ser = cache.get(s) or {}
        ds = sorted(ser)
        for a, b in zip(ds, ds[1:]):
            total[b] += 1
            if ser[b] == ser[a]:
                flat[b] += 1
    return {d for d, n in total.items() if n >= 100 and flat[d] / n > 0.9}


def is_fake_flat_day(cache, have, day, sample=300):
    """True when `day` looks fake (see fake_flat_days) — such dates must never
    be filled into the benchmark."""
    return day in fake_flat_days(cache, have, sample)


def fill_benchmark_gaps(bench, cache, have, thresh):
    """Fill benchmark days that Yahoo missed (index symbols occasionally publish
    null OHLC while individual stocks publish fine) with a synthetic close:
    previous benchmark close × (1 + equal-weight mean stock return), requiring
    ≥ 100 stocks.  (The intraday repair has already tried Yahoo's 5m bars.)  Only dates with broad stock coverage (≥ `thresh`)
    are considered.  Mutates `bench`; returns [(date, method), ...] filled."""
    bench_dates = set(bench.keys())
    all_dates = set()
    for s in have:
        all_dates.update(cache[s].keys())
    # Historical gaps are legitimate too (the market traded but Yahoo's index
    # bar is simply absent).  Fake holiday-bar days
    # are already excluded via is_holiday / is_fake_flat_day below, so no
    # date-window cutoff is needed.
    missing = sorted(d for d in all_dates if d not in bench_dates
                     if sum(1 for s in have if d in cache[s]) >= thresh
                     if not is_holiday(d)
                     if not is_fake_flat_day(cache, have, d))
    if not missing:
        return []
    filled = []
    for d in missing:
        prev_dates = sorted(dd for dd in bench_dates if dd < d)
        if not prev_dates:
            continue
        prev_d = prev_dates[-1]
        returns = []
        for s in have:
            if prev_d in cache[s] and d in cache[s]:
                p0, p1 = cache[s][prev_d], cache[s][d]
                if p0 and p0 > 0:
                    returns.append(p1 / p0 - 1)
        if len(returns) >= 100:
            bench[d] = round(bench[prev_d] * (1 + sum(returns) / len(returns)), 2)
            filled.append((d, "synthetic"))
    return filled


# --------------------------------------------------------------------------- #
#  JSON export for the FastAPI backend
# --------------------------------------------------------------------------- #
def save_rs_json(levels, breadth, meta, path, rotation=None):
    """Write precomputed RS data so the backend can serve it without recomputing."""
    data = {
        "meta": meta,
        "breadth": breadth if breadth else {"dates": [], "osc": [], "n": 0},
        "rotation": rotation or {"dates": [], "groups": []},
        "levels": [],
    }
    gid = 0
    for key, label, items in levels:
        level_data = {"key": key, "label": label, "groups": [],
                      "note": LEVEL_NOTES.get(key)}
        for it in items:
            members_out = []
            for m in it["members"]:
                members_out.append({
                    "n": m["name"], "s": m["sym"], "x": m.get("exch"),
                    "r": [round(x, 3) for x in m["rs"]],
                    "p": round(m["pct"], 4),
                    "e": m.get("ema") or [-1] * len(EMA_PERIODS),
                    "b": m.get("rse", -1),
                    "d": m.get("d"),
                    "l": round(m["ltp"], 2) if m.get("ltp") is not None else None,
                    "h": round(m["hi52"], 1) if m.get("hi52") is not None else None,
                    "a": round(m["adr"], 2) if m.get("adr") is not None else None,
                    "nw": m.get("new"),       # new listing: days since listing
                    "ap": 1 if m.get("approx") else None,   # classification mapped, not GICS
                })
            level_data["groups"].append({
                "id": gid, "name": it["name"],
                "n": it["n"], "pct": round(it["pct"], 4),
                "dp": it.get("dp"),
                "r": [round(x, 3) for x in it["rs"]],
                "members": members_out,
            })
            gid += 1
        data["levels"].append(level_data)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


# --------------------------------------------------------------------------- #
#  Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only use first N stocks (test)")
    ap.add_argument("--html-only", action="store_true", help="rebuild outputs from cached prices only (no fetching)")
    ap.add_argument("--refresh", action="store_true",
                    help="(default behaviour now; kept for compatibility)")
    ap.add_argument("--fast", action="store_true",
                    help="incremental top-up only — quicker, but leaves historical "
                         "data un-revalidated and drifts the breadth oscillator")
    args = ap.parse_args()

    # One build at a time: run_daily.sh in a terminal + the website's Update
    # Prices button both write the caches — concurrent runs interleave JSON
    # writes and corrupt them. flock is released automatically on exit.
    try:
        import fcntl
        _lock_fh = open(BUILD_LOCK, "w")
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        sys.exit("Another build is already running (website button / terminal?). "
                 "Wait for it to finish and retry.")
    except ImportError:
        pass   # non-POSIX platform — skip the lock

    universe = read_universe()
    if args.limit:
        universe = universe[:args.limit]
    print(f"Universe: {len(universe)} stocks, "
          f"{len({u['macro'] for u in universe if u['macro']})} sectors, "
          f"{len({u['sector'] for u in universe if u['sector']})} industry groups, "
          f"{len({u['industry'] for u in universe if u['industry']})} industries, "
          f"{len({u['basic'] for u in universe if u['basic']})} sub-industries.")

    # ---- price cache (keyed by Yahoo symbol; benchmark under '__BENCH__') ----
    cache = load_json_cache(PRICE_CACHE)
    highs = load_json_cache(HIGH_CACHE)
    lows = load_json_cache(LOW_CACHE)
    vols = load_json_cache(VOL_CACHE)
    changes = read_symbol_changes()

    if not args.html_only:
        if args.fast:
            n_new = sum(1 for u in universe
                        if len(cache.get(u["sym"], {})) < MIN_BARS
                        or len(highs.get(u["sym"], {})) < MIN_BARS
                        or len(lows.get(u["sym"], {})) < MIN_BARS)
            print(f"Fetching from Yahoo ({BENCHMARK_NAME}={BENCH_YSYM} + {len(universe)} "
                  f"stocks): {n_new} full-history, {len(universe)-n_new} top-up "
                  f"(--fast: history not re-validated) ...")
        else:
            print(f"Fetching from Yahoo ({BENCHMARK_NAME}={BENCH_YSYM} + {len(universe)} "
                  f"stocks): full {FULL_RANGE} history for all (re-validates history) ...")
        # renamed symbols: also pull the old ticker for its pre-rename history
        olds = [{"sym": o} for o, _n in changes if not cache.get(o)]
        ok, miss = fetch_prices(universe + olds, cache, highs, lows, args.fast, vols)
        print(f"Fetched/cached {ok} series; {len(miss)} failures.")
        if miss[:10]:
            print("  sample failures:", miss[:10])

    if stitch_renamed((cache, highs, lows), changes):
        # persist, so the backend's per-stock charts see the full history too
        for path, obj in ((PRICE_CACHE, cache), (HIGH_CACHE, highs), (LOW_CACHE, lows)):
            write_json_atomic(path, obj)

    universe, other_classes = primary_share_classes(universe, cache, vols)
    if other_classes:
        print(f"Share classes: kept one per company — folded {len(other_classes)} "
              f"secondary class(es): "
              + ", ".join(f"{d}→{k}" for d, k in sorted(other_classes.items())[:12]))

    bench = cache.get("__BENCH__", {})
    if not bench:
        sys.exit("No benchmark data in cache. Run a full fetch (drop --html-only).")

    have = [u["sym"] for u in universe if cache.get(u["sym"])]
    thresh = max(1, int(0.5 * len(have)))

    # Repair null daily bars from intraday (Yahoo gap pattern) — fetch-based
    # runs only; --html-only must stay offline.  Provenance records which
    # dates came from something other than Yahoo's daily bars.
    prov = load_json_cache(PROVENANCE)
    if not args.html_only:
        _rep = repair_intraday_gap(cache, highs, lows, have, thresh, prov=prov)
        purge_flat_carries(cache, highs, lows, have)
    # Always persist the sidecar (empty = every bar came from Yahoo) so audit
    # checks and fresh runners see the file.
    write_json_atomic(PROVENANCE, prov)

    # Reference window = last WINDOW benchmark dates that ALSO have broad stock
    # coverage, so a benchmark day that leads the stock cache can't null out RS.
    bench_dates = sorted(bench.keys())

    # Fill benchmark gaps.  Yahoo occasionally misses a day for index symbols
    # while publishing individual stocks — synthetic fill from stock returns.
    _filled = fill_benchmark_gaps(bench, cache, have, thresh)
    if _filled:
        print(f"Filled {len(_filled)} missing benchmark date(s): "
              + ", ".join(f"{d} via {m}" for d, m in _filled))
        # Persist so fills survive future --html-only runs
        write_json_atomic(PRICE_CACHE, cache)
        for d, m in _filled:
            prov[d] = m
        write_json_atomic(PROVENANCE, prov)
    covered = [d for d in bench_dates
               if sum(1 for s in have if d in cache[s]) >= thresh]
    ref_dates = (covered or bench_dates)[-WINDOW:]
    if len(ref_dates) < WINDOW:
        print(f"Warning: only {len(ref_dates)} usable days with broad coverage.")
    if ref_dates and bench_dates[-1] != ref_dates[-1]:
        print(f"Note: window ends {ref_dates[-1]} — benchmark has data through "
              f"{bench_dates[-1]} but stocks don't yet; run with --refresh.")

    # Breadth oscillator uses ALL stock dates (not just benchmark dates), since
    # it only needs advancers/decliners.  Yahoo can occasionally miss a day for
    # the benchmark index while still publishing individual stocks.
    _all_stock_dates = set()
    for s in have:
        _all_stock_dates.update(cache[s].keys())
    breadth_dates = sorted(d for d in _all_stock_dates
                           if sum(1 for s in have if d in cache[s]) >= thresh)

    # ---- group members and compute RS for one classification level ----
    skipped_breaks = []
    below_min = set()      # last price under MIN_PRICE (config min_price)
    stock_cache = {}       # sym -> metrics (shared by all four levels)

    def stock_metrics(u, ser):
        """Full-window metrics, or — for a stock listed INSIDE the window with
        every session since — the same metrics over its days since listing,
        tagged "new" (= that day count).  None otherwise (gaps, < 2 days)."""
        sym = u["sym"]
        if sym not in stock_cache:
            dates, new = ref_dates, None
            first = min(ser)
            if first > ref_dates[0]:                     # listed inside the window
                dates = [d for d in ref_dates if d >= first]
                new = len(dates)
                if new < 2 or any(d not in ser for d in dates):
                    dates = None
            srs = equal_weight_rs([ser], bench, dates)[0] if dates else None
            st = None
            if srs is not None:
                brk = last_break_date(ser, 252)   # restart metrics after a demerger
                post = {d: v for d, v in ser.items() if d >= brk} if brk else ser
                ef, price = ema_flags(post)
                st = {"name": u["name"], "sym": sym, "exch": u.get("exch"),
                      "approx": u.get("approx", False),
                      "rs": srs, "pct": percentrank_inc(srs, srs[-1]),
                      "ema": ef, "ltp": price,
                      "rse": rs_ema_flag(post, bench),
                      "hi52": pct_off_high(highs.get(sym, {}), price, since=brk),
                      "adr": adr_pct(highs.get(sym, {}), lows.get(sym, {}), since=brk),
                      "new": new}
            stock_cache[sym] = st
        st = stock_cache[sym]
        return dict(st) if st else None    # copy: deltas are set per member row

    def compute(level):
        groups = defaultdict(list)
        for u in universe:
            g = u[level]
            if g:
                groups[g].append(u)
        out = []
        for gname, members in groups.items():
            series, stocks = [], []
            for u in members:
                ser = cache.get(u["sym"])
                if not ser:
                    continue
                # a corporate action inside the RS window would swamp the equal-weight
                # group index (a "+2000% day" is a demerger, not performance) — leave
                # such a stock out of the aggregate entirely.
                if last_break_date({d: ser[d] for d in ref_dates if d in ser},
                                   len(ref_dates)):
                    skipped_breaks.append(u["sym"])
                    continue
                st = stock_metrics(u, ser)
                if st is None:
                    continue
                if st["ltp"] is not None and st["ltp"] < MIN_PRICE:
                    below_min.add(u["sym"])          # penny stock: out of the view
                    continue
                stocks.append(st)
                # only stocks with a full window enter the group index — one
                # with gaps (or a fresh listing) would make the equal-weight
                # average jump as the constituent set changes; new listings
                # are listed in the group, ranked since their listing day
                if not st["new"]:
                    series.append(ser)
            if not series:
                continue
            rs, n = equal_weight_rs(series, bench, ref_dates)
            if rs is None:
                continue
            stocks.sort(key=lambda x: x["pct"], reverse=True)   # new listings by RS% too
            out.append({"name": gname, "n": n, "rs": rs,
                        "pct": percentrank_inc(rs, rs[-1]), "members": stocks})
        out.sort(key=lambda x: x["pct"], reverse=True)
        return out

    levels = [(key, label, compute(key)) for key, label in LEVELS]
    print("Computed RS for " +
          ", ".join(f"{len(items)} {label.lower()}" for _, label, items in levels) + ".")
    if below_min:
        print(f"Left out {len(below_min)} stock(s) priced under ${MIN_PRICE:g} (min_price).")
    if skipped_breaks:
        uniq = sorted(set(skipped_breaks))
        print(f"Excluded {len(uniq)} stock(s) from the group index — corporate action "
              f"inside the {WINDOW}-day RS window: {uniq[:12]}")

    # market-breadth oscillator over the full cached history
    breadth = None
    try:
        bdates, bosc, bn = compute_breadth(universe, cache, breadth_dates)
        macros = compute_macro_breadth(universe, cache, breadth_dates)
        breadth = {"dates": bdates, "osc": bosc, "n": bn, "macros": macros,
                   "divergence": detect_breadth_divergence(
                       cache.get("__BENCH__", {}), bdates, bosc)}
        if bosc:
            print(f"Breadth oscillator: {len(bosc)} days "
                  f"({bdates[0]} → {bdates[-1]}), latest {bosc[-1]:+.1f} "
                  f"({len(macros)} macro charts)")
        else:
            print("Breadth oscillator: not enough history (run --refresh for more).")
    except Exception as e:
        print("breadth calc failed:", e)

    # stocks priced under min_price are out of the view by design, not "excluded"
    n_stocks = sum(1 for u in universe
                   if cache.get(u["sym"]) and u["sym"] not in below_min)
    level_members = set()
    for _key, _label, items in levels:
        for it in items:
            for m in it["members"]:
                if not m["new"]:              # new listings stay on the IPO watch
                    level_members.add(m["sym"])
    n_window = len(level_members)
    excluded = sorted(u["sym"] for u in universe
                      if cache.get(u["sym"]) and u["sym"] not in level_members
                      and u["sym"] not in below_min)
    if excluded:
        print(f"Window coverage: {n_window}/{n_stocks} stocks. "
              f"{len(excluded)} not in the view (no full window data today): "
              f"{excluded[:12]}{' …' if len(excluded) > 12 else ''}")

    # ---- Phase 4: ranking archive + daily deltas vs the previous snapshot ----
    seed_from_rs_data(RS_DATA_JSON)        # archive behind the published file?
    window_end = ref_dates[-1] if ref_dates else ""
    # deltas vs the last EARLIER window — a same-day rebuild must compare with
    # yesterday again, not with today's own archived line
    prev_snap = load_previous_snapshot(before=window_end)
    append_snapshot(levels, window_end)
    if prev_snap:
        pg = prev_snap.get("g", {})
        pm = prev_snap.get("m", {})
        for key, _l, items in levels:
            for it in items:
                p = pg.get(key, {}).get(it["name"])
                it["dp"] = round((it["pct"] - p) * 100, 1) if p is not None else None
                for stock in it["members"]:
                    q = pm.get(stock["sym"])
                    stock["d"] = (round((stock["pct"] - q) * 100, 1)
                                  if q is not None else None)

    ipo = build_ipo_watch(universe, cache, set(excluded),
                          cache.get("__BENCH__", {}))
    rotation = compute_rotation(universe, cache, cache.get("__BENCH__", {}),
                                breadth_dates) if breadth_dates else \
        {"dates": [], "groups": []}
    if ipo:
        print(f"IPO watch: {len(ipo)} new listing(s) awaiting their first "
              f"{WINDOW}-day ranking: {[i['s'] for i in ipo[:8]]}")

    meta = {
        "benchmark": BENCHMARK_NAME,
        "drange": f"{ref_dates[0]} → {ref_dates[-1]}" if ref_dates else "n/a",
        "window_dates": ref_dates,
        "gen": dt.datetime.now(MKT_TZ).strftime("%Y-%m-%d %H:%M"),   # MKT_TZ on CI too
        "master_sha": hashlib.sha256(open(MASTER_CSV, "rb").read()).hexdigest(),
        "n_stocks": n_stocks,
        "n_window": n_window,
        "n_below_min": len(below_min),
        "share_classes": other_classes,      # {secondary class: primary kept}
        "excluded": excluded,
        "src": prov,
        "ipo": ipo,
    }
    save_rs_json(levels, breadth, meta, RS_DATA_JSON, rotation)
    print(f"Wrote {RS_DATA_JSON}")
    write_daily_report(os.path.join(HERE, "daily_report.html"),
                       meta, breadth, levels, ipo)
    print(f"Wrote {os.path.join(HERE, 'daily_report.html')}")


if __name__ == "__main__":
    main()
