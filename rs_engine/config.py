"""Constants + configuration for the RS pipeline.

Defaults live in code; a `config.toml` next to the project root can override
them (all keys optional, section [rs]).  The NYSE trading-holiday calendar
lives in us_holidays.csv.
"""
import os
import csv
import datetime as dt
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Price caches live OUTSIDE the repo (the project can sit on a fuseblk/NTFS
# volume that strips exec bits and corrupted a cache once).
CACHE_DIR = os.environ.get("US_RS_CACHE_DIR") or os.path.join(
    os.path.expanduser("~"), ".cache", "us-rs")
os.makedirs(CACHE_DIR, exist_ok=True)


def _load_overrides():
    try:
        import tomllib
    except ImportError:                       # python < 3.11
        return {}
    path = os.path.join(HERE, "config.toml")
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "rb") as fh:
            return (tomllib.load(fh) or {}).get("rs", {})
    except Exception:
        return {}


_CFG = _load_overrides()


def cfg(key, default):
    return _CFG.get(key, default)


WINDOW = int(cfg("window", 26))                 # RS window (matches the sheet)
FULL_RANGE = str(cfg("full_range", "2y"))       # first-fetch history depth
APPEND_RANGE = str(cfg("append_range", "1mo"))  # incremental daily top-up
MIN_BARS = int(cfg("min_bars", 200))            # below → refetch FULL_RANGE
EMA_PERIODS = [int(x) for x in cfg("ema_periods", [20, 50, 100, 150, 200])]
ADR_PERIOD = int(cfg("adr_period", 20))         # ADR% lookback (Qullamaggie 20)
CORP_ACTION_JUMP = float(cfg("corp_action_jump", 0.90))   # Yahoo US closes are split-adjusted
ILLIQUID_TURNOVER_M = float(cfg("illiquid_turnover_m", 1.0))  # US$ M/day, see maths.is_illiquid
BENCHMARK_NAME = str(cfg("benchmark", "S&P 500"))
BENCH_YSYM = str(cfg("bench_symbol", "^GSPC"))
YH_WORKERS = int(cfg("yahoo_workers", 8))
MIN_PRICE = float(cfg("min_price", 1.0))        # below → kept out of the rankings
MKT_TZ = ZoneInfo("America/New_York")           # NYSE trading day (DST-aware)
SETTLE_HOUR = 17                                # Yahoo EOD settled (~1h after close)



def is_discontinuity(ratio):
    """True when a one-day close ratio (today / yesterday) is a data break, not
    trading: beyond ×(1-J) down or its mirror ÷(1-J) up — J=0.9 means a ×10
    move either way (a reverse split Yahoo adjusted late).  Symmetric in log
    terms so a real +150% biotech day stays a trading move."""
    return ratio < 1 - CORP_ACTION_JUMP or ratio > 1 / (1 - CORP_ACTION_JUMP)


HOLIDAYS_CSV = os.path.join(HERE, "us_holidays.csv")
_holidays_cache = None


def load_holidays():
    """NYSE full-day holiday calendar from us_holidays.csv (date,description).
    Early-close days (1 pm) are normal sessions and are not listed."""
    global _holidays_cache
    if _holidays_cache is None:
        _holidays_cache = set()
        if os.path.exists(HOLIDAYS_CSV):
            with open(HOLIDAYS_CSV, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    d = (row.get("date") or "").strip()
                    if d:
                        _holidays_cache.add(d)
    return _holidays_cache


def is_holiday(iso_date):
    """True when the NYSE was closed on this date (official holiday calendar)."""
    return iso_date in load_holidays()


def latest_session(now=None):
    """ISO date of the most recent COMPLETED NYSE session: today once the data
    has settled (~17:00 New York time), otherwise the previous trading day."""
    now = (now or dt.datetime.now(MKT_TZ)).astimezone(MKT_TZ)
    d = now.date() if now.hour >= SETTLE_HOUR else now.date() - dt.timedelta(days=1)
    while d.weekday() >= 5 or is_holiday(d.isoformat()):
        d -= dt.timedelta(days=1)
    return d.isoformat()


# The 11 GICS sectors (2023+ structure); anything else in the master is a
# typo or a stale/unmapped value.
CURRENT_MACROS = frozenset({
    "Communication Services", "Consumer Discretionary", "Consumer Staples",
    "Energy", "Financials", "Health Care", "Industrials",
    "Information Technology", "Materials", "Real Estate", "Utilities"})

_LOWER_WORDS = {"and", "of", "in", "for", "the", "or", "with"}


def standard_name(value):
    """One spelling per classification name: Title Case words ('Port & Port
    services' → 'Port & Port Services'), joining words ('and', 'of', …)
    lowercase after the first, tokens that already carry capitals kept (IT, FMCG, BPO), whitespace
    collapsed, and mis-decoded dashes (U+FFFD) repaired as ' - '."""
    import re
    v = re.sub(r"\s*�+\s*", " - ", value or "")
    v = " ".join(v.split())
    if v.isupper() and len(v) > 4:                # ALL-CAPS outlier → title
        v = v.title()
    words = v.split(" ")
    for i, w in enumerate(words):
        if i and w.lower() in _LOWER_WORDS:
            words[i] = w.lower()
            continue
        if not w or not w[0].isalpha() or any(c.isupper() for c in w):
            continue
        words[i] = w[0].upper() + w[1:]
    return " ".join(words)


_COMMON_SHORT = {"of", "and", "the", "ltd", "co", "inc", "plc", "llc", "one", "new", "for",
                 "in", "big", "sun", "tea", "oil", "gas", "art", "web", "hi", "go",
                 "om", "sky", "we", "win", "gem", "tek"}


def standard_company(name):
    """Company names in normal case: a few listings arrive in ALL CAPS
    ('GENERAL MOTORS COMPANY' → 'General Motors Company').  Acronyms survive —
    a token of <= 3 letters (GMR, BIL, EPC) or without vowels (DMCC) stays
    upper-case unless it is a common word (of, and, Ltd…).  Names already in
    mixed case are kept exactly as listed."""
    name = " ".join((name or "").split())
    if not name.isupper():
        return name

    def fix(tok):
        letters = "".join(c for c in tok if c.isalpha())
        if not letters:
            return tok
        low = letters.lower()
        if low in _COMMON_SHORT:
            word = low if low in ("of", "and", "the", "for", "in") else low.capitalize()
            return tok.replace(letters, word) if letters in tok else tok
        if len(letters) <= 3 or not set(low) & set("aeiouy"):
            return tok                                    # acronym
        out, up = [], True                                 # Title-case letters
        for c in tok:
            out.append(c.upper() if up else c.lower()) if c.isalpha() else out.append(c)
            up = not c.isalpha() and c != "'"
        return "".join(out)

    words = []
    for i, w in enumerate(name.split(" ")):
        w = "-".join(fix(p) for p in w.split("-"))
        if i == 0 and w[:1].islower():
            w = w[0].upper() + w[1:]
        words.append(w)
    return " ".join(words)
