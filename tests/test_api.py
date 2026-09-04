"""API tests against a temporary portfolio file with Yahoo Finance stubbed out.

These cover the wiring and the error paths deterministically. Tests that hit
the real Yahoo Finance service live in test_market_live.py.
"""

import json
import os

import pytest
from fastapi.testclient import TestClient

from app import main, market
from app.errors import InvalidSymbolError, MarketUnavailableError
from app.market import Quote
from app.storage import PortfolioStore

PRICES = {"AAPL": 319.51, "MSFT": 500.38, "TSLA": 353.42}


@pytest.fixture
def client(tmp_path, monkeypatch):
    main.store = PortfolioStore(tmp_path / "portfolio.json")

    def fake_quote(symbol, force_refresh=False):
        symbol = symbol.strip().upper()
        if symbol == "DOWN":
            raise MarketUnavailableError("Yahoo Finance is unreachable.")
        if symbol not in PRICES:
            raise InvalidSymbolError(f"'{symbol}' is not a symbol Yahoo Finance recognises.")
        return Quote(symbol=symbol, price=PRICES[symbol], currency="USD",
                     short_name=f"{symbol} Inc.", previous_close=PRICES[symbol] - 2)

    def fake_quotes(symbols, force_refresh=False):
        quotes, errors = {}, {}
        for s in {x.strip().upper() for x in symbols}:
            try:
                quotes[s] = fake_quote(s)
            except (InvalidSymbolError, MarketUnavailableError) as exc:
                errors[s] = exc.to_dict()
        return quotes, errors

    monkeypatch.setattr(market, "get_quote", fake_quote)
    monkeypatch.setattr(market, "validate_symbol", fake_quote)
    monkeypatch.setattr(market, "get_quotes", fake_quotes)
    return TestClient(main.app)


def add(client, symbol, quantity, price):
    return client.post("/api/holdings", json={
        "symbol": symbol, "quantity": quantity, "purchase_price": price})


# ------------------------------------------------------------------ basics --


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_dashboard_is_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "WealthTrack" in r.text


def test_empty_portfolio(client):
    r = client.get("/api/portfolio")
    assert r.status_code == 200
    body = r.json()
    assert body["holdings"] == []
    assert body["totals"]["total_invested"] == 0
    assert body["totals_verified"] is True


# ------------------------------------------------------------------- crud --


def test_add_holding(client):
    r = add(client, "aapl", 10, 185.50)
    assert r.status_code == 201
    holding = r.json()["holding"]
    assert holding["symbol"] == "AAPL"          # normalised to upper case
    assert holding["quantity"] == 10
    assert holding["name"] == "AAPL Inc."       # enriched from the quote


def test_add_invalid_symbol_is_rejected_and_not_stored(client):
    r = add(client, "NOTAREALTICKER", 5, 10.00)
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_symbol"
    assert client.get("/api/holdings").json()["holdings"] == []


@pytest.mark.parametrize("quantity,price", [(0, 100), (-5, 100), (10, 0), (10, -1)])
def test_add_rejects_non_positive_numbers(client, quantity, price):
    assert add(client, "AAPL", quantity, price).status_code == 422


def test_add_rejects_missing_fields(client):
    assert client.post("/api/holdings", json={"symbol": "AAPL"}).status_code == 422


def test_update_holding(client):
    hid = add(client, "AAPL", 10, 185.50).json()["holding"]["id"]
    r = client.put(f"/api/holdings/{hid}", json={"quantity": 25, "purchase_price": 190.00})
    assert r.status_code == 200
    assert r.json()["holding"]["quantity"] == 25
    assert r.json()["holding"]["purchase_price"] == 190.00


def test_update_rejects_bad_quantity(client):
    hid = add(client, "AAPL", 10, 185.50).json()["holding"]["id"]
    assert client.put(f"/api/holdings/{hid}", json={"quantity": -1}).status_code == 422


def test_update_missing_holding_is_404(client):
    assert client.put("/api/holdings/nope", json={"quantity": 5}).status_code == 404


def test_delete_holding(client):
    hid = add(client, "AAPL", 10, 185.50).json()["holding"]["id"]
    assert client.delete(f"/api/holdings/{hid}").status_code == 200
    assert client.get("/api/holdings").json()["holdings"] == []


def test_delete_missing_holding_is_404(client):
    assert client.delete("/api/holdings/nope").status_code == 404


