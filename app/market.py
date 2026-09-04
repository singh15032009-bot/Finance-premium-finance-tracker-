"""Yahoo Finance data layer.

Hard rule for this module: **it never invents a price.** Every number that
leaves here came out of Yahoo Finance via yfinance. If a real price cannot be
obtained the caller gets an exception or an explicit "unavailable" marker --
never a placeholder, a zero, or a last-known-guess presented as live.

The one nuance is the stale cache: if Yahoo goes down we may serve a price we
previously fetched *from Yahoo*, but it is flagged ``stale=True`` and keeps its
original ``market_time`` so the UI can say how old it is.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Iterable

import pandas as pd
import yfinance as yf

from .errors import InvalidSymbolError, MarketUnavailableError

log = logging.getLogger("wealthtrack.market")

# yfinance chats a lot on stderr for bad tickers; we surface those as proper
# errors ourselves, so quieten its own logger.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

# How long a fetched quote is considered fresh enough to reuse.
QUOTE_TTL_SECONDS = 60
# How long a cached quote may still be served as a *stale* fallback when Yahoo
# is unreachable. Beyond this we refuse to answer rather than mislead.
STALE_FALLBACK_SECONDS = 60 * 60 * 12

_VALID_PERIODS = {"1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Quote:
    """A real, Yahoo-sourced price for one symbol."""

    symbol: str
    price: float
    currency: str | None = None
    short_name: str | None = None
    exchange: str | None = None
    previous_close: float | None = None
    # When Yahoo says the market last traded this instrument.
    market_time: datetime | None = None
    # When *we* pulled it off Yahoo.
    fetched_at: datetime = field(default_factory=_utcnow)
    # True when served from cache because Yahoo was unreachable on this attempt.
    stale: bool = False

    @property
    def day_change(self) -> float | None:
        if self.previous_close in (None, 0):
            return None
        return self.price - self.previous_close

    @property
    def day_change_pct(self) -> float | None:
        if self.previous_close in (None, 0):
            return None
        return (self.price - self.previous_close) / self.previous_close * 100.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["market_time"] = self.market_time.isoformat() if self.market_time else None
        d["fetched_at"] = self.fetched_at.isoformat()
        d["day_change"] = self.day_change
        d["day_change_pct"] = self.day_change_pct
        return d


class _QuoteCache:
    """Small thread-safe TTL cache so a page refresh does not hammer Yahoo."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, Quote] = {}

    def get_fresh(self, symbol: str) -> Quote | None:
        with self._lock:
            q = self._entries.get(symbol)
        if q is None:
            return None
        age = (_utcnow() - q.fetched_at).total_seconds()
        return q if age < QUOTE_TTL_SECONDS else None

    def get_stale(self, symbol: str) -> Quote | None:
        """Return a previously-real price for degraded mode, if recent enough."""
        with self._lock:
            q = self._entries.get(symbol)
        if q is None:
            return None
        age = (_utcnow() - q.fetched_at).total_seconds()
        if age > STALE_FALLBACK_SECONDS:
            return None
        stale_copy = Quote(**{**asdict(q), "stale": True})
        stale_copy.market_time = q.market_time
        stale_copy.fetched_at = q.fetched_at
        return stale_copy

    def put(self, quote: Quote) -> None:
        with self._lock:
            self._entries[quote.symbol] = quote

    def clear(self, symbol: str | None = None) -> None:
        with self._lock:
            if symbol is None:
                self._entries.clear()
            else:
                self._entries.pop(symbol, None)


_cache = _QuoteCache()


def normalise_symbol(symbol: str) -> str:
    if not symbol or not symbol.strip():
        raise InvalidSymbolError("A ticker symbol is required.")
    cleaned = symbol.strip().upper()
    if len(cleaned) > 20:
        raise InvalidSymbolError(f"'{symbol}' is not a valid ticker symbol.")
    return cleaned


def _first(md: dict, *keys):
    for k in keys:
        v = md.get(k)
        if v is not None:
            return v
    return None


