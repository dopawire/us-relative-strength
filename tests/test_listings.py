"""Tests for the US universe refresh: listing filters, symbols, GICS
classification precedence, fund/SPAC exclusion, renames and delistings."""
import csv

import build_rs
import refresh_universe as ru


def _read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# --------------------------------------------------------------------------- #
#  Listings: NASDAQ Trader symbol directories
# --------------------------------------------------------------------------- #
NASDAQ_TXT = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
AAPL|Apple Inc. - Common Stock|Q|N|N|100|N|N
QQQ|Invesco QQQ Trust, Series 1|G|N|N|100|Y|N
ZXZZT|NASDAQ TEST STOCK|G|Y|N|100|N|N
ACAAU|Averin Capital Acquisition Corp. - Units|G|N|N|100|N|N
ACAAW|Averin Capital Acquisition Corp. - Warrants|G|N|N|100|N|N
IQMXW|IQM Quantum Computers Oyj - Warrants to purchase American Depositary Shares|Q|N|N|100|N|N
LEGN|Legend Biotech Corporation - American Depositary Shares|Q|N|N|100|N|N
PFBC|Preferred Bank - Common Stock|Q|N|N|100|N|N
ARCC|Ares Capital Corporation - Closed End Fund|Q|N|N|100|N|N
File Creation Time: 1030202609:46|||||||
"""

OTHER_TXT = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
BRK.B|Berkshire Hathaway Inc. New Common Stock|N|BRK.B|N|100|N|BRK.B
AAC.U|Ares Acquisition Corp Units, each consisting of one share|N|AAC.U|N|100|N|AAC=
ABR$D|Arbor Realty Trust 6.375% Series D Cumulative Redeemable Preferred Stock|N|ABRpD|N|100|N|ABR-D
EPD|Enterprise Products Partners L.P. Common Units representing Limited Partner Interests|N|EPD|N|100|N|EPD
ELC|Entergy Louisiana, Inc. Collateral Trust Mortgage Bonds, 4.875 % Series due September 1, 2066|N|ELC|N|100|N|ELC
NAD|Nuveen Quality Municipal Income Fund Common Shares of Beneficial Interest|N|NAD|N|100|N|NAD
SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY
IMO|Imperial Oil Limited Common Stock|A|IMO|N|100|N|IMO
CBOE|Cboe Global Markets, Inc. Common Stock|Z|CBOE|N|40|N|CBOE
TCBI|Texas Capital Bancshares, Inc. Common Stock|TXSE|TCBI|N|100|N|TCBI
"""


def test_parse_listings_keeps_common_equity_only():
    live = ru.parse_listings(NASDAQ_TXT, OTHER_TXT)
    assert sorted(live) == ["AAPL", "ARCC", "BRK-B", "CBOE", "EPD", "IMO", "LEGN",
                            "PFBC", "TCBI"]
    assert live["BRK-B"] == {"symbol": "BRK-B", "company": "Berkshire Hathaway Inc.",
                             "exchange": "NYSE"}
    assert live["AAPL"]["company"] == "Apple Inc." and live["AAPL"]["exchange"] == "NASDAQ"
    assert live["IMO"]["exchange"] == "AMEX"            # NYSE American → TradingView AMEX
    assert live["CBOE"]["exchange"] == "CBOE" and live["TCBI"]["exchange"] == "TXSE"
    assert live["EPD"]["company"] == "Enterprise Products Partners L.P."


def test_yahoo_symbol():
    assert ru.yahoo_symbol("BRK.B") == "BRK-B"
    assert ru.yahoo_symbol("AAPL") == "AAPL"
    assert ru.yahoo_symbol("ABR$D") is None             # preferred
    assert ru.yahoo_symbol("AAC.U") is None             # unit
    assert ru.yahoo_symbol("AAC.WS") is None            # warrant


