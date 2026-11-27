"""Upcoming earnings dates (rs_engine.events) and IPOs (rs_engine.ipos)."""
from rs_engine import events


def test_parse_nasdaq_earnings_rows():
    rows = [{"symbol": "CRM", "time": "time-after-hours"},
            {"symbol": "BMO", "time": "time-pre-market"},
            {"symbol": "BRK.B", "time": "time-not-supplied"},
            {"symbol": ""}]
    assert events.parse_nasdaq("2026-12-02", rows) == {
        ("CRM", "2026-12-02"): "Results (after close)",
        ("BMO", "2026-12-02"): "Results (before open)",
        ("BRK-B", "2026-12-02"): "Results"}
    assert events.is_results("2026-12-02 Results (after close)")


def test_next_events_window_and_results_flag():
    ev = {("A", "2026-11-23"): "Results",          # on the as-of day: already out
          ("A", "2026-11-26"): "Dividend",
          ("A", "2026-12-01"): "Results",
          ("B", "2026-12-20"): "Results"}           # beyond 14 days
    nxt = events.next_events(ev, "2026-11-23")
    assert nxt == {"A": "2026-11-26 Dividend"}
    assert events.is_results("2026-12-01 Results, Dividend") and not events.is_results("2026-11-26 Dividend")


def test_merge_keeps_recent_past_and_replaces_future(tmp_path):
    old = {("A", "2026-11-10"): "Results", ("A", "2026-11-20"): "Results", ("B", "2026-11-30"): "Results"}
    new = {("C", "2026-11-28"): "Results"}
    out = events.merge(old, new, "2026-11-23")
    assert out == {("A", "2026-11-20"): "Results", ("C", "2026-11-28"): "Results"}
    p = str(tmp_path / "ev.csv")
    events.write_events(out, p)
    assert events.read_events(p) == out


def test_refresh_events_main(tmp_path, monkeypatch):
    import datetime as dt
    import refresh_events as re_
    from rs_engine import ipos
    from rs_engine.config import MKT_TZ
    today = dt.datetime.now(MKT_TZ).date()
    d1 = today + dt.timedelta(days=1)
    while d1.weekday() >= 5:
        d1 += dt.timedelta(days=1)

    class R:
        status_code = 200

        def __init__(self, data):
            self.data = data

        def json(self):
            return {"data": self.data}

    class S:
        headers = {}

        def get(self, url, timeout):
            if "ipo/calendar" in url:
                return R({"priced": {"rows": [{"proposedTickerSymbol": "NEWIPO", "pricedDate": "11/03/2026",
                                               "proposedSharePrice": "15.00", "dealStatus": "Priced"}]}})
            day = url.rsplit("=", 1)[1]
            return R({"rows": [{"symbol": "AAA", "time": "time-after-hours"}] if day == d1.isoformat() else []})
    monkeypatch.setattr(events, "EVENTS", str(tmp_path / "ev.csv"))
    monkeypatch.setattr(ipos, "IPOS", str(tmp_path / "ipos.csv"))
    monkeypatch.setattr(re_.requests, "Session", S)
    monkeypatch.setattr(re_.time, "sleep", lambda s: None)
    assert re_.main() == 0
    assert ipos.read()["NEWIPO"] == {"listed": "2026-11-03", "price": 15.0}
    assert events.read_events() == {("AAA", d1.isoformat()): "Results (after close)"}


def test_parse_nasdaq_ipos(tmp_path):
    from rs_engine import ipos
    rows = [{"proposedTickerSymbol": "BBTX", "pricedDate": "10/30/2026", "proposedSharePrice": "16.00",
             "dealStatus": "Priced"},
            {"proposedTickerSymbol": "GBC", "pricedDate": "10/29/2026", "proposedSharePrice": None,
             "dealStatus": "Priced"},
            {"proposedTickerSymbol": "SOON", "pricedDate": "", "proposedSharePrice": "10"}]
    got = ipos.parse_nasdaq(rows)
    assert got == {"BBTX": {"listed": "2026-10-30", "price": 16.0},
                   "GBC": {"listed": "2026-10-29", "price": None}}
    p = str(tmp_path / "ipos.csv")
    ipos.write(got, p)
    assert ipos.read(p) == got


def test_read_ipos_follows_renames(tmp_path, monkeypatch):
    import build_rs as B
    from rs_engine import ipos
    p = str(tmp_path / "ipos.csv")
    ipos.write({"OLDSYM": {"listed": "2026-05-01", "price": 100.0}}, p)
    monkeypatch.setattr(ipos, "IPOS", p)
    assert B.read_ipos([("OLDSYM", "NEWSYM")]) == {"NEWSYM": {"listed": "2026-05-01", "price": 100.0}}
