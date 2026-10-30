#!/usr/bin/env python3
"""
Rebuild the US universe + GICS-style 4-level classification (us_stock_master.csv)
from free sources only — no browser, no API keys, no geo-blocks.

What it does:
  1. Downloads NASDAQ Trader's symbol directories (nasdaqlisted.txt +
     otherlisted.txt) and keeps the common stocks / ADRs / MLP units listed on
     NASDAQ, NYSE, NYSE American (+ the few on NYSE Arca, Cboe and TXSE) — ETFs, test issues, warrants, rights,
     SPAC units, preferreds, notes and closed-end funds are dropped.
  2. Adds each company's SEC CIK (company_tickers_exchange.json) — the stable
     id used to detect renames (CIK unchanged, ticker changed).
  3. Classifies every stock, first match wins:
       a. us_classification_overrides.csv (hand-checked fixes)
       b. S&P 500 / 400 / 600 constituents on Wikipedia: real GICS
          Sub-Industry → Sector / Industry Group / Industry via gics_hierarchy.csv
       c. Yahoo sector/industry (assetProfile, cached) mapped to a GICS
          Sub-Industry through yahoo_to_gics.csv.  The 'auto' rows of that file
          are LEARNT each run from the S&P 1500 (most common GICS Sub-Industry
          among the S&P stocks sharing a Yahoo industry); 'manual' rows win.
     Stocks still unclassified stay in the master as stubs (the build prices
     them and the New Listings tab shows them).
  4. Renames (same CIK, new ticker) → us_symbol_changes.csv, so build_rs.py can
     stitch the old ticker's history on; symbols that left the exchanges are
     archived to us_delisted.csv.

Usage:
    python3 refresh_universe.py               # full refresh (fetches missing Yahoo profiles)
    python3 refresh_universe.py --dry-run     # report counts, change nothing
    python3 refresh_universe.py --no-profiles # use cached Yahoo profiles only
"""

import os
import re
import csv
import html
import json
import time
import random
import argparse
import datetime as dt
import threading
import concurrent.futures as cf
from collections import Counter, defaultdict

import requests

from rs_engine.config import CACHE_DIR, CURRENT_MACROS, MKT_TZ, standard_company

HERE = os.path.dirname(os.path.abspath(__file__))
MASTER = os.path.join(HERE, "us_stock_master.csv")
CHANGES = os.path.join(HERE, "us_symbol_changes.csv")    # old,new,cik,date
DELISTED = os.path.join(HERE, "us_delisted.csv")         # archived master rows
OVERRIDES = os.path.join(HERE, "us_classification_overrides.csv")  # hand-checked fixes
HIERARCHY = os.path.join(HERE, "gics_hierarchy.csv")     # the GICS 2023 tree
YAHOO_MAP = os.path.join(HERE, "yahoo_to_gics.csv")      # yahoo industry → sub-industry
PROFILE_CACHE = os.path.join(CACHE_DIR, ".yh_profile_cache.json")

LEVEL_FIELDS = ["sector", "industryGroup", "industry", "subIndustry"]
FIELDS = ["symbol", "company", "exchange", "cik"] + LEVEL_FIELDS + ["source"]

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"
SEC_TICKERS = "https://www.sec.gov/files/company_tickers_exchange.json"
WIKI = "https://en.wikipedia.org/wiki/List_of_S%26P_{n}_companies"
YH_PROFILE = "https://query2.finance.yahoo.com/v10/finance/quoteSummary/{sym}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
# The SEC rejects requests without a "name email" User-Agent; set your own.
SEC_UA = os.environ.get("US_RS_SEC_UA", "us-relative-strength admin@example.com")

# otherlisted.txt exchange codes → TradingView prefix (NYSE American = AMEX).
# Cboe BZX / TXSE / IEX list a handful of real companies (CBOE, TCBI, IBOC …).
OTHER_EXCHANGES = {"N": "NYSE", "A": "AMEX", "P": "AMEX", "Z": "CBOE",
                   "TXSE": "TXSE", "V": "IEX"}
PROFILE_RETRY_DAYS = 3       # re-ask Yahoo for a symbol it had no profile for (IPOs)
PROFILE_REFRESH_DAYS = 90    # re-validate known profiles (reclassifications)
PROFILE_REFRESH_CAP = 300    # ... at most this many per run