def test_is_common_equity_names():
    ok = ["Preferred Bank - Common Stock",                 # 'Preferred' in the company name
          "Enterprise Products Partners L.P. Common Units representing LP Interests",
          "Coca Cola Femsa S.A.B. de C.V. American Depositary Shares, each representing "
          "10 Units (each Unit consists of 3 Series B Shares and 5 Series L Shares)",
          "Ares Capital Corporation - Closed End Fund"]    # BDC: left to the Yahoo check
    bad = ["Kensington Capital Acquisition Corp. VI Class 1 redeemable warrants",
           "QuasarEdge Acquisition Corporation Units, each consisting of one Ordinary Share",
           "Saratoga Investment Corp 8.125% Notes due 2027",
           "Arbor Realty Trust Depositary Shares, each representing 1/40th of a share",
           "Virtus Total Return Fund Inc.",
           "BlackRock Technology and Private Equity Term Trust Common Shares",
           "Goldman Sachs Group Securities STRATS Trust for Goldman Sachs Group Securities",
           "Comcast Holdings ZONES",
           "Dillard's Capital Trust I",
           "IQM Quantum Computers Oyj - Warrants to purchase American Depositary Shares"]
    assert all(ru.is_common_equity(n) for n in ok), [n for n in ok if not ru.is_common_equity(n)]
    assert not any(ru.is_common_equity(n) for n in bad), [n for n in bad if ru.is_common_equity(n)]


def test_clean_company():
    assert ru.clean_company("Agilent Technologies, Inc. Common Stock") == "Agilent Technologies, Inc."
    assert ru.clean_company("AEVEX Corp. Class A Common Stock") == "AEVEX Corp."
    assert ru.clean_company("Zhihu Inc. - American Depositary Shares") == "Zhihu Inc."
    assert ru.clean_company("Alcoa Corporation Common Stock ") == "Alcoa Corporation"


# --------------------------------------------------------------------------- #
#  GICS: tree, Wikipedia, precedence, Yahoo mapping
# --------------------------------------------------------------------------- #
def test_gics_hierarchy_is_complete():
    from rs_engine.config import CURRENT_MACROS
    tree = ru.load_hierarchy()
    assert len(tree) == 163                                       # GICS 2023 sub-industries
    assert len({v["industry"] for v in tree.values()}) == 74
    assert len({v["industryGroup"] for v in tree.values()}) == 25
    assert {v["sector"] for v in tree.values()} == CURRENT_MACROS  # the 11 sectors
    assert tree["Mortgage REITs"]["sector"] == "Financials"       # moved out of Real Estate in 2023
    assert tree["Semiconductors"]["industryGroup"] == "Semiconductors & Semiconductor Equipment"


def test_parse_wiki_constituents():
    page = ('<table class="wikitable" id="constituents"><tbody>'
            '<tr id="a"><th>Symbol</th><th>Security</th><th>GICS Sector</th><th>GICS Sub-Industry</th></tr>'
            '<tr id="b"><td><a href="x" data-mw=\'{"a":"<b>"}\'>BRK.B<!-- DO NOT CHANGE --></a></td>'
            '<td>Berkshire</td><td>Financials</td><td>Multi-Sector Holdings</td></tr>'
            '<tr><td>AAPL</td><td>Apple</td><td>Information Technology</td>'
            '<td>Technology Hardware, Storage &amp; Peripherals</td></tr></tbody></table>')
    assert ru.parse_wiki_constituents(page) == [
        ("BRK-B", "Multi-Sector Holdings"),
        ("AAPL", "Technology Hardware, Storage & Peripherals")]


def test_classify_precedence():
    tree = ru.load_hierarchy()
    profiles = {"X": {"industry": "Biotechnology"}, "Y": {"industry": "Biotechnology"},
                "Z": {"industry": "Biotechnology"}}
    ymap = {"Biotechnology": {"subIndustry": "Biotechnology"}}
    sp = {"Y": "Pharmaceuticals", "Z": "Pharmaceuticals"}
    overrides = {"Z": "Life Sciences Tools & Services"}
    assert ru.classify("X", tree, overrides, sp, profiles, ymap)[1] == "yahoo"
    lv, src = ru.classify("Y", tree, overrides, sp, profiles, ymap)
    assert src == "sp1500" and lv["subIndustry"] == "Pharmaceuticals"
    lv, src = ru.classify("Z", tree, overrides, sp, profiles, ymap)
    assert src == "override" and lv["industry"] == "Life Sciences Tools & Services"
    assert ru.classify("Q", tree, overrides, sp, profiles, ymap) == (
        {k: "" for k in ru.LEVEL_FIELDS}, "")


