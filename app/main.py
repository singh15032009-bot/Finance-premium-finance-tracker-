"""WealthTrack API.

FastAPI app wiring the storage, Yahoo Finance and calculation layers together
and serving the dashboard.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, analytics, calculations, config, market, premium
from .errors import InvalidSymbolError, WealthTrackError
from .models import HoldingCreate, HoldingUpdate, PlanActivate
from .storage import PortfolioStore
from .premium import SubscriptionStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("wealthtrack")

BASE_DIR = config.BASE_DIR
STATIC_DIR = config.STATIC_DIR

# Point yfinance's timezone cache somewhere writable before the first Yahoo
# call. On serverless hosts $HOME is read-only and this otherwise blows up on
# the first request rather than at startup.
config.configure_yfinance_cache()

app = FastAPI(
    title="WealthTrack",
    version=__version__,
    description="Portfolio tracker backed by real Yahoo Finance data (no API key required).",
)

store = PortfolioStore()
subscription = SubscriptionStore()

if config.IS_EPHEMERAL:
    log.warning("Ephemeral storage in use (%s). %s",
                config.DATA_DIR_SOURCE, config.EPHEMERAL_WARNING)


@app.exception_handler(WealthTrackError)
async def _wealthtrack_error_handler(request: Request, exc: WealthTrackError):
    return JSONResponse(status_code=exc.status_code, content={"error": exc.to_dict()})


# ---------------------------------------------------------------- health --


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "server_time": datetime.now(timezone.utc).isoformat(),
        "data_file": str(store.path),
        "storage": {
            **config.storage_info(),
            "in_memory_only": store.in_memory_only,
        },
    }


# -------------------------------------------------------------- holdings --


@app.get("/api/holdings")
def list_holdings() -> dict:
    return {"holdings": store.list_holdings()}


@app.post("/api/holdings", status_code=201)
def add_holding(payload: HoldingCreate) -> dict:
    premium.check_holding_limit(subscription, len(store.list_holdings()))
    # Validate against Yahoo *before* saving, so a typo never reaches storage.
    quote = market.validate_symbol(payload.symbol)
    holding = store.add_holding(
        symbol=quote.symbol,
        quantity=payload.quantity,
        purchase_price=payload.purchase_price,
        purchase_date=payload.purchase_date,
        notes=payload.notes,
        name=quote.short_name,
    )
    return {"holding": holding, "quote": quote.to_dict()}


@app.put("/api/holdings/{holding_id}")
def update_holding(holding_id: str, payload: HoldingUpdate) -> dict:
    existing = store.get_holding(holding_id)
    fields = payload.model_dump(exclude_unset=True)

    if fields.get("symbol") and fields["symbol"] != existing["symbol"]:
        quote = market.validate_symbol(fields["symbol"])
        fields["symbol"] = quote.symbol
        fields["name"] = quote.short_name

    holding = store.update_holding(holding_id, **fields)
    return {"holding": holding}


@app.delete("/api/holdings/{holding_id}")
def delete_holding(holding_id: str) -> dict:
    removed = store.delete_holding(holding_id)
    return {"deleted": removed}


@app.delete("/api/holdings")
def clear_holdings() -> dict:
    return {"deleted_count": store.clear()}


# ------------------------------------------------------------- portfolio --


def _build_portfolio(force_refresh: bool = False) -> dict:
    holdings = store.list_holdings()
    symbols = {h["symbol"] for h in holdings}
    quotes, errors = market.get_quotes(symbols, force_refresh=force_refresh)
    quote_dicts = {s: q.to_dict() for s, q in quotes.items()}

    portfolio = calculations.compute_portfolio(holdings, quote_dicts, errors)
    portfolio["price_errors"] = errors
    portfolio["as_of"] = datetime.now(timezone.utc).isoformat()
    portfolio["source"] = "Yahoo Finance (yfinance)"
    portfolio["storage"] = {
        "ephemeral": config.IS_EPHEMERAL or store.in_memory_only,
    }
    # Never let someone believe a holding was saved when it was not.
    if config.IS_EPHEMERAL or store.in_memory_only:
        portfolio["warnings"].append(config.EPHEMERAL_WARNING)

    # Self-check the arithmetic on every response.
    problems = calculations.verify_totals(portfolio)
    portfolio["totals_verified"] = not problems
    if problems:
        log.error("Portfolio total verification FAILED: %s", problems)
        portfolio["verification_errors"] = problems
        portfolio["warnings"].append(
            "Internal check: portfolio totals did not reconcile. See verification_errors."
        )
    return portfolio


@app.get("/api/portfolio")
def get_portfolio() -> dict:
    return _build_portfolio(force_refresh=False)


@app.post("/api/portfolio/refresh")
def refresh_portfolio() -> dict:
    """Force a fresh pull from Yahoo Finance, bypassing the quote cache."""
    market.clear_cache()
    return _build_portfolio(force_refresh=True)


@app.get("/api/portfolio/history")
def portfolio_history(period: str = Query("1mo", description="1mo, 3mo, 6mo, 1y, ...")) -> dict:
    premium.check_history_period(subscription, period)
    return market.get_portfolio_history(store.positions(), period=period)


# --------------------------------------------------------------- premium --


@app.get("/api/plans")
def get_plans() -> dict:
    """Pricing, feature comparison and the caller's current entitlement."""
    return premium.pricing_payload(subscription)