# Non-common-stock securities, by name.  ADRs are checked first (their names
# mention "Units"/"Series" of the underlying share); MLP "Common Units" are real
# equity and stay.
_ADR = re.compile(r"american deposita?ry", re.I)
# 'preferred' only as a security type — "Preferred Bank" is a common stock
_PREFERRED = re.compile(r"\bpreferred\s+(stock|shares?|securities)\b|\bpreference\s+shares?\b"
                        r"|\bseries\s+[a-z0-9-]+\b.*\bpreferred\b|\bdepositary\s+shares?\b", re.I)
_DROP_NAME = re.compile(
    r"\bwarrants?\b|\brights?\b|\bnotes?\b|\bdebentures?\b|\bbonds?\b"
    r"|\d\s*%|(?<!common )\bunits?\b|\bfund\b|\bmunicipal\b|\bincome trust\b|\bterm trust\b"
    r"|\bacquisition (corp|corporation|company|co|limited|ltd)\b|\bspac\b"
    # structured products / trust preferreds that list as "shares"
    r"|\bstrats\b|\bzones\b|\bcapital trust\b|\bcertificates?\b|\bctf\b"
    r"|\bfixed-income securities\b|\bpplus\b|\bdue (19|20)\d\d\b", re.I)
_DROP_SUFFIX = {"U", "W", "WS", "WI", "R", "RT", "WD"}    # .U units, .WS warrants …
# trailing security description in otherlisted names ("Acme, Inc. Common Stock")
_SECURITY_DESC = re.compile(
    r"\s+(?:New\s+)?(?:(?:Class|Series)\s+[A-Z0-9]+\s+)?"
    r"(?:Common\s+Stock|Common\s+Shares|Ordinary\s+Shares|Common\s+Units|"
    r"Subordinate\s+Voting\s+Shares|Shares\s+of\s+Beneficial\s+Interest|"
    r"American\s+Deposita?ry\s+Shares|Limited\s+Partnership\s+Units|Units)\b.*$", re.I)
_DROP_YAHOO_INDUSTRY = {"Shell Companies"}        # SPACs
# Closed-end funds look like stocks everywhere (listing files, Yahoo quoteType
# EQUITY, industry "Asset Management"); only Yahoo's business summary says
# "closed-end fund".  BDCs use the same words but are operating companies.
_CEF = re.compile(r"\bclosed?[- ]?end(ed)?\b|\bmutual fund\b", re.I)
_BDC = re.compile(r"business development compan|\bBDC\b", re.I)


def _today():
    return dt.datetime.now(MKT_TZ).date().isoformat()


# --------------------------------------------------------------------------- #
#  Listings (NASDAQ Trader symbol directories)
# --------------------------------------------------------------------------- #
def clean_company(name):
    """'Apple Inc. - Common Stock' → 'Apple Inc.';
    'Agilent Technologies, Inc. Common Stock' → 'Agilent Technologies, Inc.'"""
    name = " ".join((name or "").split())
    if " - " in name:
        name = name.split(" - ")[0]
    name = _SECURITY_DESC.sub("", name).strip(" ,")
    return standard_company(name)


def is_common_equity(name):
    """True for common stock, ordinary shares, ADRs and MLP common units.
    NASDAQ types every BDC "- Closed End Fund": that label is left to the
    Yahoo summary check (is_cef), which tells BDCs from real funds."""
    name = re.sub(r"\s+-\s+closed end fund\s*$", "", name, flags=re.I)
    if _ADR.search(name):           # ADR wording mentions units/series of the share
        return not re.search(r"\bpreferred\b|\bpreference\b|\bwarrants?\b|\brights?\b",
                             name, re.I)
    return not (_PREFERRED.search(name) or _DROP_NAME.search(name))


def yahoo_symbol(act_symbol):
    """Exchange symbol → Yahoo symbol ('BRK.B' → 'BRK-B'), or None for a
    preferred ('ABR$D') or a unit/warrant/rights/when-issued line."""
    if "$" in act_symbol or "^" in act_symbol:
        return None
    base, _, suffix = act_symbol.partition(".")
    if suffix in _DROP_SUFFIX:
        return None
    return f"{base}-{suffix}" if suffix else base


