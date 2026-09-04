"""Serverless / read-only filesystem behaviour.

Regression tests for the Vercel failure:

    OSError: [Errno 30] Read-only file system: '/var/task/data'

which happened at *import* time, so every route returned 500 -- including
/favicon.ico, which never touches the portfolio at all.
"""

import importlib
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.premium import SubscriptionStore
from app.storage import PortfolioStore


# ----------------------------------------------------------- path resolve --


def test_writable_dir_is_detected(tmp_path):
    assert config._is_writable(tmp_path / "sub") is True


def test_unwritable_dir_is_detected(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Path, "mkdir", boom)
    assert config._is_writable(tmp_path / "nope") is False


def test_env_override_is_honoured(tmp_path, monkeypatch):
    target = tmp_path / "custom"
    monkeypatch.setenv("WEALTHTRACK_DATA_DIR", str(target))
    data_dir, ephemeral, source = config._resolve_data_dir()
    assert data_dir == target
    assert ephemeral is False
    assert "WEALTHTRACK_DATA_DIR" in source


def test_falls_back_to_temp_when_project_dir_is_readonly(tmp_path, monkeypatch):
    """The Vercel case: project dir unwritable, temp dir fine."""
    monkeypatch.delenv("WEALTHTRACK_DATA_DIR", raising=False)
    real_writable = config._is_writable

    def only_temp_writable(path: Path) -> bool:
        if "wealthtrack" in str(path).lower() and "data" not in path.name:
            return real_writable(path)
        return False

    monkeypatch.setattr(config, "_is_writable", only_temp_writable)
    data_dir, ephemeral, source = config._resolve_data_dir()
    assert ephemeral is True, "temp storage must be flagged as ephemeral"
    assert "read-only" in source


def test_storage_info_exposes_the_warning(monkeypatch):
    monkeypatch.setattr(config, "IS_EPHEMERAL", True)
    info = config.storage_info()
    assert info["ephemeral"] is True
    assert info["warning"] and "lost when the server restarts" in info["warning"]


# ------------------------------------------------------- stores degrade --


def test_portfolio_store_survives_readonly_filesystem(tmp_path, monkeypatch):
    """This is the exact crash: mkdir raising OSError 30 in __init__."""
    def readonly_mkdir(*a, **k):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Path, "mkdir", readonly_mkdir)

    store = PortfolioStore(tmp_path / "data" / "portfolio.json")  # must not raise
    assert store.in_memory_only is True

    # ...and it still works, just without persistence.
    holding = store.add_holding("AAPL", 10, 185.50)
    assert holding["symbol"] == "AAPL"
    assert len(store.list_holdings()) == 1
    store.delete_holding(holding["id"])
    assert store.list_holdings() == []


def test_subscription_store_survives_readonly_filesystem(tmp_path, monkeypatch):
    def readonly_mkdir(*a, **k):
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Path, "mkdir", readonly_mkdir)

    sub = SubscriptionStore(tmp_path / "data" / "subscription.json")
    assert sub.in_memory_only is True
    assert sub.state()["plan"] == "free"
    assert sub.activate("pro_annual", trial=True)["is_premium"] is True


def test_normal_filesystem_still_persists(tmp_path):
    """The fallback must not accidentally disable real persistence."""
    path = tmp_path / "data" / "portfolio.json"
    store = PortfolioStore(path)
    assert store.in_memory_only is False
    store.add_holding("AAPL", 10, 185.50)
    assert path.is_file()
    assert json.loads(path.read_text())["holdings"][0]["symbol"] == "AAPL"
    assert len(PortfolioStore(path).list_holdings()) == 1


# ------------------------------------------------------------ app import --


def test_app_imports_on_a_readonly_filesystem(monkeypatch):
    """The regression itself: importing app.main must not raise."""
    real_mkdir = Path.mkdir

    def readonly_mkdir(self, *a, **k):
        # Let temp dirs through, deny everything else, like a serverless host.
        if "temp" in str(self).lower() or "tmp" in str(self).lower():
            return real_mkdir(self, *a, **k)
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr(Path, "mkdir", readonly_mkdir)

    for mod in ("app.main", "app.config", "app.storage", "app.premium"):
        sys.modules.pop(mod, None)

    import app.main as main_mod  # must not raise
    importlib.reload(main_mod)
    assert main_mod.app is not None


def test_health_and_favicon_respond_on_ephemeral_storage(tmp_path, monkeypatch):
    """/favicon.ico 500'd in production purely because the module failed."""
    import app.main as main

    main.store = PortfolioStore(tmp_path / "portfolio.json")
    main.subscription = SubscriptionStore(tmp_path / "subscription.json")
    monkeypatch.setattr(config, "IS_EPHEMERAL", True)

    client = TestClient(main.app)

    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["storage"]["ephemeral"] is True

    # No favicon file exists, so this should be a clean 204, never a 500.
    assert client.get("/favicon.ico").status_code in (200, 204)
    assert client.get("/favicon.png").status_code in (200, 204)


def test_portfolio_warns_when_storage_is_ephemeral(tmp_path, monkeypatch):
    import app.main as main
    from app import market
    from app.market import Quote

    main.store = PortfolioStore(tmp_path / "portfolio.json")
    main.subscription = SubscriptionStore(tmp_path / "subscription.json")
    monkeypatch.setattr(config, "IS_EPHEMERAL", True)
    monkeypatch.setattr(
        market, "get_quotes",
        lambda symbols, force_refresh=False: (
            {s: Quote(symbol=s, price=100.0, currency="USD") for s in symbols}, {}))

    client = TestClient(main.app)
    body = client.get("/api/portfolio").json()
    assert body["storage"]["ephemeral"] is True
    assert any("lost when the server restarts" in w for w in body["warnings"]), (
        "a user must not believe their portfolio was saved when it was not")
