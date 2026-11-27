"""Priced US IPOs from Nasdaq's IPO calendar → us_ipos.csv.

A stock's first session in the price data is not enough to call it an IPO:
direct listings, spin-offs and uplistings from OTC also start trading cold.
Nasdaq's IPO calendar lists the real ones with their issue price.  `listed`
is the pricing date — the first trade is usually the next session.

    us_ipos.csv   symbol,listed,price      (ISO date; issue price in US$)
"""
import csv
import datetime as dt
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IPOS = os.path.join(ROOT, "us_ipos.csv")
NASDAQ_URL = "https://api.nasdaq.com/api/ipo/calendar?date={ym}"


def _date(s):
    for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime((s or "").strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _price(s):
    try:
        v = float(str(s).replace(",", "").replace("$", "").strip())
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def parse_nasdaq(rows):
    """Nasdaq IPO-calendar "priced" rows → {SYMBOL: {"listed": iso, "price": float|None}}."""
    out = {}
    for x in rows or []:
        sym = (x.get("proposedTickerSymbol") or "").strip().upper().replace(".", "-")
        listed = _date(x.get("pricedDate"))
        if sym and listed and (x.get("dealStatus") or "Priced") == "Priced":
            out[sym] = {"listed": listed, "price": _price(x.get("proposedSharePrice"))}
    return out


def read(path=None):
    path = path or IPOS
    if not os.path.exists(path):
        return {}
    with open(path, newline="", encoding="utf-8") as f:
        return {r["symbol"]: {"listed": r["listed"], "price": _price(r.get("price"))}
                for r in csv.DictReader(f) if r.get("symbol") and r.get("listed")}


def write(ipos, path=None):
    path = path or IPOS
    tmp = path + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "listed", "price"])
        for s in sorted(ipos, key=lambda s: (ipos[s]["listed"], s)):
            p = ipos[s]["price"]
            w.writerow([s, ipos[s]["listed"], "" if p is None else f"{p:g}"])
    os.replace(tmp, path)


def merge(old, new):
    """New rows win; old ones are kept (each run only fetches recent months)."""
    return {**old, **new}
