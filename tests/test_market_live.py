"""Live Yahoo Finance tests.

These hit the real service, so they need an internet connection. Run just
these with:  pytest -m live
Skip them with:  pytest -m "not live"

They deliberately assert on *properties* (price is a positive float, timestamp
is recent, invalid tickers raise) rather than on specific prices, which move.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import market
from app.errors import InvalidSymbolError

pytestmark = pytest.mark.live

REAL_SYMBOLS = ["AAPL", "MSFT", "TSLA"]


@pytest.mark.parametrize("symbol", REAL_SYMBOLS)
def test_real_quote_has_a_sane_live_price(symbol):
    q = market.get_quote(symbol, force_refresh=True)
    assert q.symbol == symbol
    assert isinstance(q.price, float)
    assert q.price > 0
    assert q.currency, "Yahoo should tell us the currency"
    assert q.stale is False


@pytest.mark.parametrize("symbol", REAL_SYMBOLS)
def test_real_quote_carries_a_recent_market_timestamp(symbol):
    q = market.get_quote(symbol, force_refresh=True)
    assert q.market_time is not None, "we should know when this price is from"
    age = datetime.now(timezone.utc) - q.market_time
    # Generous: covers weekends and market holidays.
    assert age < timedelta(days=7), f"{symbol} market time is {age} old"
    assert q.fetched_at <= datetime.now(timezone.utc) + timedelta(seconds=5)


def test_invalid_symbol_raises_rather_than_inventing_a_price():
    with pytest.raises(InvalidSymbolError):
        market.get_quote("NOTAREALTICKER123", force_refresh=True)


@pytest.mark.parametrize("bad", ["ZZZZZZZZ", "123FAKE456", "!!!"])
def test_various_invalid_symbols_rejected(bad):
    with pytest.raises(InvalidSymbolError):
        market.get_quote(bad, force_refresh=True)


def test_empty_symbol_rejected():
    with pytest.raises(InvalidSymbolError):
        market.get_quote("   ")


def test_batch_quotes_separates_good_from_bad():
    quotes, errors = market.get_quotes(
        ["AAPL", "MSFT", "NOTAREALTICKER123"], force_refresh=True)
    assert set(quotes) == {"AAPL", "MSFT"}
    assert "NOTAREALTICKER123" in errors
    assert errors["NOTAREALTICKER123"]["code"] == "invalid_symbol"
    assert all(q.price > 0 for q in quotes.values())


def test_real_history_is_ordered_and_positive():
    points = market.get_history("AAPL", period="1mo")
    assert len(points) > 5
    dates = [p["date"] for p in points]
    assert dates == sorted(dates), "history must be chronological"
    assert all(p["close"] > 0 for p in points)


def test_history_for_invalid_symbol_raises():
    with pytest.raises(InvalidSymbolError):
        market.get_history("NOTAREALTICKER123", period="1mo")


def test_portfolio_history_matches_quantities():
    positions = {"AAPL": 10, "MSFT": 5}
    result = market.get_portfolio_history(positions, period="1mo")
    assert result["points"], "expected a value series"
    assert not result["missing"]
    assert all(p["value"] > 0 for p in result["points"])
    dates = [p["date"] for p in result["points"]]
    assert dates == sorted(dates)


def test_portfolio_history_reports_missing_symbols():
    result = market.get_portfolio_history({"NOTAREALTICKER123": 1}, period="1mo")
    assert result["points"] == []
    assert result["missing"] and result["missing"][0]["symbol"] == "NOTAREALTICKER123"


def test_cache_returns_quickly_and_consistently():
    market.clear_cache()
    first = market.get_quote("AAPL")
    second = market.get_quote("AAPL")     # served from cache
    assert first.price == second.price
    assert first.fetched_at == second.fetched_at
