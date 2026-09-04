"""Premium plan gating, pricing payload and the analytics endpoints."""

import pytest
from fastapi.testclient import TestClient

from app import main, market, premium
from app.errors import InvalidSymbolError, PremiumRequiredError
from app.market import Quote
from app.premium import SubscriptionStore
from app.storage import PortfolioStore

PRICES = {"AAPL": 319.51, "MSFT": 500.38, "TSLA": 353.42}
PROFILES = {
    "AAPL": {"sector": "Technology", "industry": "Consumer Electronics",
             "country": "United States", "dividendRate": 1.08, "beta": 1.086,
             "shortName": "Apple Inc.", "exDividendDate": 4102444800},  # 2100, future
    "MSFT": {"sector": "Technology", "industry": "Software",
             "country": "United States", "dividendRate": 3.64, "beta": 0.9,
             "shortName": "Microsoft Corporation"},
    "TSLA": {"sector": "Consumer Cyclical", "industry": "Auto Manufacturers",
             "country": "United States", "dividendRate": None, "beta": 1.827,
             "shortName": "Tesla, Inc."},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    main.store = PortfolioStore(tmp_path / "portfolio.json")
    main.subscription = SubscriptionStore(tmp_path / "subscription.json")

    def fake_quote(symbol, force_refresh=False):
        symbol = symbol.strip().upper()
        if symbol not in PRICES:
            raise InvalidSymbolError(f"'{symbol}' is not recognised.")
        return Quote(symbol=symbol, price=PRICES[symbol], currency="USD",
                     short_name=PROFILES[symbol]["shortName"])

    def fake_quotes(symbols, force_refresh=False):
        quotes, errors = {}, {}
        for s in {x.strip().upper() for x in symbols}:
            try:
                quotes[s] = fake_quote(s)
            except InvalidSymbolError as exc:
                errors[s] = exc.to_dict()
        return quotes, errors

    monkeypatch.setattr(market, "get_quote", fake_quote)
    monkeypatch.setattr(market, "validate_symbol", fake_quote)
    monkeypatch.setattr(market, "get_quotes", fake_quotes)
    monkeypatch.setattr(market, "get_profiles",
                        lambda symbols: {s: PROFILES.get(s, {}) for s in symbols})
    return TestClient(main.app)


def add(client, symbol, quantity=10, price_=100.0):
    return client.post("/api/holdings", json={
        "symbol": symbol, "quantity": quantity, "purchase_price": price_})


def go_pro(client):
    return client.post("/api/subscription/trial", json={"plan_id": "pro_annual"})


# ------------------------------------------------------------- plan state --


def test_starts_on_the_free_plan(client):
    sub = client.get("/api/subscription").json()["subscription"]
    assert sub["plan"] == "free"
    assert sub["is_premium"] is False
    assert sub["max_holdings"] == premium.FREE_MAX_HOLDINGS
    assert sub["refresh_seconds"] == premium.FREE_REFRESH_SECONDS


def test_plans_payload_shape(client):
    body = client.get("/api/plans").json()
    assert set(body["plans"]) == {"free", "pro_monthly", "pro_annual", "lifetime"}
    assert body["plans"]["pro_annual"]["price"] == 71.88
    assert len(body["premium_features"]) >= 5
    assert body["feature_matrix"][0]["group"] == "Portfolio"
    assert body["subscription"]["plan"] == "free"


def test_annual_is_cheaper_per_month_than_monthly(client):
    plans = client.get("/api/plans").json()["plans"]
    monthly_total = plans["pro_monthly"]["price"] * 12
    assert plans["pro_annual"]["price"] < monthly_total, "annual must actually save money"


def test_trial_unlocks_pro(client):
    r = go_pro(client)
    assert r.status_code == 200
    sub = r.json()["subscription"]
    assert sub["is_premium"] is True
    assert sub["is_trial"] is True
    assert sub["trial_days_left"] == premium.TRIAL_DAYS - 1
    assert sub["max_holdings"] is None
    assert sub["refresh_seconds"] == premium.PRO_REFRESH_SECONDS


def test_cancel_returns_to_free(client):
    go_pro(client)
    r = client.post("/api/subscription/cancel")
    assert r.json()["subscription"]["is_premium"] is False


def test_expired_trial_reverts_to_free(client, tmp_path):
    from datetime import datetime, timedelta, timezone
    import json

    go_pro(client)
    path = main.subscription.path
    data = json.loads(path.read_text())
    data["trial_ends_at"] = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    path.write_text(json.dumps(data))

    sub = client.get("/api/subscription").json()["subscription"]
    assert sub["is_premium"] is False
    assert sub["trial_expired"] is True
    assert sub["plan"] == "free"


def test_corrupt_subscription_file_fails_to_free_not_pro(client):
    main.subscription.path.write_text("{ not json")
    sub = client.get("/api/subscription").json()["subscription"]
    assert sub["is_premium"] is False


# ---------------------------------------------------------- holding limit --


def test_free_plan_stops_at_the_limit(client):
    for i in range(premium.FREE_MAX_HOLDINGS):
        assert add(client, "AAPL").status_code == 201
    r = add(client, "MSFT")
    assert r.status_code == 402
    body = r.json()["error"]
    assert body["code"] == "premium_required"
    assert body["details"]["feature"] == "unlimited"
    assert body["details"]["limit"] == premium.FREE_MAX_HOLDINGS


def test_pro_has_no_holding_limit(client):
    go_pro(client)
    for _ in range(premium.FREE_MAX_HOLDINGS + 3):
        assert add(client, "AAPL").status_code == 201
    assert len(client.get("/api/holdings").json()["holdings"]) == premium.FREE_MAX_HOLDINGS + 3


def test_limit_does_not_delete_existing_holdings_on_downgrade(client):
    """Downgrading must not silently destroy data the user already entered."""
    go_pro(client)
    for _ in range(premium.FREE_MAX_HOLDINGS + 2):
        add(client, "AAPL")
    client.post("/api/subscription/cancel")

    holdings = client.get("/api/holdings").json()["holdings"]
    assert len(holdings) == premium.FREE_MAX_HOLDINGS + 2, "existing holdings kept"
    # ...they just cannot add more until they upgrade again.
    assert add(client, "MSFT").status_code == 402
    # and the portfolio still values all of them
    assert client.get("/api/portfolio").json()["totals"]["holdings_count"] == 12


# ------------------------------------------------------------ history gate --


@pytest.mark.parametrize("period", ["1mo", "6mo", "1y"])
def test_free_history_periods_allowed(client, period):
    add(client, "AAPL")
    assert client.get(f"/api/portfolio/history?period={period}").status_code in (200, 503)


@pytest.mark.parametrize("period", ["2y", "5y", "10y", "max"])
def test_long_history_is_gated(client, period):
    add(client, "AAPL")
    r = client.get(f"/api/portfolio/history?period={period}")
    assert r.status_code == 402
    assert r.json()["error"]["details"]["feature"] == "history"


# --------------------------------------------------------- feature gates --


@pytest.mark.parametrize("path", [
    "/api/premium/dividends", "/api/premium/sectors",
    "/api/premium/benchmark", "/api/premium/export.csv",
])
def test_premium_endpoints_blocked_on_free(client, path):
    add(client, "AAPL")
    r = client.get(path)
    assert r.status_code == 402
    assert r.json()["error"]["code"] == "premium_required"


# ------------------------------------------------------------- analytics --


def test_dividend_summary_uses_real_rates(client):
    add(client, "AAPL", 10, 185.50)     # 1.08/share -> 10.80
    add(client, "MSFT", 5, 400.00)      # 3.64/share -> 18.20
    add(client, "TSLA", 20, 250.00)     # no dividend
    go_pro(client)

    d = client.get("/api/premium/dividends").json()
    by = {h["symbol"]: h for h in d["holdings"]}
    assert by["AAPL"]["annual_income"] == 10.80
    assert by["MSFT"]["annual_income"] == 18.20
    # A non-payer reports None, not a confident $0.00
    assert by["TSLA"]["pays_dividend"] is False
    assert by["TSLA"]["annual_income"] is None

    assert d["totals"]["annual_income"] == 29.00
    assert d["totals"]["paying_holdings"] == 2
    assert d["totals"]["total_holdings"] == 3
    # yield on cost = 29.00 / 8855.00 = 0.3275% -> 0.33
    assert d["totals"]["yield_on_cost"] == 0.33


def test_past_ex_dividend_dates_are_not_listed_as_upcoming(client):
    add(client, "MSFT", 5, 400.00)   # profile has no exDividendDate
    add(client, "AAPL", 10, 185.50)  # far-future date in the fixture
    go_pro(client)
    d = client.get("/api/premium/dividends").json()
    assert [u["symbol"] for u in d["upcoming_ex_dates"]] == ["AAPL"]
    assert all(u["ex_dividend_is_upcoming"] for u in d["upcoming_ex_dates"])


def test_sector_breakdown_percentages(client):
    add(client, "AAPL", 10, 100.0)   # 3195.10 Technology
    add(client, "MSFT", 5, 100.0)    # 2501.90 Technology
    add(client, "TSLA", 20, 100.0)   # 7068.40 Consumer Cyclical
    go_pro(client)

    s = client.get("/api/premium/sectors").json()
    by = {row["label"]: row for row in s["sectors"]}
    assert set(by) == {"Technology", "Consumer Cyclical"}
    # Technology = 3195.10 + 2501.90 = 5697.00 of 12765.40 = 44.63%
    assert by["Technology"]["pct"] == 44.63
    assert by["Consumer Cyclical"]["pct"] == 55.37
    assert round(sum(r["pct"] for r in s["sectors"]), 2) == 100.00


def test_diversification_score_does_not_flatter_a_two_sector_portfolio(client):
    """An even 50/50 split across two sectors is NOT well diversified."""
    add(client, "AAPL", 10, 100.0)
    add(client, "TSLA", 9, 100.0)     # roughly equal value
    go_pro(client)
    s = client.get("/api/premium/sectors").json()
    assert s["effective_sectors"] <= 2.05
    assert s["diversification_score"] < 20, (
        f"two sectors scored {s['diversification_score']}, which would mislead")


def test_concentration_warning_fires(client):
    add(client, "AAPL", 100, 100.0)
    add(client, "MSFT", 100, 100.0)   # 100% Technology
    go_pro(client)
    s = client.get("/api/premium/sectors").json()
    assert s["sectors"][0]["pct"] == 100.00
    assert "Technology" in s["concentration_warning"]
    assert s["diversification_score"] == 0.0


def test_csv_export(client):
    add(client, "AAPL", 10, 185.50)
    go_pro(client)
    r = client.get("/api/premium/export.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("symbol,name,quantity")
    assert "AAPL" in lines[1]
    assert lines[-1].startswith("TOTAL")


# -------------------------------------------------------------- checkout --


def test_checkout_is_honest_about_not_being_connected(client):
    r = client.post("/api/checkout", json={"plan_id": "pro_annual"})
    assert r.status_code == 501
    err = r.json()["error"]
    assert err["code"] == "checkout_not_configured"
    assert err["details"]["price"] == 71.88


def test_checkout_rejects_unknown_plan(client):
    assert client.post("/api/checkout", json={"plan_id": "nope"}).status_code == 400


def test_checkout_accepts_no_payment_fields(client):
    """The checkout model must not accept card data, even if sent."""
    r = client.post("/api/checkout", json={
        "plan_id": "pro_annual", "card_number": "4242424242424242", "cvc": "123"})
    # Extra fields are ignored, not stored; the response is still the 501 note.
    assert r.status_code == 501
    assert "card" not in r.text.lower() or "card checkout is not connected" in r.text.lower()