def _pipe_rows(text):
    lines = [ln for ln in text.splitlines()
             if ln.strip() and not ln.startswith("File Creation Time")]
    return list(csv.DictReader(lines, delimiter="|"))


def parse_listings(nasdaq_text, other_text):
    """{yahoo_symbol: {symbol, company, exchange}} for the common equity on
    NASDAQ / NYSE / NYSE American.  Pure function (tested offline)."""
    out = {}
    for r in _pipe_rows(nasdaq_text):
        if r.get("ETF") == "Y" or r.get("Test Issue") == "Y" or r.get("NextShares") == "Y":
            continue
        name = r.get("Security Name") or ""
        sym = yahoo_symbol((r.get("Symbol") or "").strip())
        if sym and is_common_equity(name):
            out[sym] = {"symbol": sym, "company": clean_company(name), "exchange": "NASDAQ"}
    for r in _pipe_rows(other_text):
        exch = OTHER_EXCHANGES.get(r.get("Exchange"))
        if not exch or r.get("ETF") == "Y" or r.get("Test Issue") == "Y":
            continue
        name = r.get("Security Name") or ""
        sym = yahoo_symbol((r.get("ACT Symbol") or "").strip())
        if sym and is_common_equity(name):
            out[sym] = {"symbol": sym, "company": clean_company(name), "exchange": exch}
    return out


def fetch_listings():
    texts = []
    for url in (NASDAQ_LISTED, OTHER_LISTED):
        r = requests.get(url, headers={"User-Agent": UA}, timeout=60)
        r.raise_for_status()
        texts.append(r.text)
    return parse_listings(*texts)


def fetch_ciks():
    """{yahoo_symbol: cik} from the SEC ticker file ('BRK-B' style already)."""
    try:
        r = requests.get(SEC_TICKERS, headers={"User-Agent": SEC_UA}, timeout=60)
        r.raise_for_status()
        j = r.json()
        i_cik, i_tk = j["fields"].index("cik"), j["fields"].index("ticker")
        return {row[i_tk].upper(): str(row[i_cik]) for row in j["data"] if row[i_tk]}
    except (requests.RequestException, ValueError, KeyError) as e:
        print(f"SEC ticker file unavailable ({type(e).__name__}) — CIKs kept from the master.")
        return {}


# --------------------------------------------------------------------------- #
#  GICS: hierarchy, S&P 1500 constituents, overrides
# --------------------------------------------------------------------------- #
def load_hierarchy():
    """{subIndustry: {sector, industryGroup, industry, subIndustry}}"""
    with open(HIERARCHY, newline="", encoding="utf-8") as f:
        return {r["subIndustry"]: {k: r[k] for k in LEVEL_FIELDS} for r in csv.DictReader(f)}


_TAG = re.compile(r"<(?:[^>\"']|\"[^\"]*\"|'[^']*')*>")   # a tag, '>' inside quotes allowed


def parse_wiki_constituents(page_html):
    """[(yahoo_symbol, gics_sub_industry)] from a 'List of S&P N companies'
    page (table id="constituents": Symbol, Security, GICS Sector, GICS
    Sub-Industry, …).  Wikipedia's sector column is ignored: it lags GICS
    reclassifications (e.g. Mortgage REITs), the tree is authoritative."""
    m = re.search(r'<table[^>]*id="constituents".*?</table>', page_html, re.S)
    if not m:
        return []
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(0), re.S):
        cells = [html.unescape(_TAG.sub("", re.sub(r"<!--.*?-->", "", c, flags=re.S))).strip()
                 for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) >= 4 and cells[0]:
            out.append((cells[0].upper().replace(".", "-"), cells[3]))
    return out


def fetch_sp1500(tree):
    """{yahoo_symbol: subIndustry} for the S&P 500 + 400 + 600."""
    out = {}
    for n in (500, 400, 600):
        try:
            r = requests.get(WIKI.format(n=n), headers={"User-Agent": UA}, timeout=60)
            r.raise_for_status()
        except requests.RequestException as e:
            print(f"S&P {n} list unavailable ({type(e).__name__}) — skipped.")
            continue
        rows = parse_wiki_constituents(r.text)
        unknown = sorted({sub for _s, sub in rows if sub not in tree})
        if unknown:
            print(f"S&P {n}: sub-industries missing from gics_hierarchy.csv: {unknown}")
        out.update({s: sub for s, sub in rows if sub in tree})
        print(f"S&P {n}: {len(rows)} constituents")
    return out


