#!/usr/bin/env python3
"""Daily data-integrity audit for the US RS pipeline.

Runs every forensic check that has caught real problems so far (corrupt
caches, Yahoo null-day gaps, fake holiday bars, stale builds) and prints a
PASS/FAIL report.  Exits non-zero on any FAIL so cron/CI can alert.

    python3 audit_data.py          # human report
    python3 audit_data.py --json   # machine-readable
"""
import os
import csv
import sys
import json
import datetime as dt

import build_rs as rs
from rs_engine.config import (latest_session, load_holidays, CURRENT_MACROS,
                              standard_company)

ROOT = rs.HERE
LEVELS = ("sector", "industryGroup", "industry", "subIndustry")


def check_caches():
    """Caches must parse as JSON, with no half-written .tmp leftovers."""
    issues = []
    for path in (rs.PRICE_CACHE, rs.HIGH_CACHE, rs.LOW_CACHE, rs.VOL_CACHE,
                 rs.PROVENANCE, rs.RS_DATA_JSON):
        name = os.path.basename(path)
        if not os.path.exists(path):
            issues.append(f"{name}: missing")
            continue
        try:
            with open(path) as fh:
                json.load(fh)
        except Exception as e:
            issues.append(f"{name}: CORRUPT ({type(e).__name__})")
    for path in (rs.PRICE_CACHE, rs.HIGH_CACHE, rs.LOW_CACHE, rs.PROVENANCE):
        if os.path.exists(path + ".tmp"):
            issues.append(f"{os.path.basename(path)}.tmp: leftover partial write")
    return issues


def _have(cache, symbols=None):
    """Symbols with price data — limited to `symbols` (the current universe)
    when given, so delisted / pre-rename tickers lingering in the cache don't
    inflate the coverage thresholds."""
    pool = symbols if symbols is not None else (s for s in cache if s != "__BENCH__")
    return [s for s in pool if cache.get(s)]


def check_benchmark_calendar(cache, symbols=None):
    """Every weekday between the first and last benchmark date must either be
    a trading day in the benchmark or an official NYSE holiday."""
    bench = cache.get("__BENCH__", {})
    if not bench:
        return ["benchmark: empty"]
    have = _have(cache, symbols)
    thresh = max(1, int(0.5 * len(have)))
    dates = sorted(bench)
    start, end = dt.date.fromisoformat(dates[0]), dt.date.fromisoformat(dates[-1])
    bench_set = set(dates)
    issues = []
    d = start
    while d <= end:
        iso = d.isoformat()
        if d.weekday() < 5:
            cov = sum(1 for s in have if iso in cache[s])
            if iso not in bench_set and not rs.is_holiday(iso):
                if cov >= thresh:
                    issues.append(f"{iso}: MISSING from benchmark but "
                                  f"{cov}/{len(have)} stocks have data")
            elif iso in bench_set and rs.is_holiday(iso):
                if cov >= thresh * 0.5:
                    issues.append(f"{iso}: in benchmark but it's an NYSE holiday")
        d += dt.timedelta(days=1)
    return issues


def check_flat_days(cache, symbols=None):
    """Yahoo's fake holiday/outage bars: >90% of stock closes equal to the
    previous close.  Such dates must not sit in the benchmark."""
    have = _have(cache, symbols)
    bench = cache.get("__BENCH__", {})
    n = len(have)
    cov = {}
    for s in have:
        for day in cache[s]:
            cov[day] = cov.get(day, 0) + 1
    issues = []
    fake = rs.fake_flat_days(cache, have)
    for day in sorted(cov):
        if day in fake and cov[day] > 0.5 * n:
            if day in bench:
                issues.append(f"{day}: fake flat bars IN the benchmark")
            else:
                issues.append(f"{day}: fake flat bars in stock caches "
                              f"({cov[day]} stocks) — consider purging")
    return issues


def check_freshness():
    """The build should not be more than a few weekdays stale."""
    try:
        with open(rs.RS_DATA_JSON) as f:
            meta = json.load(f).get("meta", {})
    except Exception:
        return ["rs_data.json: unreadable"]
    gen = meta.get("gen", "")
    if not gen:
        return ["rs_data.json: no generation timestamp"]
    gen_dt = dt.datetime.strptime(gen, "%Y-%m-%d %H:%M")
    age = dt.datetime.now(rs.MKT_TZ).replace(tzinfo=None) - gen_dt   # gen is NY time
    if age > dt.timedelta(days=7):
        return [f"rs_data.json generated {age.days} days ago — build is stale"]
    return []


