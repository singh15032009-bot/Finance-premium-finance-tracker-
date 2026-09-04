"""Pro-tier analytics: dividend income, sector/risk exposure, benchmark.

Every figure here is derived from real Yahoo Finance data, on the same terms as
the rest of the app: when a value is missing, it is reported as missing rather
than defaulted to zero. A holding with no dividend data is "not paying or not
reported", never "$0.00 income" stated as fact.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd
import yfinance as yf

from . import market
from .calculations import _dec, _money, _pct, price
from .errors import MarketUnavailableError

BENCHMARKS = {
    "^GSPC": "S&P 500",
    "^IXIC": "NASDAQ Composite",
    "^DJI": "Dow Jones Industrial Average",
}
DEFAULT_BENCHMARK = "^GSPC"

# Spreading evenly across this many sectors scores 100 on the diversification
# scale. Eight of the eleven GICS sectors is a defensible "well diversified".
WELL_DIVERSIFIED_SECTORS = 8


# ------------------------------------------------------------- dividends --


def dividend_summary(holdings: list[dict], quotes: dict[str, dict]) -> dict:
    """Projected annual dividend income for the portfolio.

    Uses Yahoo's forward ``dividendRate`` (annual payout per share) where
    available, falling back to the trailing annual rate.
    """
    symbols = {h["symbol"] for h in holdings}
    profiles = market.get_profiles(symbols)

    rows: list[dict] = []
    total_income = Decimal("0")
    total_cost = Decimal("0")
    total_value = Decimal("0")
    payers = 0

    for h in holdings:
        symbol = h["symbol"]
        profile = profiles.get(symbol) or {}
        quantity = _dec(h["quantity"])
        cost = _money(quantity * price(h["purchase_price"]))
        total_cost += cost

        quote = quotes.get(symbol)
        value = _money(quantity * price(quote["price"])) if quote else None
        if value is not None:
            total_value += value

        rate = profile.get("dividendRate") or profile.get("trailingAnnualDividendRate")
        row = {
            "symbol": symbol,
            "name": profile.get("shortName") or profile.get("longName") or h.get("name"),
            "quantity": float(quantity),
            "dividend_rate": None,
            "annual_income": None,
            "yield_on_cost": None,
            "current_yield": None,
            "ex_dividend_date": None,
            "ex_dividend_is_upcoming": False,
            "pays_dividend": False,
        }

        if rate:
            per_share = price(rate)
            income = _money(quantity * per_share)
            total_income += income
            payers += 1
            row.update({
                "dividend_rate": float(per_share),
                "annual_income": float(income),
                "pays_dividend": True,
                # Yield on cost: income against what you actually paid. The
                # number that tells you whether the position is working.
                "yield_on_cost": float(_pct(income / cost * 100)) if cost > 0 else None,
                "current_yield": float(_pct(income / value * 100)) if value and value > 0 else None,
            })

        # Yahoo's exDividendDate is whichever ex-date it last recorded, which
        # is frequently in the past. Report it with an explicit flag rather
        # than presenting a stale date as the next payment.
        ex_date = profile.get("exDividendDate")
        if ex_date:
            try:
                parsed = datetime.fromtimestamp(ex_date, tz=timezone.utc).date()
                row["ex_dividend_date"] = parsed.isoformat()
                row["ex_dividend_is_upcoming"] = parsed >= datetime.now(timezone.utc).date()
            except (OSError, ValueError, TypeError):
                pass

        rows.append(row)

    rows.sort(key=lambda r: (r["annual_income"] is None, -(r["annual_income"] or 0)))

    # Only genuinely future dates go in the "upcoming" list.
    upcoming = sorted(
        [r for r in rows if r.get("ex_dividend_is_upcoming")],
        key=lambda r: r["ex_dividend_date"],
    )

    return {
        "holdings": rows,
        "totals": {
            "annual_income": float(_money(total_income)),
            "monthly_income": float(_money(total_income / 12)) if total_income else 0.0,
            "yield_on_cost": float(_pct(total_income / total_cost * 100)) if total_cost > 0 else None,
            "portfolio_yield": float(_pct(total_income / total_value * 100)) if total_value > 0 else None,
            "paying_holdings": payers,
            "total_holdings": len(rows),
        },
        "upcoming_ex_dates": upcoming[:5],
        "source": "Yahoo Finance (yfinance)",
    }


# ---------------------------------------------------------------- sectors --


def _weighted(rows: list[tuple[str, Decimal]], total: Decimal) -> list[dict]:
    """Group (label, value) pairs into percentage buckets summing to 100."""
    if total <= 0:
        return []
    buckets: dict[str, Decimal] = {}
    for label, value in rows:
        buckets[label] = buckets.get(label, Decimal("0")) + value
    out = [
        {"label": label, "value": float(_money(value)),
         "pct": float(_pct(value / total * 100))}
        for label, value in buckets.items()
    ]
    out.sort(key=lambda d: d["value"], reverse=True)
    return out


def sector_breakdown(holdings: list[dict], quotes: dict[str, dict]) -> dict:
    """Sector, industry and country exposure, plus beta and a diversification score."""
    symbols = {h["symbol"] for h in holdings}
    profiles = market.get_profiles(symbols)

    sector_rows: list[tuple[str, Decimal]] = []
    country_rows: list[tuple[str, Decimal]] = []
    industry_rows: list[tuple[str, Decimal]] = []
    beta_weighted = Decimal("0")
    beta_covered = Decimal("0")
    total_value = Decimal("0")
    unknown: list[str] = []

    for h in holdings:
        symbol = h["symbol"]
        quote = quotes.get(symbol)
        if not quote:
            continue
        value = _money(_dec(h["quantity"]) * price(quote["price"]))
        total_value += value

        profile = profiles.get(symbol) or {}
        # ETFs report a fund category rather than a sector.
        sector = profile.get("sector") or profile.get("category")
        if not sector:
            sector = "Unclassified"
            unknown.append(symbol)
        sector_rows.append((sector, value))
        country_rows.append((profile.get("country") or "Unknown", value))
        industry_rows.append((profile.get("industry") or sector, value))

        beta = profile.get("beta")
        if beta is not None:
            try:
                beta_weighted += _dec(beta) * value
                beta_covered += value
            except (TypeError, ValueError):
                pass

    sectors = _weighted(sector_rows, total_value)

    # Diversification score from the effective number of sectors (1/HHI),
    # measured against WELL_DIVERSIFIED_SECTORS.
    #
    # Deliberately NOT normalised against the minimum HHI for the sector count
    # you happen to hold: that scores an even two-sector split ~100/100, which
    # flatters a concentrated portfolio. Holding two sectors is not
    # diversification no matter how evenly you split them.
    score = None
    effective_sectors = None
    if sectors:
        hhi = sum((Decimal(str(s["pct"])) / 100) ** 2 for s in sectors)
        if hhi > 0:
            effective = 1 / hhi
            effective_sectors = float(round(effective, 2))
            span = Decimal(WELL_DIVERSIFIED_SECTORS) - 1
            score = float(_pct(max(Decimal(0), (effective - 1)) / span * 100))
            score = max(0.0, min(100.0, score))

    portfolio_beta = (
        float(round(beta_weighted / beta_covered, 3)) if beta_covered > 0 else None
    )

    top = sectors[0] if sectors else None

    return {
        "sectors": sectors,
        "countries": _weighted(country_rows, total_value),
        "industries": _weighted(industry_rows, total_value)[:8],
        "total_value": float(_money(total_value)),
        "portfolio_beta": portfolio_beta,
        "beta_coverage_pct": float(_pct(beta_covered / total_value * 100)) if total_value > 0 else None,
        "diversification_score": score,
        "effective_sectors": effective_sectors,
        "well_diversified_sectors": WELL_DIVERSIFIED_SECTORS,
        "concentration_warning": (
            f"{top['pct']:.1f}% of your portfolio sits in {top['label']}."
            if top and top["pct"] >= 40 else None
        ),
        "unclassified_symbols": sorted(set(unknown)),
        "source": "Yahoo Finance (yfinance)",
    }


# -------------------------------------------------------------- benchmark --


def benchmark_comparison(
    positions: dict[str, float], period: str = "1y", benchmark: str = DEFAULT_BENCHMARK
) -> dict:
    """Compare portfolio growth against an index over the same window.

    Both series are rebased to 100 at the start of the period so they are
    directly comparable regardless of portfolio size.
    """
    if benchmark not in BENCHMARKS:
        benchmark = DEFAULT_BENCHMARK

    portfolio = market.get_portfolio_history(positions, period=period)
    points = portfolio.get("points") or []
    if len(points) < 2:
        return {
            "period": period,
            "benchmark": benchmark,
            "benchmark_name": BENCHMARKS[benchmark],
            "points": [],
            "portfolio_return_pct": None,
            "benchmark_return_pct": None,
            "outperformance_pct": None,
            "missing": portfolio.get("missing", []),
        }

    try:
        index_hist = yf.Ticker(benchmark).history(period=period, interval="1d")
    except Exception as exc:
        raise MarketUnavailableError(
            f"Could not load {BENCHMARKS[benchmark]} data from Yahoo Finance.",
            details={"benchmark": benchmark, "reason": str(exc)},
        ) from exc

    if index_hist is None or index_hist.empty or "Close" not in index_hist:
        raise MarketUnavailableError(
            f"Yahoo Finance returned no data for {BENCHMARKS[benchmark]}.",
            details={"benchmark": benchmark},
        )

    index_closes = index_hist["Close"].dropna()
    index_closes.index = pd.to_datetime(index_closes.index).tz_localize(None).normalize()

    port = pd.Series(
        [p["value"] for p in points],
        index=pd.to_datetime([p["date"] for p in points]),
    )

    # Align on the dates both series share, so a market holiday in one does not
    # shift the comparison.
    frame = pd.DataFrame({"portfolio": port, "benchmark": index_closes}).dropna()
    if len(frame) < 2:
        return {
            "period": period,
            "benchmark": benchmark,
            "benchmark_name": BENCHMARKS[benchmark],
            "points": [],
            "portfolio_return_pct": None,
            "benchmark_return_pct": None,
            "outperformance_pct": None,
            "missing": portfolio.get("missing", []),
        }

    base_p = float(frame["portfolio"].iloc[0])
    base_b = float(frame["benchmark"].iloc[0])

    series = [
        {
            "date": idx.date().isoformat(),
            "portfolio": round(float(row.portfolio) / base_p * 100, 3),
            "benchmark": round(float(row.benchmark) / base_b * 100, 3),
        }
        for idx, row in frame.iterrows()
    ]

    port_return = (float(frame["portfolio"].iloc[-1]) / base_p - 1) * 100
    bench_return = (float(frame["benchmark"].iloc[-1]) / base_b - 1) * 100

    return {
        "period": period,
        "benchmark": benchmark,
        "benchmark_name": BENCHMARKS[benchmark],
        "points": series,
        "portfolio_return_pct": float(_pct(port_return)),
        "benchmark_return_pct": float(_pct(bench_return)),
        "outperformance_pct": float(_pct(port_return - bench_return)),
        "beating_benchmark": port_return > bench_return,
        "missing": portfolio.get("missing", []),
        "source": "Yahoo Finance (yfinance)",
    }


# ----------------------------------------------------------------- export --


def portfolio_csv(portfolio: dict) -> str:
    """Flatten the computed portfolio to CSV (Pro export)."""
    import csv
    import io

    buf = io.StringIO()
    fields = ["symbol", "name", "quantity", "purchase_price", "purchase_date",
              "invested", "current_price", "current_value", "profit_loss",
              "return_pct", "currency", "market_time"]
    writer = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for row in portfolio["holdings"]:
        writer.writerow(row)

    t = portfolio["totals"]
    writer.writerow({
        "symbol": "TOTAL",
        "invested": t["total_invested"],
        "current_value": t["total_value"],
        "profit_loss": t["total_profit_loss"],
        "return_pct": t["total_return_pct"],
    })
    return buf.getvalue()