def test_learn_yahoo_map_majority_and_manual_wins():
    sp = {"A": "Regional Banks", "B": "Regional Banks", "C": "Diversified Banks",
          "D": "Brewers", "E": "Agricultural Products & Services"}
    profiles = {"A": {"industry": "Banks - Regional"}, "B": {"industry": "Banks - Regional"},
                "C": {"industry": "Banks - Regional"}, "D": {"industry": "Beverages - Brewers"},
                "E": {"industry": "Shell Companies"},           # SPAC shells never vote
                "N": {"industry": "Silver"}}                    # no S&P evidence
    current = {"Beverages - Brewers": {"yahoo_industry": "Beverages - Brewers",
                                       "subIndustry": "Distillers & Vintners", "source": "manual"}}
    out = ru.learn_yahoo_map(sp, profiles, current)
    assert out["Banks - Regional"]["subIndustry"] == "Regional Banks"
    assert out["Banks - Regional"]["votes"] == "2/3"
    assert out["Beverages - Brewers"]["subIndustry"] == "Distillers & Vintners"   # manual kept
    assert out["Silver"]["subIndustry"] == "" and out["Silver"]["votes"] == "0/0"
    assert "Shell Companies" not in out


def test_is_excluded_funds_shells_and_overrides():
    cef = {"qt": "EQUITY", "industry": "Asset Management",
           "summary": "Royce Small-Cap Trust, Inc. is a close ended equity mutual fund ..."}
    bdc = {"qt": "EQUITY", "industry": "Asset Management",
           "summary": "Ares Capital Corporation is a business development company ..."}
    bdc_closed = {"qt": "EQUITY", "industry": "Asset Management",
                  "summary": "a closed-end, non-diversified management investment company "
                             "that has elected to be regulated as a business development company"}
    assert ru.is_excluded("RVT", cef, sp={})
    assert not ru.is_excluded("ARCC", bdc, sp={})
    assert not ru.is_excluded("X", bdc_closed, sp={})
    assert ru.is_excluded("SPY", {"qt": "ETF"}, sp={})
    assert ru.is_excluded("QSEA", {"qt": "EQUITY", "industry": "Shell Companies"}, sp={})
    assert not ru.is_excluded("RVT", cef, sp={"RVT": "Asset Management & Custody Banks"})
    assert ru.is_excluded("TVC", None, sp={}, overrides={"TVC": "EXCLUDE"})
    assert not ru.is_excluded("NEW", None, sp={})        # IPO without a profile yet: stub


def test_profiles_to_fetch():
    cache = {"OK": {"industry": "Banks - Regional", "t": "2026-10-20"},
             "MISS_NEW": {"industry": "", "t": "2026-10-29"},           # retry in 3 days
             "MISS_OLD": {"industry": "", "t": "2026-10-20"},
             "STALE": {"industry": "Biotechnology", "t": "2026-06-01"},
             "AM_OLD": {"industry": "Asset Management", "t": "2026-10-20"}}   # no summary
    todo = ru.profiles_to_fetch(["OK", "MISS_NEW", "MISS_OLD", "STALE", "AM_OLD", "NEW"],
                                cache, today="2026-10-30")
    assert todo == ["MISS_OLD", "AM_OLD", "NEW", "STALE"]


# --------------------------------------------------------------------------- #
#  Renames / delistings
# --------------------------------------------------------------------------- #
def _row(sym, cik, sub=""):
    r = {k: "" for k in ru.FIELDS}
    r.update(symbol=sym, company=sym + " Inc.", exchange="NYSE", cik=cik, subIndustry=sub)
    return r


def test_reconcile_renames_by_cik_and_archives():
    master = [_row("KEEP", "1"), _row("OLDNAME", "2", "Gold"), _row("GONE", "3"),
              _row("GOOG", "4"), _row("GOOGX", "4")]          # dual class: one class goes away
    filler = [_row(f"F{i}", f"9{i}") for i in range(30)]       # clear the truncation guard
    master += filler
    live = {s: {} for s in ["KEEP", "NEWNAME", "GOOG"] + [r["symbol"] for r in filler]}
    ciks = {"KEEP": "1", "NEWNAME": "2", "GOOG": "4"}
    renames = ru.reconcile(master, live, ciks)
    assert [(c["old"], c["new"]) for c in renames] == [("OLDNAME", "NEWNAME")]
    assert [(c["old"], c["new"], c["cik"]) for c in _read(ru.CHANGES)] == [
        ("OLDNAME", "NEWNAME", "2")]
    assert [g["symbol"] for g in _read(ru.DELISTED)] == ["GONE", "GOOGX"]


