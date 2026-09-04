"""Yahoo Finance failure handling, with yfinance stubbed so failures are exact.

The property under test throughout: when a real price cannot be obtained,
WealthTrack raises or reports -- it never returns a made-up number.
"""

import pytest

from app import market
from app.errors import InvalidSymbolError, MarketUnavailableError


class FakeTicker:
    """Stand-in for yf.Ticker with configurable failure modes."""

    def __init__(self, metadata=None, fast=None, history_df=None,
                 metadata_exc=None, fast_exc=None, history_exc=None):
        self._metadata, self._fast, self._history = metadata, fast, history_df
        self._metadata_exc, self._fast_exc, self._history_exc = (
            metadata_exc, fast_exc, history_exc)

    def get_history_metadata(self):
        if self._metadata_exc:
            raise self._metadata_exc
        return self._metadata

    @property
    def fast_info(self):
        if self._fast_exc:
            raise self._fast_exc
        return self._fast

    def history(self, **kwargs):
        if self._history_exc:
            raise self._history_exc
        return self._history


@pytest.fixture(autouse=True)
def clear_cache():
    market.clear_cache()
    yield
    market.clear_cache()


def patch_ticker(monkeypatch, ticker):
    monkeypatch.setattr(market.yf, "Ticker", lambda symbol: ticker)


# ------------------------------------------------------------ happy path --


def test_metadata_is_the_primary_source(monkeypatch):
    patch_ticker(monkeypatch, FakeTicker(metadata={
        "regularMarketPrice": 319.51, "currency": "USD",
        "shortName": "Apple Inc.", "previousClose": 317.00,
    }))
    q = market.get_quote("AAPL")
    assert q.price == 319.51
    assert q.short_name == "Apple Inc."
    assert q.previous_close == 317.00
    assert q.stale is False


def test_falls_back_to_fast_info_when_metadata_has_no_price(monkeypatch):
    patch_ticker(monkeypatch, FakeTicker(
        metadata={}, fast={"lastPrice": 250.00, "currency": "USD",
                           "exchange": "NMS", "previousClose": 248.0}))
    assert market.get_quote("MSFT").price == 250.00


# ------------------------------------------------------------- failures --


def test_network_failure_raises_market_unavailable(monkeypatch):
    boom = ConnectionError("getaddrinfo failed")
    patch_ticker(monkeypatch, FakeTicker(
        metadata_exc=boom, fast_exc=boom, history_exc=boom))
    with pytest.raises(MarketUnavailableError) as exc:
        market.get_quote("AAPL")
    assert "Could not reach Yahoo Finance" in exc.value.message


def test_unknown_symbol_raises_invalid_symbol(monkeypatch):
    # Yahoo answered, it just has no such instrument.
    patch_ticker(monkeypatch, FakeTicker(
        metadata={"YF repair?": False},
        fast_exc=KeyError("currentTradingPeriod"),
        history_df=None))
    with pytest.raises(InvalidSymbolError):
        market.get_quote("NOTAREAL")


def test_zero_or_negative_price_is_refused(monkeypatch):
    patch_ticker(monkeypatch, FakeTicker(metadata={"regularMarketPrice": 0.0}))
    with pytest.raises(MarketUnavailableError):
        market.get_quote("WEIRD")


def test_unparseable_price_is_refused(monkeypatch):
    patch_ticker(monkeypatch, FakeTicker(metadata={"regularMarketPrice": "n/a"}))
    with pytest.raises(MarketUnavailableError):
        market.get_quote("WEIRD")


# --------------------------------------------------------- stale fallback --


def test_stale_cache_serves_a_real_previous_price_when_yahoo_goes_down(monkeypatch):
    good = FakeTicker(metadata={"regularMarketPrice": 319.51, "currency": "USD"})
    patch_ticker(monkeypatch, good)
    first = market.get_quote("AAPL")
    assert first.stale is False

    boom = ConnectionError("network down")
    patch_ticker(monkeypatch, FakeTicker(
        metadata_exc=boom, fast_exc=boom, history_exc=boom))
    second = market.get_quote("AAPL", force_refresh=True)

    # Same real price we fetched earlier, clearly flagged and keeping its
    # original timestamp -- not a fresh invention.
    assert second.price == 319.51
    assert second.stale is True
    assert second.fetched_at == first.fetched_at


def test_no_stale_fallback_for_an_invalid_symbol(monkeypatch):
    """A bad ticker must never be papered over with a cached price."""
    patch_ticker(monkeypatch, FakeTicker(metadata={"regularMarketPrice": 100.0}))
    market.get_quote("TYPO")

    patch_ticker(monkeypatch, FakeTicker(
        metadata={}, fast_exc=KeyError("currentTradingPeriod"), history_df=None))
    with pytest.raises(InvalidSymbolError):
        market.get_quote("TYPO", force_refresh=True)


def test_stale_fallback_expires(monkeypatch):
    from datetime import timedelta

    patch_ticker(monkeypatch, FakeTicker(metadata={"regularMarketPrice": 100.0}))
    q = market.get_quote("AAPL")
    # Age the cached entry past the fallback window.
    q.fetched_at = q.fetched_at - timedelta(seconds=market.STALE_FALLBACK_SECONDS + 60)

    boom = ConnectionError("down")
    patch_ticker(monkeypatch, FakeTicker(
        metadata_exc=boom, fast_exc=boom, history_exc=boom))
    with pytest.raises(MarketUnavailableError):
        market.get_quote("AAPL", force_refresh=True)


# ---------------------------------------------------------------- batch --


def test_batch_isolates_one_bad_symbol_from_the_good_ones(monkeypatch):
    def fake_ticker(symbol):
        if symbol == "BAD":
            return FakeTicker(metadata={}, fast_exc=KeyError("x"), history_df=None)
        return FakeTicker(metadata={"regularMarketPrice": 100.0, "currency": "USD"})

    monkeypatch.setattr(market.yf, "Ticker", fake_ticker)
    quotes, errors = market.get_quotes(["AAPL", "BAD", "MSFT"])
    assert set(quotes) == {"AAPL", "MSFT"}
    assert errors["BAD"]["code"] == "invalid_symbol"


# ------------------------------------------------------------ symbol io --


@pytest.mark.parametrize("raw,expected", [("aapl", "AAPL"), ("  msft  ", "MSFT")])
def test_symbols_are_normalised(raw, expected):
    assert market.normalise_symbol(raw) == expected


@pytest.mark.parametrize("bad", ["", "   ", "A" * 21])
def test_bad_symbol_shapes_rejected(bad):
    with pytest.raises(InvalidSymbolError):
        market.normalise_symbol(bad)
