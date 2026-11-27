#!/usr/bin/env python3
"""Update us_events.csv with Nasdaq's earnings calendar for the next 14 days,
and us_ipos.csv with Nasdaq's priced IPOs (the last 12 months on the first
run, then the current and previous month).  Best effort: keeps the files on
failure.  See rs_engine/events.py and rs_engine/ipos.py."""
import datetime as dt
import sys
import time

import requests

from rs_engine import events, ipos
from rs_engine.config import MKT_TZ

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def _get(s, url):
    try:
        r = s.get(url, timeout=30)
        return (r.json() or {}).get("data") if r.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None


def refresh_earnings(s, today):
    new, ok = {}, 0
    for k in range(events.AHEAD_DAYS + 1):
        day = today + dt.timedelta(days=k)
        if day.weekday() >= 5:
            continue
        data = _get(s, events.NASDAQ_URL.format(d=day.isoformat()))
        if data is not None:
            ok += 1
            new.update(events.parse_nasdaq(day.isoformat(), data.get("rows")))
        time.sleep(0.3)
    if not ok:
        print("Nasdaq earnings calendar: unavailable — us_events.csv unchanged.")
        return
    ev = events.merge(events.read_events(), new, today.isoformat())
    events.write_events(ev)
    print(f"Events: {len(new)} upcoming earnings dates → {events.EVENTS}")


def refresh_ipos(s, today):
    old = ipos.read()
    months = 12 if not old else 2
    new = {}
    for k in range(months):
        y, m = today.year, today.month - k
        while m <= 0:
            y, m = y - 1, m + 12
        data = _get(s, ipos.NASDAQ_URL.format(ym=f"{y}-{m:02d}"))
        if data:
            new.update(ipos.parse_nasdaq((data.get("priced") or {}).get("rows")))
        time.sleep(0.3)
    if not new:
        print("Nasdaq IPO calendar: unavailable — us_ipos.csv unchanged.")
        return
    allx = ipos.merge(old, new)
    ipos.write(allx)
    print(f"IPOs: {len(new)} priced issues from Nasdaq ({len(allx)} kept) → {ipos.IPOS}")


def main():
    today = dt.datetime.now(MKT_TZ).date()
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/json"})
    refresh_earnings(s, today)
    refresh_ipos(s, today)
    return 0


if __name__ == "__main__":
    sys.exit(main())
