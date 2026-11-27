"""Upcoming earnings dates from Nasdaq's earnings calendar → us_events.csv.
The build tags each stock with its next event so the Swing page can warn
before a position is held through results.
"""
import csv
import datetime as dt
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVENTS = os.path.join(ROOT, "us_events.csv")
NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings?date={d}"
AHEAD_DAYS = 14          # calendar days a stock is flagged ahead of its event
TIMING = {"time-pre-market": "Results (before open)", "time-after-hours": "Results (after close)"}


def parse_nasdaq(day, rows):
    """Nasdaq earnings-calendar rows for `day` (YYYY-MM-DD) →
    {(yahoo_symbol, day): purpose}.  BRK.B-style symbols become BRK-B."""
    out = {}
    for x in rows or []:
        sym = (x.get("symbol") or "").strip().upper().replace(".", "-")
        if sym:
            out[(sym, day)] = TIMING.get(x.get("time"), "Results")
    return out


def read_events(path=None):
    path = path or EVENTS
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {(r["symbol"], r["date"]): r["purpose"] for r in csv.DictReader(f)}


def write_events(ev, path=None):
    path = path or EVENTS
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "date", "purpose"])
        for (s, d), p in sorted(ev.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            w.writerow([s, d, p])
    os.replace(tmp, path)


def merge(old, new, today):
    """Keep events from a week back on (recent results explain a gap move);
    the fresh fetch replaces everything from today on."""
    keep_from = (dt.date.fromisoformat(today) - dt.timedelta(days=7)).isoformat()
    out = {k: v for k, v in old.items() if keep_from <= k[1] < today}
    out.update({k: v for k, v in new.items() if k[1] >= keep_from})
    return out


def next_events(ev, asof, ahead=AHEAD_DAYS):
    """{symbol: "YYYY-MM-DD Purpose"} for the first event after `asof` (the
    last close) within `ahead` calendar days."""
    end = (dt.date.fromisoformat(asof) + dt.timedelta(days=ahead)).isoformat()
    out = {}
    for (s, d), p in sorted(ev.items(), key=lambda kv: kv[0][1]):
        if asof < d <= end and s not in out:
            out[s] = f"{d} {p}"
    return out


def is_results(tag):
    return bool(tag) and bool(re.search(r"\bResults\b", tag))
