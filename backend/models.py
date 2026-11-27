from pydantic import BaseModel


class MetaResponse(BaseModel):
    benchmark: str
    window: int
    drange: str
    generated: str
    n_stocks: int
    n_groups_per_level: list[int]
    n_window: int = 0          # stocks with full window data (in the levels)
    excluded: list[str] = []   # stocks with price data but no window coverage
    illiquid: list[str] = []   # thinly traded: left out of the rankings
    src: dict[str, str] = {}   # {date: source} for non-Yahoo benchmark bars
    ipo: list[dict] = []       # new listings awaiting their first ranking


class LevelSummary(BaseModel):
    key: str
    label: str
    n_groups: int
    note: str | None = None


class MemberOut(BaseModel):
    n: str               # name
    s: str               # symbol (Yahoo style, BRK-B)
    x: str | None = None  # exchange (NASDAQ / NYSE / AMEX / CBOE / TXSE / IEX)
    r: list[float]       # rs_series (26 values for sparkline)
    p: float             # RS_STS% (0..1)
    e: list[int]         # EMA flags [-1,0,1] × 5
    b: int               # RS line vs its EMA21: 1=above, 0=below, -1=n/a
    d: float | None = None   # RS_STS% change vs previous snapshot (pp)
    l: float | None = None  # noqa: E741 — last traded price (data key 'l')
    h: float | None      # % off 52-week high
    a: float | None      # ADR%
    nw: int | None = None   # new listing: days since listing (RS% over those days)
    ap: int | None = None   # 1 = classification mapped from Yahoo, not official GICS
    m1: float | None = None   # % return over 21 sessions (~1 month)
    m3: float | None = None   # % return over 63 sessions (~3 months)
    d1: float | None = None   # % change on the last session
    w1: float | None = None   # % return over 5 sessions (1 week)
    cap: str | None = None    # Large / Mid / Small / Micro / New (size class)
    tv: float | None = None   # avg daily traded value, last 20 sessions (US$ M)
    rr: int | None = None     # RS rating 1-99 vs all stocks (weighted 12M performance)
    vr: float | None = None   # today's volume ÷ its 50-session average
    vd: float | None = None   # 10-session ÷ prior 50-session average volume
    tt: float | None = None   # 10-session high-low range, % of close
    ev: str | None = None     # next earnings date within 14 days: "YYYY-MM-DD Results …"
    ipo: dict | None = None   # IPO in the last year: {d, p, n, g, x}
    wk: dict | None = None    # weekly 10-SMA trend: {s: SMA, d: % vs it, u: rising, n: weeks above}
    htf: dict | None = None   # High Tight Flag: {p, g, pw, fd, fw, dist, dry, bo}


class GroupOut(BaseModel):
    id: int
    name: str
    n: int               # n_constituents
    pct: float           # group RS_STS% (0..1)
    dp: float | None = None  # RS_STS% change vs previous snapshot (pp)
    r: list[float]       # group rs_series
    members: list[MemberOut]


class LevelResponse(BaseModel):
    key: str
    label: str
    groups: list[GroupOut]
    note: str | None = None    # e.g. "Official GICS only — S&P 1500 …"


class MacroBreadth(BaseModel):
    name: str
    dates: list[str]
    osc: list[float]
    latest: float


class BreadthResponse(BaseModel):
    dates: list[str]
    osc: list[float]
    n: int
    latest: float
    span: str
    macros: list[MacroBreadth] = []
    divergence: dict = {}
    market: dict = {}      # benchmark trend: close, ema flags, w1/m1/m3, off_high


class RotationGroup(BaseModel):
    name: str
    rs: list[float]


class RotationResponse(BaseModel):
    dates: list[str]
    groups: list[RotationGroup] = []


class WatchlistIn(BaseModel):
    symbols: list[str] = []


class StockDetail(BaseModel):
    sym: str
    name: str = ""
    dates: list[str]       # ~125 recent trading days
    rs: list[float]        # RS line = stock / S&P 500 (TradingView style)
    ema21: list[float]     # 21-day EMA of the RS line
    ltp: float
    rse: int               # 1 = above EMA21, 0 = below