def test_atomic_write_retries_a_transient_windows_lock(tmp_path, monkeypatch):
    """os.replace can hit WinError 32 when a scanner opens the temp file.

    The lock clears in milliseconds, so the write must retry rather than lose
    the user's data.
    """
    import app.storage as storage_mod

    calls = {"n": 0}
    real_replace = os.replace

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(
                32, "The process cannot access the file because it is being "
                    "used by another process")
        return real_replace(src, dst)

    monkeypatch.setattr(storage_mod.os, "replace", flaky_replace)

    target = tmp_path / "retry.json"
    storage_mod.atomic_write_json(target, {"holdings": [{"id": "x"}]}, what="portfolio")

    assert calls["n"] == 3, "should have retried twice before succeeding"
    assert json.loads(target.read_text())["holdings"][0]["id"] == "x"


def test_atomic_write_gives_up_and_reports_after_persistent_lock(tmp_path, monkeypatch):
    import app.storage as storage_mod
    from app.errors import StorageError

    def always_locked(src, dst):
        raise PermissionError(32, "locked forever")

    monkeypatch.setattr(storage_mod.os, "replace", always_locked)
    with pytest.raises(StorageError) as exc:
        storage_mod.atomic_write_json(tmp_path / "x.json", {"a": 1})
    assert "Could not save" in str(exc.value)
    # the temp file must not be left behind
    assert not list(tmp_path.glob("*.tmp"))


def test_holdings_persist_across_store_reload(client, tmp_path):
    add(client, "AAPL", 10, 185.50)
    reloaded = PortfolioStore(main.store.path)
    assert len(reloaded.list_holdings()) == 1
    assert reloaded.list_holdings()[0]["symbol"] == "AAPL"


# -------------------------------------------------------------- portfolio --


def test_portfolio_totals_end_to_end(client):
    add(client, "AAPL", 10, 185.50)
    add(client, "MSFT", 5, 400.00)
    add(client, "TSLA", 20, 250.00)

    body = client.get("/api/portfolio").json()
    t = body["totals"]
    assert t["total_invested"] == 8855.00
    assert t["total_value"] == 12765.40
    assert t["total_profit_loss"] == 3910.40
    assert t["total_return_pct"] == 44.16
    assert body["totals_verified"] is True
    assert "verification_errors" not in body


def test_portfolio_reports_unavailable_prices_without_faking_them(client, monkeypatch):
    add(client, "AAPL", 10, 185.50)
    # Now make Yahoo fail for AAPL specifically.
    monkeypatch.setattr(
        market, "get_quotes",
        lambda symbols, force_refresh=False: (
            {}, {"AAPL": {"code": "market_unavailable", "message": "Yahoo Finance is unreachable."}}
        ))
    body = client.get("/api/portfolio").json()
    row = body["holdings"][0]
    assert row["price_available"] is False
    assert row["current_price"] is None
    assert row["current_value"] is None
    assert row["invested"] == 1855.00           # cost basis is still real
    assert body["totals"]["total_value"] == 0
    assert body["unpriced_symbols"] == ["AAPL"]
    assert any("AAPL" in w for w in body["warnings"])


def test_refresh_endpoint(client):
    add(client, "AAPL", 10, 185.50)
    r = client.post("/api/portfolio/refresh")
    assert r.status_code == 200
    assert r.json()["totals"]["total_value"] == 3195.10


def test_allocation_percentages(client):
    add(client, "AAPL", 10, 185.50)   # 3195.10
    add(client, "MSFT", 5, 400.00)    # 2501.90
    alloc = client.get("/api/portfolio").json()["allocation"]
    assert [a["symbol"] for a in alloc] == ["AAPL", "MSFT"]
    assert round(sum(a["pct"] for a in alloc), 2) == 100.00


# ----------------------------------------------------------------- quotes --


def test_quote_endpoint(client):
    body = client.get("/api/quote/AAPL").json()
    assert body["quote"]["symbol"] == "AAPL"
    assert body["quote"]["price"] == 319.51


def test_quote_endpoint_invalid_symbol(client):
    r = client.get("/api/quote/NOPE")
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "invalid_symbol"


def test_quote_endpoint_market_down_is_503(client):
    r = client.get("/api/quote/DOWN")
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "market_unavailable"


def test_validate_endpoint_valid(client):
    body = client.get("/api/validate/MSFT").json()
    assert body["valid"] is True and body["quote"]["symbol"] == "MSFT"


def test_validate_endpoint_invalid(client):
    body = client.get("/api/validate/FAKE").json()
    assert body["valid"] is False
    assert body["error"]["code"] == "invalid_symbol"
