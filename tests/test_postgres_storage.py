"""PostgreSQL storage tests.

The CRUD tests are parametrised over BOTH backends with identical assertions.
That is the point: the file store and the Postgres store must be genuinely
interchangeable, and a shared test is the only way to keep them that way.

Postgres tests are marked ``pg`` and skip automatically when no test database
is configured. Point them at one with:

    WEALTHTRACK_TEST_DATABASE_URL=postgresql://user:pw@host:5432/dbname
"""

from __future__ import annotations

import os
import uuid

import pytest

from app import db as db_mod
from app.errors import HoldingNotFoundError, StorageError, ValidationError
from app.premium import SubscriptionStore
from app.storage import PortfolioStore

TEST_DB_URL = os.environ.get("WEALTHTRACK_TEST_DATABASE_URL") or os.environ.get(
    "WEALTHTRACK_DATABASE_URL")

requires_pg = pytest.mark.skipif(
    not TEST_DB_URL, reason="no test database (set WEALTHTRACK_TEST_DATABASE_URL)")


@pytest.fixture
def pg_database():
    """A Database pointed at the test DB, with tables emptied."""
    database = db_mod.Database(db_mod.normalise_dsn(TEST_DB_URL))
    database.ensure_schema()
    with database.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM holdings")
            cur.execute("UPDATE subscription SET plan='free', trial_ends_at=NULL WHERE id=1")
        conn.commit()
    yield database
    database.close()


@pytest.fixture
def pg_store(pg_database):
    from app.pg_storage import PostgresPortfolioStore
    return PostgresPortfolioStore(pg_database)


@pytest.fixture
def file_store(tmp_path):
    return PortfolioStore(tmp_path / "portfolio.json")


@pytest.fixture(params=["file", "postgres"])
def any_store(request, tmp_path):
    """Both backends, same assertions."""
    if request.param == "file":
        return PortfolioStore(tmp_path / "portfolio.json")
    if not TEST_DB_URL:
        pytest.skip("no test database configured")
    from app.pg_storage import PostgresPortfolioStore

    database = db_mod.Database(db_mod.normalise_dsn(TEST_DB_URL))
    database.ensure_schema()
    with database.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM holdings")
        conn.commit()
    return PostgresPortfolioStore(database)


# ------------------------------------------------- URL handling (no DB) --


def test_url_precedence(monkeypatch):
    for var in db_mod._URL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://a:b@h/db1")
    monkeypatch.setenv("POSTGRES_URL", "postgresql://a:b@h/db2")
    # POSTGRES_URL (the pooled Vercel one) wins over DATABASE_URL
    assert "db2" in db_mod.get_database_url()
    monkeypatch.setenv("WEALTHTRACK_DATABASE_URL", "postgresql://a:b@h/db3")
    assert "db3" in db_mod.get_database_url()


def test_no_url_means_not_configured(monkeypatch):
    for var in db_mod._URL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    assert db_mod.is_configured() is False
    assert db_mod.get_database_url() is None


def test_remote_urls_get_sslmode_required():
    dsn = db_mod.normalise_dsn("postgres://u:p@ep-cool-1.aws.neon.tech/neondb")
    assert "sslmode=require" in dsn
    assert dsn.startswith("postgresql://")


def test_local_urls_are_not_forced_to_ssl():
    dsn = db_mod.normalise_dsn("postgresql://u:p@127.0.0.1:5432/wealthtrack")
    assert "sslmode" not in dsn


def test_existing_sslmode_is_preserved():
    dsn = db_mod.normalise_dsn("postgresql://u:p@host.neon.tech/db?sslmode=verify-full")
    assert "sslmode=verify-full" in dsn
    assert dsn.count("sslmode") == 1


def test_prisma_only_params_are_stripped():
    """libpq rejects pgbouncer=true, which Vercel puts in POSTGRES_PRISMA_URL."""
    dsn = db_mod.normalise_dsn(
        "postgres://u:p@host.neon.tech/db?pgbouncer=true&connect_timeout=15&schema=public")
    assert "pgbouncer" not in dsn
    assert "schema=" not in dsn
    assert "connect_timeout=15" in dsn


def test_redact_hides_credentials():
    out = db_mod.redact("postgresql://user:supersecret@host.neon.tech/neondb")
    assert "supersecret" not in out and "user" not in out
    assert "host.neon.tech" in out and "neondb" in out


def test_database_object_does_not_connect_on_construction():
    """Constructing must never open a socket -- that is what broke deploy 1."""
    database = db_mod.Database("postgresql://nobody:nope@127.0.0.1:1/none")
    assert database.dsn is not None  # no exception