def load_overrides():
    """{symbol: subIndustry} hand fixes — the other levels follow from the
    tree; the value EXCLUDE drops the symbol from the universe (debt or other
    non-equity listed as "Common Stock")."""
    if not os.path.exists(OVERRIDES):
        return {}
    with open(OVERRIDES, newline="", encoding="utf-8") as f:
        return {r["symbol"].strip().upper(): r["subIndustry"].strip()
                for r in csv.DictReader(f) if r.get("symbol") and r.get("subIndustry")}


# --------------------------------------------------------------------------- #
#  Yahoo assetProfile (sector / industry), cached
# --------------------------------------------------------------------------- #
_tls = threading.local()


def _yahoo_session(reset=False):
    """Per-thread Yahoo session holding the cookie + crumb quoteSummary needs."""
    s = getattr(_tls, "s", None)
    if s is None or reset:
        s = requests.Session()
        s.headers["User-Agent"] = UA
        try:
            s.get("https://fc.yahoo.com", timeout=20)      # sets the A3 cookie (404 is fine)
        except requests.RequestException:
            pass
        s.crumb = s.get("https://query2.finance.yahoo.com/v1/test/getcrumb",
                        timeout=20).text.strip()
        _tls.s = s
    return s


def yahoo_profile(sym):
    """{'sector','industry','qt','summary'} for one symbol; qt None when Yahoo has no
    such quote.  Raises on persistent failure (the symbol is retried next run)."""
    for attempt in range(5):
        try:
            s = _yahoo_session(reset=attempt > 0 and attempt % 2 == 0)
            r = s.get(YH_PROFILE.format(sym=requests.utils.quote(sym)),
                      params={"modules": "assetProfile,quoteType", "crumb": s.crumb},
                      timeout=25)
        except requests.RequestException:
            time.sleep(1 + attempt + random.random())
            continue
        if r.status_code == 404:
            return {"sector": "", "industry": "", "qt": None}
        if r.status_code in (401, 429) or r.status_code >= 500:
            time.sleep(2 * (attempt + 1) + random.random())
            _tls.s = None if r.status_code == 401 else _tls.s     # stale crumb
            continue
        res = ((r.json().get("quoteSummary") or {}).get("result") or [{}])[0]
        ap, qt = res.get("assetProfile") or {}, res.get("quoteType") or {}
        return {"sector": ap.get("sector") or "", "industry": ap.get("industry") or "",
                "qt": qt.get("quoteType"),
                # opening of the business summary — enough to tell a fund (see is_cef)
                "summary": (ap.get("longBusinessSummary") or "")[:300]}
    raise RuntimeError(f"Yahoo profile failed for {sym}")


def profiles_to_fetch(symbols, cache, today=None):
    """Symbols whose cached profile is missing, a recent miss to retry, or old
    enough to re-validate (capped per run)."""
    today = dt.date.fromisoformat(today or _today())
    missing, stale = [], []
    for s in symbols:
        p = cache.get(s)
        if not p:
            missing.append(s)
            continue
        age = (today - dt.date.fromisoformat(p.get("t", "1970-01-01"))).days
        if not p.get("industry") and age >= PROFILE_RETRY_DAYS:
            missing.append(s)
        elif "summary" not in p and p.get("industry") == "Asset Management":
            missing.append(s)                  # cached before summaries were kept
        elif age >= PROFILE_REFRESH_DAYS:
            stale.append(s)
    stale.sort(key=lambda s: cache[s].get("t", ""))
    return missing + stale[:PROFILE_REFRESH_CAP]