@app.get("/api/subscription")
def get_subscription() -> dict:
    return {"subscription": subscription.state()}


@app.post("/api/subscription/trial")
def start_trial(payload: PlanActivate) -> dict:
    """Start the local free trial. No payment details are taken."""
    state = subscription.activate(payload.plan_id, trial=True, source="trial")
    return {"subscription": state, "message": f"Pro unlocked for {premium.TRIAL_DAYS} days."}


@app.post("/api/subscription/activate")
def activate_plan(payload: PlanActivate) -> dict:
    """Activate a plan locally.

    This is the hook a real payment provider would call after a successful
    charge. It does not itself take payment -- see /api/checkout.
    """
    state = subscription.activate(payload.plan_id, source="manual")
    return {"subscription": state, "message": f"{state['plan_name']} activated."}


@app.post("/api/subscription/cancel")
def cancel_plan() -> dict:
    return {"subscription": subscription.cancel(), "message": "Switched back to the free plan."}


@app.post("/api/checkout")
def checkout(payload: PlanActivate) -> JSONResponse:
    """Where a real checkout session would be created.

    Returns 501 rather than pretending: no payment processor is connected, and
    this endpoint deliberately accepts no card details.
    """
    plan = premium.PLANS.get(payload.plan_id)
    if not plan:
        return JSONResponse(status_code=400, content={
            "error": {"code": "validation_error",
                      "message": f"Unknown plan '{payload.plan_id}'.", "details": {}}})
    return JSONResponse(status_code=501, content={
        "error": {
            "code": "checkout_not_configured",
            "message": premium.CHECKOUT_NOTE,
            "details": {
                "plan": plan["id"],
                "price": plan["price"],
                "period": plan["period"],
                "next_step": "Connect a payment provider, then call "
                             "/api/subscription/activate from its success webhook.",
            },
        }})


@app.get("/api/premium/dividends")
def premium_dividends() -> dict:
    premium.require(subscription, "dividends")
    holdings = store.list_holdings()
    quotes, _ = market.get_quotes({h["symbol"] for h in holdings})
    return analytics.dividend_summary(holdings, {s: q.to_dict() for s, q in quotes.items()})


@app.get("/api/premium/sectors")
def premium_sectors() -> dict:
    premium.require(subscription, "sectors")
    holdings = store.list_holdings()
    quotes, _ = market.get_quotes({h["symbol"] for h in holdings})
    return analytics.sector_breakdown(holdings, {s: q.to_dict() for s, q in quotes.items()})


@app.get("/api/premium/benchmark")
def premium_benchmark(
    period: str = Query("1y"),
    benchmark: str = Query(analytics.DEFAULT_BENCHMARK),
) -> dict:
    premium.require(subscription, "benchmark")
    return analytics.benchmark_comparison(store.positions(), period=period, benchmark=benchmark)


@app.get("/api/premium/export.csv")
def premium_export() -> PlainTextResponse:
    premium.require(subscription, "export")
    csv_text = analytics.portfolio_csv(_build_portfolio())
    return PlainTextResponse(csv_text, media_type="text/csv", headers={
        "Content-Disposition": 'attachment; filename="wealthtrack-portfolio.csv"'})


# ---------------------------------------------------------------- market --


@app.get("/api/quote/{symbol}")
def get_quote(symbol: str, refresh: bool = False) -> dict:
    return {"quote": market.get_quote(symbol, force_refresh=refresh).to_dict()}


@app.get("/api/history/{symbol}")
def get_history(symbol: str, period: str = Query("1mo")) -> dict:
    return {"symbol": symbol.upper(), "period": period, "points": market.get_history(symbol, period)}


@app.get("/api/validate/{symbol}")
def validate(symbol: str) -> dict:
    """Check a ticker without saving anything (used by the add form)."""
    try:
        quote = market.validate_symbol(symbol)
    except InvalidSymbolError as exc:
        return {"valid": False, "error": exc.to_dict()}
    return {"valid": True, "quote": quote.to_dict()}


# ------------------------------------------------------------------- ui --

# Mounting a missing directory raises at import time, which would take the
# whole app down over a packaging mistake. Degrade to an API-only service and
# say so instead.
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
else:  # pragma: no cover - deployment packaging problem
    log.error("Static directory missing at %s; serving API only.", STATIC_DIR)


@app.get("/", include_in_schema=False)
def index():
    index_file = STATIC_DIR / "index.html"
    if not index_file.is_file():
        return JSONResponse(status_code=500, content={
            "error": {
                "code": "static_missing",
                "message": "The dashboard files were not deployed with the app.",
                "details": {"expected_at": str(index_file)},
            }})
    return FileResponse(str(index_file))


@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.png", include_in_schema=False)
def favicon():
    """Browsers request these unprompted; answer without a 500 in the logs."""
    for name in ("favicon.ico", "favicon.png"):
        candidate = STATIC_DIR / name
        if candidate.is_file():
            return FileResponse(str(candidate))
    return Response(status_code=204)