def test_reconcile_skips_truncated_list():
    master = [_row(f"S{i}", str(i)) for i in range(10)]
    assert ru.reconcile(master, {"S0": {}}, {}) == []
    import os
    assert not os.path.exists(ru.DELISTED)


def test_stitch_renamed_prepends_history_only():
    old = {"2026-01-01": 10.0, "2026-01-02": 11.0, "2026-01-05": 99.0}
    new = {"2026-01-05": 12.0, "2026-01-06": 13.0}
    stores = ({"OLD": old, "NEW": new},)
    assert build_rs.stitch_renamed(stores, [("OLD", "NEW")]) == 2
    assert new == {"2026-01-01": 10.0, "2026-01-02": 11.0,
                   "2026-01-05": 12.0, "2026-01-06": 13.0}     # overlap keeps the new


def test_standard_company_keeps_acronyms():
    from rs_engine.config import standard_company as f
    assert f("GENERAL MOTORS COMPANY") == "General Motors Company"
    assert f("IBM CORP") == "IBM Corp"                                 # acronym
    assert f("BANK OF AMERICA CORP") == "Bank of America Corp"
    assert f("NVIDIA Corporation") == "NVIDIA Corporation"            # mixed: untouched


# --------------------------------------------------------------------------- #
#  Broken sources must never empty the master (Nov 2026: the website served a
#  bot-check page with HTTP 200 and an empty universe was written)
# --------------------------------------------------------------------------- #
def test_listing_text_falls_back_to_ftp_on_a_bot_page(monkeypatch):
    import io
    import urllib.request

    class Resp:
        status_code = 200
        text = "<html>bot check</html>"
        headers = {"content-type": "text/html"}
    monkeypatch.setattr(ru.requests, "get", lambda *a, **k: Resp())
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout: io.BytesIO(NASDAQ_TXT.encode()))
    assert ru._listing_text(ru.NASDAQ_LISTED).startswith("Symbol|")
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout: io.BytesIO(b"<html>also blocked</html>"))
    assert ru._listing_text(ru.NASDAQ_LISTED) is None


def test_refresh_keeps_master_when_listings_fail_or_shrink(monkeypatch):
    import os
    import pytest
    ru.save_master([_row(f"S{i}", str(i)) for i in range(2000)])
    before = open(ru.MASTER).read()
    for live in (None, {}, {f"S{i}": {"symbol": f"S{i}", "company": "x", "exchange": "NYSE"}
                            for i in range(1500)}):
        monkeypatch.setattr(ru, "fetch_listings", lambda live=live: live)
        monkeypatch.setattr("sys.argv", ["refresh_universe.py"])
        with pytest.raises(SystemExit):
            ru.main()
        assert open(ru.MASTER).read() == before
    assert not os.path.exists(ru.DELISTED)


def test_build_refuses_an_empty_universe(tmp_path, monkeypatch):
    import pytest
    p = tmp_path / "m.csv"
    p.write_text("symbol,company,exchange,cik,sector,industryGroup,industry,subIndustry,source\n")
    monkeypatch.setattr(build_rs, "MASTER_CSV", str(p))
    monkeypatch.setattr(build_rs, "RS_DATA_JSON", str(tmp_path / "rs.json"))
    monkeypatch.setattr(build_rs, "BUILD_LOCK", str(tmp_path / "lock"))
    monkeypatch.setattr("sys.argv", ["build_rs.py", "--html-only"])
    with pytest.raises(SystemExit):
        build_rs.main()
    assert not (tmp_path / "rs.json").exists()


def test_txse_code_and_unarchive_on_relisting():
    live = ru.parse_listings(
        NASDAQ_TXT.split("\n")[0] + "\n",
        OTHER_TXT.split("\n")[0] + "\nET|Energy Transfer LP Common Units|F|ET|N|100|N|ET\n")
    assert live["ET"]["exchange"] == "TXSE"
    ru._append_csv(ru.DELISTED, ru.FIELDS + ["delisted_on"],
                   [dict(_row("ET", "1"), delisted_on="2026-11-27"),
                    dict(_row("GONE", "2"), delisted_on="2026-11-27")])
    assert ru.unarchive(live) == ["ET"]
    assert [r["symbol"] for r in _read(ru.DELISTED)] == ["GONE"]
