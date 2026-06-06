"""NYSE/NASDAQ universe discovery and batch TradingAgents ranking.

The universe source is Nasdaq Trader's public symbol directory:
- ``nasdaqlisted.txt`` for Nasdaq-listed securities
- ``otherlisted.txt`` filtered to ``Exchange == N`` for NYSE-listed securities

Both files are generated for the current trading day and carry test-issue
flags, which lets this module exclude Nasdaq Trader test securities before
ranking by market cap. ETFs are intentionally eligible.
"""

from __future__ import annotations

import csv
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

import requests
import yfinance as yf

from tradingagents.agents.utils.rating import parse_rating


NASDAQ_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED_URL = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

RATING_SCORES = {
    "Buy": 5,
    "Overweight": 4,
    "Hold": 3,
    "Underweight": 2,
    "Sell": 1,
}


@dataclass(frozen=True)
class SecurityListing:
    symbol: str
    name: str
    exchange: str
    market_cap: Optional[int] = None


@dataclass(frozen=True)
class UniverseRunResult:
    ticker: str
    rating: str
    score: int
    market_cap: Optional[int]
    final_decision: str
    error: Optional[str] = None


@dataclass(frozen=True)
class UniverseSummary:
    best_ticker: Optional[str]
    ranked_results: list[UniverseRunResult]
    failed_results: list[UniverseRunResult]
    output_dir: Path


def _fetch_text(url: str, timeout: int = 30) -> str:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.text


def _normalize_symbol(symbol: str) -> str:
    """Normalize Nasdaq Trader symbols for yfinance/TradingAgents calls."""
    return symbol.strip().upper().replace("/", "-")


def _rows_from_pipe_text(text: str) -> Iterable[dict[str, str]]:
    lines = [
        line
        for line in text.splitlines()
        if line and not line.startswith("File Creation Time:")
    ]
    if not lines:
        return []
    return csv.DictReader(lines, delimiter="|")


def _parse_nasdaq_listed(text: str) -> list[SecurityListing]:
    listings: list[SecurityListing] = []
    for row in _rows_from_pipe_text(text):
        if row.get("Test Issue") != "N":
            continue
        symbol = _normalize_symbol(row.get("Symbol", ""))
        name = row.get("Security Name", "").strip()
        if symbol and name:
            listings.append(SecurityListing(symbol=symbol, name=name, exchange="NASDAQ"))
    return listings


def _parse_other_listed(text: str) -> list[SecurityListing]:
    listings: list[SecurityListing] = []
    for row in _rows_from_pipe_text(text):
        if row.get("Exchange") != "N":
            continue
        if row.get("Test Issue") != "N":
            continue
        symbol = _normalize_symbol(row.get("ACT Symbol", ""))
        name = row.get("Security Name", "").strip()
        if symbol and name:
            listings.append(SecurityListing(symbol=symbol, name=name, exchange="NYSE"))
    return listings


def load_nyse_nasdaq_company_listings(
    fetch_text: Callable[[str], str] = _fetch_text,
) -> list[SecurityListing]:
    """Load current Nasdaq and NYSE listings.

    Nasdaq Trader test issues and non-NYSE other-listed securities are
    excluded. ETFs are included. Duplicates are collapsed by symbol, with
    deterministic sorting.
    """
    by_symbol: dict[str, SecurityListing] = {}
    for listing in _parse_other_listed(fetch_text(OTHER_LISTED_URL)):
        by_symbol[listing.symbol] = listing
    for listing in _parse_nasdaq_listed(fetch_text(NASDAQ_LISTED_URL)):
        by_symbol[listing.symbol] = listing
    exchange_order = {"NYSE": 0, "NASDAQ": 1}
    return sorted(
        by_symbol.values(),
        key=lambda item: (exchange_order.get(item.exchange, 99), item.symbol),
    )


def fetch_yfinance_market_cap(symbol: str) -> Optional[int]:
    """Fetch market cap from yfinance, returning ``None`` when unavailable."""
    ticker = yf.Ticker(symbol)

    try:
        fast_info = ticker.fast_info
        market_cap = (
            fast_info.get("market_cap") or fast_info.get("marketCap")
            if isinstance(fast_info, dict)
            else getattr(fast_info, "market_cap", None)
        )
        if market_cap:
            return int(market_cap)
    except Exception:
        pass

    try:
        info = ticker.get_info() if hasattr(ticker, "get_info") else ticker.info
        market_cap = (
            info.get("marketCap")
            or info.get("market_cap")
            # ETFs commonly expose assets under management instead of market
            # cap. Use it for universe sizing so ETFs remain eligible.
            or info.get("totalAssets")
        )
        if market_cap:
            return int(market_cap)
    except Exception:
        return None

    return None