def fetch_profiles(symbols, cache, workers=4):
    todo = profiles_to_fetch(symbols, cache)
    if not todo:
        return 0
    print(f"Yahoo profiles: fetching {len(todo)} ...")
    today, done, failed = _today(), 0, 0

    def work(sym):
        try:
            return sym, yahoo_profile(sym)
        except Exception:
            return sym, None

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for sym, prof in ex.map(work, todo):
            done += 1
            if prof is None:
                failed += 1
            else:
                cache[sym] = dict(prof, t=today)
            if done % 250 == 0:
                print(f"  {done}/{len(todo)} profiles ...")
                _write_json(PROFILE_CACHE, cache)
    _write_json(PROFILE_CACHE, cache)
    if failed:
        print(f"  {failed} profile(s) failed — retried next run")
    return done - failed


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def _load_json(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


# --------------------------------------------------------------------------- #
#  Yahoo industry → GICS sub-industry mapping
# --------------------------------------------------------------------------- #
def load_yahoo_map():
    """{yahoo_industry: row} from yahoo_to_gics.csv (source = manual | auto)."""
    if not os.path.exists(YAHOO_MAP):
        return {}
    with open(YAHOO_MAP, newline="", encoding="utf-8") as f:
        return {r["yahoo_industry"]: r for r in csv.DictReader(f) if r.get("yahoo_industry")}


def learn_yahoo_map(sp, profiles, current):
    """Rebuild the 'auto' rows: for every Yahoo industry, the most common GICS
    sub-industry among S&P 1500 stocks carrying it.  'manual' rows are kept
    untouched (they always win).  Returns the new {yahoo_industry: row}."""
    votes = defaultdict(Counter)
    for sym, sub in sp.items():
        ind = (profiles.get(sym) or {}).get("industry")
        if ind and ind not in _DROP_YAHOO_INDUSTRY:
            votes[ind][sub] += 1
    out = {k: v for k, v in current.items() if v.get("source") == "manual"}
    for ind, c in votes.items():
        if ind in out:
            continue
        sub, n = c.most_common(1)[0]
        out[ind] = {"yahoo_industry": ind, "subIndustry": sub, "source": "auto",
                    "votes": f"{n}/{sum(c.values())}"}
    # Yahoo industries seen in the universe with no S&P evidence and no manual
    # row: listed with an empty sub-industry so they are easy to fill in by hand
    for p in profiles.values():
        ind = p.get("industry")
        if ind and ind not in out and ind not in _DROP_YAHOO_INDUSTRY:
            out[ind] = {"yahoo_industry": ind, "subIndustry": "", "source": "auto",
                        "votes": "0/0"}
    return out


def save_yahoo_map(rows):
    tmp = YAHOO_MAP + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["yahoo_industry", "subIndustry", "source", "votes"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(rows.values(), key=lambda r: r["yahoo_industry"]))
    os.replace(tmp, YAHOO_MAP)


# --------------------------------------------------------------------------- #
#  Classification + master
# --------------------------------------------------------------------------- #
def is_excluded(sym, profile, sp, overrides=None):
    """Securities that look like common stock in the listing files but aren't
    operating companies: Yahoo ETFs/funds, closed-end funds, SPAC shells, and
    symbols marked EXCLUDE in the overrides (S&P members never)."""
    if (overrides or {}).get(sym) == "EXCLUDE":
        return True
    if sym in sp or not profile:
        return False
    if profile.get("qt") and profile["qt"] != "EQUITY":
        return True
    return is_cef(profile) or profile.get("industry") in _DROP_YAHOO_INDUSTRY


def is_cef(profile):
    summary = (profile or {}).get("summary") or ""
    return bool(_CEF.search(summary)) and not _BDC.search(summary)


def classify(sym, tree, overrides, sp, profiles, ymap):
    """(levels dict, source) — source in override | sp1500 | yahoo | ''."""
    sub = overrides.get(sym)
    if sub in tree:
        return tree[sub], "override"
    sub = sp.get(sym)
    if sub in tree:
        return tree[sub], "sp1500"
    ind = (profiles.get(sym) or {}).get("industry")
    sub = (ymap.get(ind) or {}).get("subIndustry") if ind else None
    if sub in tree:
        return tree[sub], "yahoo"
    return {k: "" for k in LEVEL_FIELDS}, ""


def load_master_rows():
    if not os.path.exists(MASTER):
        return []
    with open(MASTER, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def reconcile(master, live, ciks, dry_run=False):
    """Old master rows whose symbol left the listing files:
      * renamed — its CIK now trades under a symbol that was NOT already in the
        master (dual-class companies share a CIK, so a class going away is not
        a rename): logged to us_symbol_changes.csv for history stitching.
      * gone — archived to us_delisted.csv.
    Skipped when the live list looks truncated.  Returns the renames list."""
    if not master:
        return []
    if len(live) < 0.9 * len(master):
        print(f"Live list has {len(live)} symbols vs master {len(master)} — "
              f"looks truncated, skipping rename/delisting reconcile.")
        return []
    before = {r["symbol"] for r in master}
    by_cik = defaultdict(list)
    for s in live:
        if ciks.get(s):
            by_cik[ciks[s]].append(s)
    today = _today()
    renames, gone = [], []
    for r in master:
        if r["symbol"] in live:
            continue
        cands = [s for s in by_cik.get(r.get("cik") or "", []) if s not in before]
        if len(cands) == 1:
            renames.append({"old": r["symbol"], "new": cands[0], "cik": r["cik"], "date": today})
        else:
            gone.append(dict(r, delisted_on=today))
    if renames:
        print(f"Symbol changes: {', '.join(c['old'] + '→' + c['new'] for c in renames)}")
    if gone:
        print(f"No longer listed (archived to {os.path.basename(DELISTED)}): "
              f"{[g['symbol'] for g in gone][:40]}")
    if not dry_run:
        _append_csv(CHANGES, ["old", "new", "cik", "date"], renames)
        _append_csv(DELISTED, FIELDS + ["delisted_on"], gone)
    return renames


def _append_csv(path, fields, rows):
    if not rows:
        return
    new_file = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new_file:
            w.writeheader()
        w.writerows(rows)


def save_master(rows):
    tmp = MASTER + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["symbol"]))
    os.replace(tmp, MASTER)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="report only, change nothing")
    ap.add_argument("--no-profiles", action="store_true",
                    help="don't fetch Yahoo profiles (use the cache)")
    args = ap.parse_args()

    live = fetch_listings()
    print(f"Listings: {len(live)} common stocks "
          f"({Counter(v['exchange'] for v in live.values())})")
    ciks = fetch_ciks()
    master = load_master_rows()
    old_cik = {r["symbol"]: r.get("cik", "") for r in master}
    reconcile(master, live, ciks, dry_run=args.dry_run)

    tree = load_hierarchy()
    assert {v["sector"] for v in tree.values()} == CURRENT_MACROS, "GICS tree ≠ 11 sectors"
    sp = fetch_sp1500(tree)
    overrides = load_overrides()
    profiles = _load_json(PROFILE_CACHE)
    if args.dry_run or args.no_profiles:
        print(f"Yahoo profiles: {len(profiles_to_fetch(live, profiles))} to fetch "
              f"(skipped: {'--dry-run' if args.dry_run else '--no-profiles'})")
    else:
        fetch_profiles(list(live), profiles)

    ymap = learn_yahoo_map(sp, profiles, load_yahoo_map())
    rows, excluded, src = [], [], Counter()
    for sym, info in live.items():
        if is_excluded(sym, profiles.get(sym), sp, overrides):
            excluded.append(sym)
            continue
        levels, source = classify(sym, tree, overrides, sp, profiles, ymap)
        src[source or "unclassified"] += 1
        rows.append(dict(info, cik=ciks.get(sym) or old_cik.get(sym, ""),
                         source=source, **levels))

    n = len(rows)
    print(f"Universe: {n} stocks ({len(excluded)} funds/ETFs/SPAC shells/excluded dropped). "
          f"Classified: {n - src['unclassified']} ({(n - src['unclassified']) / max(n, 1):.1%}) "
          f"— {dict(src)}")
    unmapped = Counter((profiles.get(r['symbol']) or {}).get("industry") for r in rows
                       if not r["source"] and (profiles.get(r['symbol']) or {}).get("industry"))
    if unmapped:
        print(f"Yahoo industries without a GICS mapping (fill in {os.path.basename(YAHOO_MAP)}): "
              f"{unmapped.most_common(15)}")
    if args.dry_run:
        print("(dry run — nothing written)")
        return
    save_yahoo_map(ymap)
    save_master(rows)
    print(f"Wrote {MASTER}")


if __name__ == "__main__":
    main()
