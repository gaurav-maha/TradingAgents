from __future__ import annotations

import json


NASDAQ_LISTED = """Symbol|Security Name|Market Category|Test Issue|Financial Status|Round Lot Size|ETF|NextShares
MSFT|Microsoft Corporation Common Stock|Q|N|N|100|N|N
QQQM|Invesco NASDAQ 100 ETF|G|N|N|100|Y|N
ZTEST|Nasdaq Test Issue|G|Y|N|100|N|N
File Creation Time: 0605202618:01|||||||
"""

OTHER_LISTED = """ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|Test Issue|NASDAQ Symbol
JPM|JP Morgan Chase & Co. Common Stock|N|JPM|N|100|N|JPM
BRK/B|Berkshire Hathaway Inc. Class B|N|BRK/B|N|100|N|BRK/B
SPY|SPDR S&P 500 ETF Trust|P|SPY|Y|100|N|SPY
NYSETEST|NYSE Test Issue|N|NYSETEST|N|100|Y|NYSETEST
File Creation Time: 0605202618:01|||||||
"""


def test_load_nyse_nasdaq_company_listings_includes_etfs_but_filters_tests_and_non_nyse():
    from tradingagents.universe import load_nyse_nasdaq_company_listings

    pages = {
        "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt": NASDAQ_LISTED,
        "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt": OTHER_LISTED,
    }

    listings = load_nyse_nasdaq_company_listings(fetch_text=lambda url: pages[url])

    assert [(item.symbol, item.exchange) for item in listings] == [
        ("BRK-B", "NYSE"),
        ("JPM", "NYSE"),
        ("MSFT", "NASDAQ"),
        ("QQQM", "NASDAQ"),
    ]
    assert listings[0].name == "Berkshire Hathaway Inc. Class B"


def test_select_top_companies_by_market_cap_caps_and_sorts_descending():
    from tradingagents.universe import SecurityListing, select_top_companies_by_market_cap

    listings = [
        SecurityListing(symbol="AAA", name="AAA Corp", exchange="NASDAQ"),
        SecurityListing(symbol="BBB", name="BBB Corp", exchange="NYSE"),
        SecurityListing(symbol="CCC", name="CCC Corp", exchange="NASDAQ"),
    ]
    caps = {"AAA": 10, "BBB": None, "CCC": 30}

    selected = select_top_companies_by_market_cap(
        listings,
        limit=2,
        market_cap_provider=lambda symbol: caps[symbol],
        workers=1,
    )

    assert [(item.symbol, item.market_cap) for item in selected] == [
        ("CCC", 30),
        ("AAA", 10),
    ]


def test_fetch_yfinance_market_cap_falls_back_to_etf_total_assets(monkeypatch):
    from tradingagents import universe

    class FakeTicker:
        fast_info = {"market_cap": None, "marketCap": None}

        def get_info(self):
            return {
                "quoteType": "ETF",
                "marketCap": None,
                "totalAssets": 123_456,
            }

    monkeypatch.setattr(universe.yf, "Ticker", lambda symbol: FakeTicker())

    assert universe.fetch_yfinance_market_cap("SPY") == 123_456


def test_rank_universe_run_results_chooses_best_rating_then_market_cap():
    from tradingagents.universe import UniverseRunResult, rank_universe_run_results

    results = [
        UniverseRunResult(ticker="AAA", rating="Overweight", score=4, market_cap=100, final_decision=""),
        UniverseRunResult(ticker="BBB", rating="Buy", score=5, market_cap=20, final_decision=""),
        UniverseRunResult(ticker="CCC", rating="Buy", score=5, market_cap=200, final_decision=""),
        UniverseRunResult(ticker="ERR", rating="Error", score=0, market_cap=999, final_decision="", error="boom"),
    ]

    ranked = rank_universe_run_results(results)

    assert [item.ticker for item in ranked] == ["CCC", "BBB", "AAA"]


def test_run_top_nyse_nasdaq_universe_runs_each_selected_ticker_and_writes_summary(tmp_path):
    from tradingagents.universe import (
        SecurityListing,
        UniverseSummary,
        run_top_nyse_nasdaq_universe,
    )

    class FakeGraph:
        def __init__(self, selected_analysts, config, debug):
            self.selected_analysts = selected_analysts
            self.config = config
            self.debug = debug
            self.calls = []

        def propagate(self, ticker, trade_date, asset_type="stock"):
            self.calls.append((ticker, trade_date, asset_type))
            rating = "Buy" if ticker == "MSFT" else "Hold"
            return {"final_trade_decision": f"**Rating**: {rating}\nDecision for {ticker}"}, rating

    graph_instances = []

    def graph_factory(selected_analysts, config, debug):
        graph = FakeGraph(selected_analysts, config, debug)
        graph_instances.append(graph)
        return graph

    summary = run_top_nyse_nasdaq_universe(
        config={"results_dir": str(tmp_path)},
        trade_date="2026-06-05",
        selected_analysts=["market", "news"],
        limit=2,
        listings_loader=lambda: [
            SecurityListing("AAPL", "Apple Inc.", "NASDAQ", market_cap=100),
            SecurityListing("MSFT", "Microsoft Corporation", "NASDAQ", market_cap=200),
        ],
        market_cap_provider=lambda symbol: {"AAPL": 100, "MSFT": 200}[symbol],
        graph_factory=graph_factory,
        output_dir=tmp_path / "summary",
        workers=1,
    )

    assert isinstance(summary, UniverseSummary)
    assert graph_instances[0].calls == [
        ("MSFT", "2026-06-05", "stock"),
        ("AAPL", "2026-06-05", "stock"),
    ]
    assert summary.best_ticker == "MSFT"
    assert [item.ticker for item in summary.ranked_results] == ["MSFT", "AAPL"]

    payload = json.loads((tmp_path / "summary" / "universe_summary.json").read_text())
    assert payload["best_ticker"] == "MSFT"
    assert payload["ranked_results"][0]["rating"] == "Buy"
    assert (tmp_path / "summary" / "universe_summary.md").exists()