def select_top_companies_by_market_cap(
    listings: Sequence[SecurityListing],
    limit: int = 5000,
    market_cap_provider: Callable[[str], Optional[int]] = fetch_yfinance_market_cap,
    workers: int = 8,
) -> list[SecurityListing]:
    """Return the largest ``limit`` companies by market cap."""
    if limit <= 0:
        return []

    def with_market_cap(listing: SecurityListing) -> Optional[SecurityListing]:
        try:
            market_cap = market_cap_provider(listing.symbol)
        except Exception:
            return None
        if market_cap is None:
            return None
        return replace(listing, market_cap=int(market_cap))

    enriched: list[SecurityListing] = []
    if workers <= 1:
        for listing in listings:
            item = with_market_cap(listing)
            if item is not None:
                enriched.append(item)
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(with_market_cap, listing) for listing in listings]
            for future in as_completed(futures):
                item = future.result()
                if item is not None:
                    enriched.append(item)

    enriched.sort(key=lambda item: (-(item.market_cap or 0), item.symbol))
    return enriched[:limit]


def score_rating(rating: str) -> int:
    return RATING_SCORES.get(rating, 0)


def rank_universe_run_results(
    results: Sequence[UniverseRunResult],
) -> list[UniverseRunResult]:
    successful = [item for item in results if item.error is None and item.score > 0]
    return sorted(
        successful,
        key=lambda item: (-item.score, -(item.market_cap or 0), item.ticker),
    )


def _summary_payload(summary: UniverseSummary) -> dict:
    return {
        "best_ticker": summary.best_ticker,
        "ranked_results": [asdict(item) for item in summary.ranked_results],
        "failed_results": [asdict(item) for item in summary.failed_results],
    }


def write_universe_summary(
    results: Sequence[UniverseRunResult],
    output_dir: Path,
) -> UniverseSummary:
    output_dir.mkdir(parents=True, exist_ok=True)
    ranked = rank_universe_run_results(results)
    failed = [item for item in results if item.error is not None]
    best_ticker = ranked[0].ticker if ranked else None
    summary = UniverseSummary(
        best_ticker=best_ticker,
        ranked_results=ranked,
        failed_results=failed,
        output_dir=output_dir,
    )

    (output_dir / "universe_summary.json").write_text(
        json.dumps(_summary_payload(summary), indent=2),
        encoding="utf-8",
    )

    lines = [
        "# NYSE/NASDAQ Top Tickers TradingAgents Summary",
        "",
        f"Best ticker: **{best_ticker or 'None'}**",
        "",
        "| Rank | Ticker | Rating | Score | Market Cap | Error |",
        "| ---: | --- | --- | ---: | ---: | --- |",
    ]
    for idx, item in enumerate(ranked, start=1):
        lines.append(
            f"| {idx} | {item.ticker} | {item.rating} | {item.score} | "
            f"{item.market_cap or ''} |  |"
        )
    for item in failed:
        lines.append(
            f"|  | {item.ticker} | {item.rating} | {item.score} | "
            f"{item.market_cap or ''} | {item.error or ''} |"
        )
    (output_dir / "universe_summary.md").write_text("\n".join(lines), encoding="utf-8")

    return summary


def run_top_nyse_nasdaq_universe(
    *,
    config: dict,
    trade_date: str,
    selected_analysts: Sequence[str],
    limit: int = 5000,
    listings_loader: Callable[[], Sequence[SecurityListing]] = load_nyse_nasdaq_company_listings,
    market_cap_provider: Callable[[str], Optional[int]] = fetch_yfinance_market_cap,
    graph_factory=None,
    output_dir: Optional[Path] = None,
    workers: int = 8,
) -> UniverseSummary:
    """Run TradingAgents for the top NYSE/NASDAQ companies and rank outputs."""
    if graph_factory is None:
        from tradingagents.graph.trading_graph import TradingAgentsGraph

        graph_factory = TradingAgentsGraph

    listings = listings_loader()
    selected = select_top_companies_by_market_cap(
        listings,
        limit=limit,
        market_cap_provider=market_cap_provider,
        workers=workers,
    )

    graph = graph_factory(list(selected_analysts), config=config, debug=False)
    results: list[UniverseRunResult] = []
    for listing in selected:
        try:
            final_state, signal = graph.propagate(
                listing.symbol,
                trade_date,
                asset_type="stock",
            )
            final_decision = final_state.get("final_trade_decision", "")
            rating = signal or parse_rating(final_decision)
            results.append(
                UniverseRunResult(
                    ticker=listing.symbol,
                    rating=rating,
                    score=score_rating(rating),
                    market_cap=listing.market_cap,
                    final_decision=final_decision,
                )
            )
        except Exception as exc:
            results.append(
                UniverseRunResult(
                    ticker=listing.symbol,
                    rating="Error",
                    score=0,
                    market_cap=listing.market_cap,
                    final_decision="",
                    error=str(exc),
                )
            )

    summary_dir = output_dir or (
        Path(config["results_dir"]) / "universe" / "nyse_nasdaq_top" / trade_date
    )
    return write_universe_summary(results, Path(summary_dir))