def _to_datetime(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        ts = value.to_pydatetime()
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


def _fetch_quote_uncached(symbol: str) -> Quote:
    """Pull one real quote from Yahoo Finance.

    Raises InvalidSymbolError if Yahoo has no such instrument, or
    MarketUnavailableError if we could not reach/parse Yahoo at all.
    """
    ticker = yf.Ticker(symbol)

    metadata: dict = {}
    metadata_failed: Exception | None = None
    try:
        metadata = ticker.get_history_metadata() or {}
    except Exception as exc:  # network error, parse error, ...
        metadata_failed = exc
        log.debug("history metadata failed for %s: %s", symbol, exc)

    price = _first(metadata, "regularMarketPrice")
    market_time = _to_datetime(_first(metadata, "regularMarketTime"))

    # Fallback 1: fast_info (a different Yahoo endpoint).
    if price is None:
        try:
            fi = ticker.fast_info
            price = fi.get("lastPrice")
            if not metadata:
                metadata = {
                    "currency": fi.get("currency"),
                    "exchangeName": fi.get("exchange"),
                    "previousClose": fi.get("previousClose"),
                }
        except Exception as exc:
            log.debug("fast_info failed for %s: %s", symbol, exc)

    # Fallback 2: the most recent daily close from price history.
    if price is None:
        try:
            hist = ticker.history(period="5d", interval="1d")
        except Exception as exc:
            raise MarketUnavailableError(
                f"Could not reach Yahoo Finance for {symbol}. Check your internet "
                f"connection and try again.",
                details={"symbol": symbol, "reason": str(exc)},
            ) from exc
        if hist is not None and not hist.empty and "Close" in hist:
            closes = hist["Close"].dropna()
            if not closes.empty:
                price = float(closes.iloc[-1])
                market_time = _to_datetime(closes.index[-1])

    if price is None:
        # Yahoo answered but has no instrument/price here. Distinguish an
        # unknown ticker from a total outage: metadata came back as a dict
        # (Yahoo responded) => it is the symbol that is wrong.
        if metadata_failed is not None and not metadata:
            raise MarketUnavailableError(
                f"Could not reach Yahoo Finance for {symbol}. Check your internet "
                f"connection and try again.",
                details={"symbol": symbol, "reason": str(metadata_failed)},
            )
        raise InvalidSymbolError(
            f"'{symbol}' is not a symbol Yahoo Finance recognises. "
            f"Check the ticker (for example AAPL, MSFT, TSLA).",
            details={"symbol": symbol},
        )

    try:
        price = float(price)
    except (TypeError, ValueError) as exc:
        raise MarketUnavailableError(
            f"Yahoo Finance returned an unreadable price for {symbol}.",
            details={"symbol": symbol, "raw": repr(price)},
        ) from exc

    if price <= 0:
        raise MarketUnavailableError(
            f"Yahoo Finance returned a non-positive price for {symbol}.",
            details={"symbol": symbol, "raw": price},
        )

    prev = _first(metadata, "previousClose", "chartPreviousClose", "regularMarketPreviousClose")
    quote = Quote(
        symbol=symbol,
        price=price,
        currency=_first(metadata, "currency"),
        short_name=_first(metadata, "shortName", "longName"),
        exchange=_first(metadata, "fullExchangeName", "exchangeName"),
        previous_close=float(prev) if prev is not None else None,
        market_time=market_time,
        fetched_at=_utcnow(),
        stale=False,
    )
    return quote


def get_quote(symbol: str, *, force_refresh: bool = False) -> Quote:
    """Return a real Yahoo quote for ``symbol``.

    Uses a short TTL cache. If Yahoo is unreachable but we hold a recent real
    price, that price is returned flagged ``stale=True``.
    """
    symbol = normalise_symbol(symbol)

    if not force_refresh:
        cached = _cache.get_fresh(symbol)
        if cached is not None:
            return cached

    try:
        quote = _fetch_quote_uncached(symbol)
    except InvalidSymbolError:
        # A bad ticker is a bad ticker; never paper over it with a cached price.
        raise
    except MarketUnavailableError:
        stale = _cache.get_stale(symbol)
        if stale is not None:
            log.warning("Serving stale cached price for %s (Yahoo unreachable)", symbol)
            return stale
        raise

    _cache.put(quote)
    return quote


def get_quotes(
    symbols: Iterable[str], *, force_refresh: bool = False
) -> tuple[dict[str, Quote], dict[str, dict]]:
    """Fetch many quotes concurrently.

    Returns ``(quotes, errors)`` where ``errors`` maps symbol -> error dict for
    the symbols that could not be priced. Callers must treat a symbol appearing
    in ``errors`` as having *no* price, not a price of zero.
    """
    unique = sorted({normalise_symbol(s) for s in symbols})
    quotes: dict[str, Quote] = {}
    errors: dict[str, dict] = {}
    if not unique:
        return quotes, errors

    def _one(sym: str):
        try:
            return sym, get_quote(sym, force_refresh=force_refresh), None
        except (InvalidSymbolError, MarketUnavailableError) as exc:
            return sym, None, exc.to_dict()
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("Unexpected error pricing %s", sym)
            return sym, None, MarketUnavailableError(
                f"Unexpected error pricing {sym}: {exc}"
            ).to_dict()

    with ThreadPoolExecutor(max_workers=min(8, len(unique))) as pool:
        for sym, quote, err in pool.map(_one, unique):
            if quote is not None:
                quotes[sym] = quote
            else:
                errors[sym] = err

    return quotes, errors


def validate_symbol(symbol: str) -> Quote:
    """Confirm a ticker really exists on Yahoo, returning its live quote."""
    return get_quote(symbol, force_refresh=False)


def get_history(symbol: str, period: str = "1mo", interval: str = "1d") -> list[dict]:
    """Real historical closes for one symbol."""
    symbol = normalise_symbol(symbol)
    if period not in _VALID_PERIODS:
        raise InvalidSymbolError(
            f"Unsupported period '{period}'. Use one of: {', '.join(sorted(_VALID_PERIODS))}."
        )
    try:
        hist = yf.Ticker(symbol).history(period=period, interval=interval)
    except Exception as exc:
        raise MarketUnavailableError(
            f"Could not load price history for {symbol} from Yahoo Finance.",
            details={"symbol": symbol, "reason": str(exc)},
        ) from exc

    if hist is None or hist.empty or "Close" not in hist:
        raise InvalidSymbolError(
            f"Yahoo Finance has no price history for '{symbol}'.",
            details={"symbol": symbol},
        )

    closes = hist["Close"].dropna()
    return [
        {"date": idx.date().isoformat(), "close": float(val)}
        for idx, val in closes.items()
    ]


def get_portfolio_history(
    positions: dict[str, float], period: str = "1mo"
) -> dict:
    """Portfolio market value over time, from real historical closes.

    ``positions`` maps symbol -> total quantity held. Symbols whose history
    cannot be loaded are reported in ``missing`` and excluded from the series.
    """
    if not positions:
        return {"period": period, "points": [], "missing": [], "symbols": []}

    series: dict[str, pd.Series] = {}
    missing: list[dict] = []

    def _one(sym: str):
        try:
            hist = yf.Ticker(sym).history(period=period, interval="1d")
            if hist is None or hist.empty or "Close" not in hist:
                return sym, None, "no history returned by Yahoo Finance"
            return sym, hist["Close"].dropna(), None
        except Exception as exc:
            return sym, None, str(exc)

    symbols = sorted(positions)
    with ThreadPoolExecutor(max_workers=min(8, len(symbols))) as pool:
        for sym, closes, err in pool.map(_one, symbols):
            if closes is None or closes.empty:
                missing.append({"symbol": sym, "reason": err or "no data"})
            else:
                closes.index = pd.to_datetime(closes.index).tz_localize(None).normalize()
                series[sym] = closes

    if not series:
        return {"period": period, "points": [], "missing": missing, "symbols": []}

    frame = pd.DataFrame(series).sort_index()
    # Carry the last real close forward across holidays/half-days so every
    # symbol contributes on every trading day present in the union of indexes.
    frame = frame.ffill().dropna(how="all")

    total = None
    for sym, closes in frame.items():
        contribution = closes * float(positions[sym])
        total = contribution if total is None else total.add(contribution, fill_value=0.0)

    points = [
        {"date": idx.date().isoformat(), "value": round(float(val), 2)}
        for idx, val in total.items()
        if pd.notna(val)
    ]
    return {
        "period": period,
        "points": points,
        "missing": missing,
        "symbols": sorted(series),
    }


def clear_cache(symbol: str | None = None) -> None:
    _cache.clear(symbol)
    if symbol is None:
        with _profile_lock:
            _profiles.clear()


# --------------------------------------------------------------- profiles --

# Company profile data (sector, dividend rate, beta). Slow to fetch (~2s per
# symbol) but slow to change, so it gets its own long-lived cache.
PROFILE_TTL_SECONDS = 60 * 60 * 6

_profiles: dict[str, tuple[dict, datetime]] = {}
_profile_lock = threading.Lock()

_PROFILE_KEYS = (
    "sector", "industry", "country", "quoteType", "category",
    "dividendRate", "dividendYield", "trailingAnnualDividendRate",
    "exDividendDate", "beta", "marketCap", "longName", "shortName", "currency",
)


def get_profile(symbol: str) -> dict:
    """Company profile for one symbol. Returns {} if Yahoo has no profile.

    Never raises for a missing profile -- profile data is enrichment, and a
    holding without it should still appear in every other view.
    """
    symbol = normalise_symbol(symbol)

    with _profile_lock:
        cached = _profiles.get(symbol)
    if cached and (_utcnow() - cached[1]).total_seconds() < PROFILE_TTL_SECONDS:
        return cached[0]

    try:
        info = yf.Ticker(symbol).info or {}
    except Exception as exc:
        log.debug("profile fetch failed for %s: %s", symbol, exc)
        return cached[0] if cached else {}

    profile = {k: info.get(k) for k in _PROFILE_KEYS}
    profile["symbol"] = symbol

    with _profile_lock:
        _profiles[symbol] = (profile, _utcnow())
    return profile


def get_profiles(symbols: Iterable[str]) -> dict[str, dict]:
    unique = sorted({normalise_symbol(s) for s in symbols})
    if not unique:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(unique))) as pool:
        return dict(zip(unique, pool.map(get_profile, unique)))