def test_unreachable_database_raises_storage_error_not_import_error():
    from app.pg_storage import PostgresPortfolioStore

    database = db_mod.Database("postgresql://nobody:nope@127.0.0.1:1/none")
    store = PostgresPortfolioStore(database)
    with pytest.raises(StorageError):
        store.list_holdings()


# ------------------------------------------- shared behaviour, both backends --


def test_add_and_list(any_store):
    h = any_store.add_holding("aapl", 25, 185.50, purchase_date="2025-03-14",
                              name="Apple Inc.")
    assert h["symbol"] == "AAPL"
    assert h["quantity"] == 25.0
    assert h["purchase_price"] == 185.50
    assert h["purchase_date"] == "2025-03-14"
    assert h["name"] == "Apple Inc."
    assert len(h["id"]) == 12
    assert len(any_store.list_holdings()) == 1


def test_returned_types_are_plain_floats(any_store):
    """Postgres returns Decimal; the contract is float, like the file store."""
    h = any_store.add_holding("AAPL", 25, 185.50)
    assert type(h["quantity"]) is float
    assert type(h["purchase_price"]) is float
    listed = any_store.list_holdings()[0]
    assert type(listed["quantity"]) is float
    assert type(listed["purchase_price"]) is float


def test_get_holding(any_store):
    h = any_store.add_holding("AAPL", 10, 100.0)
    assert any_store.get_holding(h["id"])["symbol"] == "AAPL"


def test_get_missing_raises(any_store):
    with pytest.raises(HoldingNotFoundError):
        any_store.get_holding("does-not-exist")


def test_update_holding(any_store):
    h = any_store.add_holding("AAPL", 10, 100.0)
    u = any_store.update_holding(h["id"], quantity=30, purchase_price=190.25)
    assert u["quantity"] == 30.0
    assert u["purchase_price"] == 190.25
    assert u["symbol"] == "AAPL"          # untouched fields survive


def test_update_partial_keeps_other_fields(any_store):
    h = any_store.add_holding("AAPL", 10, 100.0, purchase_date="2025-01-02",
                              notes="long term")
    u = any_store.update_holding(h["id"], quantity=11)
    assert u["purchase_price"] == 100.0
    assert u["purchase_date"] == "2025-01-02"
    assert u["notes"] == "long term"


def test_update_missing_raises(any_store):
    with pytest.raises(HoldingNotFoundError):
        any_store.update_holding("nope", quantity=5)


def test_delete_holding(any_store):
    h = any_store.add_holding("AAPL", 10, 100.0)
    assert any_store.delete_holding(h["id"])["symbol"] == "AAPL"
    assert any_store.list_holdings() == []


def test_delete_missing_raises(any_store):
    with pytest.raises(HoldingNotFoundError):
        any_store.delete_holding("nope")


def test_clear(any_store):
    any_store.add_holding("AAPL", 1, 1.0)
    any_store.add_holding("MSFT", 1, 1.0)
    assert any_store.clear() == 2
    assert any_store.list_holdings() == []


def test_positions_merges_lots(any_store):
    any_store.add_holding("AAPL", 10, 100.0)
    any_store.add_holding("AAPL", 5, 200.0)
    any_store.add_holding("MSFT", 3, 300.0)
    assert any_store.positions() == {"AAPL": 15.0, "MSFT": 3.0}


@pytest.mark.parametrize("quantity,price", [(0, 100), (-5, 100), (10, 0), (10, -1)])
def test_rejects_non_positive(any_store, quantity, price):
    with pytest.raises(ValidationError):
        any_store.add_holding("AAPL", quantity, price)


def test_fractional_quantities(any_store):
    h = any_store.add_holding("AAPL", 0.12345678, 185.5)
    assert h["quantity"] == pytest.approx(0.12345678)


def test_sub_cent_price_precision_survives(any_store):
    """NUMERIC(20,4) must keep a 4dp fill price intact."""
    h = any_store.add_holding("AAPL", 3, 33.3335)
    assert any_store.get_holding(h["id"])["purchase_price"] == 33.3335


# --------------------------------------------------------- postgres only --


@requires_pg
def test_data_persists_across_new_store_instances(pg_database):
    """The whole point: a fresh process must see the same rows."""
    from app.pg_storage import PostgresPortfolioStore

    first = PostgresPortfolioStore(pg_database)
    first.add_holding("AAPL", 25, 185.50, purchase_date="2025-03-14")

    # A completely separate Database + store, as a new serverless instance would be.
    other_db = db_mod.Database(db_mod.normalise_dsn(TEST_DB_URL))
    second = PostgresPortfolioStore(other_db)
    rows = second.list_holdings()
    other_db.close()

    assert len(rows) == 1
    assert rows[0]["symbol"] == "AAPL"
    assert rows[0]["quantity"] == 25.0