def check_reconciliation(rs_data):
    """meta.n_stocks ≥ n_window == union of RANKED members (new listings are
    shown in the groups but still counted on the IPO watch); excluded must
    reconcile."""
    meta = rs_data.get("meta", {})
    union = {m["s"] for lv in rs_data.get("levels", [])
             for g in lv.get("groups", []) for m in g.get("members", [])
             if not m.get("nw")}
    issues = []
    if len(union) != meta.get("n_window"):
        issues.append(f"n_window={meta.get('n_window')} but member union is "
                      f"{len(union)}")
    n_stocks, n_window = meta.get("n_stocks", 0), meta.get("n_window", 0)
    excluded = meta.get("excluded", [])
    if not (0 < n_window <= n_stocks):
        issues.append(f"bad counts: n_stocks={n_stocks}, n_window={n_window}")
    if len(excluded) != n_stocks - n_window:
        issues.append(f"excluded={len(excluded)} does not reconcile with "
                      f"n_stocks-n_window={n_stocks - n_window}")
    return issues


def check_classification():
    """Every classified master row must be a real path in gics_hierarchy.csv
    (sector > industry group > industry > sub-industry, exact spelling) — a
    typo in an override or a stale value becomes a phantom 1-stock group —
    and company names must be in the standard spelling."""
    issues = []
    with open(os.path.join(ROOT, "gics_hierarchy.csv"), newline="", encoding="utf-8") as fh:
        paths = {tuple(r[k] for k in LEVELS) for r in csv.DictReader(fh)}
    if {p[0] for p in paths} != CURRENT_MACROS:
        issues.append("gics_hierarchy.csv: sectors differ from the 11 GICS sectors")
    with open(rs.MASTER_CSV, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            path = tuple(r[k] for k in LEVELS)
            if any(path) and path not in paths:
                issues.append(f"{r['symbol']}: not a GICS path {' > '.join(path)!r}")
            if r["company"] != standard_company(r["company"]):
                issues.append(f"{r['symbol']}: non-standard company {r['company']!r}")
    return issues


def check_holiday_calendar(today=None):
    """From December on, us_holidays.csv must already hold next year's
    holidays — otherwise every January holiday reads as a stale build (hourly
    rebuilds + alerts).  The NYSE publishes its calendar years ahead
    (nyse.com/markets/hours-calendars); add the rows by hand."""
    today = today or dt.datetime.now(rs.MKT_TZ).date()
    nxt = str(today.year + 1)
    if today.month == 12 and not any(d.startswith(nxt) for d in load_holidays()):
        return [f"us_holidays.csv has no {nxt} holidays yet"]
    return []


def check_data_covers_latest_session():
    """'Stale' = a trading session has ended but the price cache has no bar for
    it (Yahoo published late).  Returns issues; empty means data is current."""
    try:
        with open(rs.PRICE_CACHE) as fh:
            bench = json.load(fh).get("__BENCH__", {})
    except Exception:
        return ["price cache unreadable"]
    if not bench:
        return ["price cache empty"]
    # before today's data settles (~17:00 NY) the PREVIOUS session must be present
    expected = latest_session(dt.datetime.now(rs.MKT_TZ))
    if max(bench) < expected:
        return [f"stale: data ends {max(bench)} but the latest session is "
                f"{expected} (Yahoo published late?)"]
    return []


def main():
    as_json = "--json" in sys.argv
    if "--stale-only" in sys.argv:
        issues = check_data_covers_latest_session()
        if not as_json:
            print("current — data covers the latest session" if not issues
                  else issues[0])
        sys.exit(1 if issues else 0)
    report = {}
    report["caches"] = check_caches()

    cache = {}
    try:
        with open(rs.PRICE_CACHE) as fh:
            cache = json.load(fh)
    except Exception:
        cache = {}

    if cache:
        symbols = [u["sym"] for u in rs.read_universe()]
        report["benchmark_calendar"] = check_benchmark_calendar(cache, symbols)
        report["flat_days"] = check_flat_days(cache, symbols)
    else:
        report["benchmark_calendar"] = ["price cache unreadable"]
        report["flat_days"] = ["price cache unreadable"]

    report["freshness"] = check_freshness()
    report["holiday_calendar"] = check_holiday_calendar()
    report["classification"] = check_classification()
    report["latest_session"] = check_data_covers_latest_session()

    try:
        with open(rs.RS_DATA_JSON) as fh:
            report["reconciliation"] = check_reconciliation(json.load(fh))
    except Exception:
        report["reconciliation"] = ["rs_data.json unreadable"]

    failed = sum(1 for v in report.values() if v)
    if as_json:
        print(json.dumps({"ok": failed == 0, "checks": report}, indent=1))
    else:
        for name, issues in report.items():
            status = "FAIL" if issues else "PASS"
            print(f"[{status}] {name}")
            for msg in issues:
                print(f"       - {msg}")
        print()
        print("ALL CHECKS PASSED" if failed == 0 else f"{failed} CHECK(S) FAILED")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