@requires_pg
def test_schema_creation_is_idempotent(pg_database):
    pg_database.ensure_schema(force=True)
    pg_database.ensure_schema(force=True)
    with pg_database.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM schema_migrations WHERE version = %s",
                    (db_mod.SCHEMA_VERSION,))
        assert cur.fetchone()[0] == 1


@requires_pg
def test_quantity_check_constraint_is_enforced_in_the_database(pg_database):
    """Defence in depth: even a direct INSERT cannot store a bad quantity."""
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        with pg_database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO holdings (id, symbol, quantity, purchase_price) "
                    "VALUES (%s, 'AAPL', -5, 100)", (uuid.uuid4().hex[:12],))
            conn.commit()


@requires_pg
def test_bad_date_rejected(pg_store):
    with pytest.raises(ValidationError):
        pg_store.add_holding("AAPL", 1, 1.0, purchase_date="not-a-date")


@requires_pg
def test_subscription_round_trip(pg_database):
    from app.pg_storage import PostgresSubscriptionStore

    sub = PostgresSubscriptionStore(pg_database)
    assert sub.state()["plan"] == "free"
    assert sub.activate("pro_annual", trial=True)["is_premium"] is True

    # A separate instance sees it -- state is in the database, not memory.
    other = PostgresSubscriptionStore(pg_database)
    assert other.state()["is_premium"] is True
    assert other.state()["trial_days_left"] is not None

    other.cancel()
    assert sub.state()["plan"] == "free"


@requires_pg
def test_healthcheck_reports_connected(pg_database):
    health = pg_database.healthcheck()
    assert health["connected"] is True
    assert "holdings" in health
    assert "@" in health["url"] and "***" in health["url"]


@requires_pg
def test_health_endpoint_reports_the_postgres_backend(pg_database):
    """Regression: /api/health 500'd on the Postgres branch.

    The other API tests swap in a file store, so this path was never exercised
    until it ran in a real deployment.
    """
    from fastapi.testclient import TestClient

    import app.main as main
    from app.pg_storage import PostgresPortfolioStore, PostgresSubscriptionStore

    original_store, original_sub = main.store, main.subscription
    main.store = PostgresPortfolioStore(pg_database)
    main.subscription = PostgresSubscriptionStore(pg_database)
    try:
        r = TestClient(main.app).get("/api/health")
        assert r.status_code == 200, r.text
        storage = r.json()["storage"]
        assert storage["backend"] == "postgres"
        assert storage["durable"] is True
        assert storage["database"]["connected"] is True
        assert "***" in storage["database"]["url"]      # credentials redacted
    finally:
        main.store, main.subscription = original_store, original_sub


@requires_pg
def test_portfolio_endpoint_has_no_ephemeral_warning_on_postgres(pg_database):
    from fastapi.testclient import TestClient

    import app.main as main
    from app import market
    from app.market import Quote
    from app.pg_storage import PostgresPortfolioStore, PostgresSubscriptionStore

    original_store, original_sub = main.store, main.subscription
    original_quotes = market.get_quotes
    main.store = PostgresPortfolioStore(pg_database)
    main.subscription = PostgresSubscriptionStore(pg_database)
    market.get_quotes = lambda symbols, force_refresh=False: (
        {s: Quote(symbol=s, price=100.0, currency="USD") for s in symbols}, {})
    try:
        main.store.add_holding("AAPL", 10, 50.0)
        body = TestClient(main.app).get("/api/portfolio").json()
        assert body["storage"]["backend"] == "postgres"
        assert body["storage"]["ephemeral"] is False
        assert not any("lost when the server restarts" in w for w in body["warnings"])
    finally:
        main.store, main.subscription = original_store, original_sub
        market.get_quotes = original_quotes


@requires_pg
def test_factory_picks_postgres_when_configured(monkeypatch):
    from app.storage import create_portfolio_store

    monkeypatch.setenv("WEALTHTRACK_DATABASE_URL", TEST_DB_URL)
    db_mod.reset_db()
    try:
        assert create_portfolio_store().backend == "postgres"
    finally:
        db_mod.reset_db()


def test_factory_falls_back_to_file_without_a_url(monkeypatch):
    from app.storage import create_portfolio_store

    for var in db_mod._URL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    db_mod.reset_db()
    try:
        store = create_portfolio_store()
        assert store.backend == "file"
        assert store.path is not None
    finally:
        db_mod.reset_db()
