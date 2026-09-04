"""PostgreSQL connection handling and schema management.

Used when a database URL is present in the environment (Vercel Postgres /
Neon sets these automatically). Without one, the app falls back to the local
JSON file store -- see ``storage.create_portfolio_store``.

Two rules this module follows carefully:

1. **Nothing connects at import time.** A database that is down or misconfigured
   must produce a clear error on a request, not a module-import crash that
   500s every route (the exact failure mode that broke the first deployment).
2. **The pool is opened lazily and reused.** Serverless invocations are short,
   so a connection is created on first use and kept for the life of the
   instance rather than reconnecting per request.
"""

from __future__ import annotations

import logging
import os
import threading
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .errors import StorageError

log = logging.getLogger("wealthtrack.db")

# Vercel Postgres / Neon inject several of these. Order matters: the pooled
# URL is the right default for serverless, and our own override wins outright.
_URL_ENV_VARS = (
    "WEALTHTRACK_DATABASE_URL",
    "POSTGRES_URL",
    "DATABASE_URL",
    "POSTGRES_URL_NON_POOLING",
    "POSTGRES_PRISMA_URL",
)

# Query params some providers add that libpq does not understand.
_STRIP_PARAMS = {"pgbouncer", "schema", "connection_limit", "pool_timeout"}

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}

SCHEMA_VERSION = "001_init"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS holdings (
    id              TEXT          PRIMARY KEY,
    symbol          TEXT          NOT NULL,
    name            TEXT,
    -- NUMERIC, not float: this is money. 8dp on quantity covers fractional
    -- shares; 4dp on price matches the app's price precision.
    quantity        NUMERIC(24,8) NOT NULL CHECK (quantity > 0),
    purchase_price  NUMERIC(20,4) NOT NULL CHECK (purchase_price > 0),
    purchase_date   DATE,
    notes           TEXT,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS holdings_symbol_idx  ON holdings (symbol);
CREATE INDEX IF NOT EXISTS holdings_created_idx ON holdings (created_at);

-- Single-row table; the CHECK keeps it that way.
CREATE TABLE IF NOT EXISTS subscription (
    id             SMALLINT    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    plan           TEXT        NOT NULL DEFAULT 'free',
    since          TIMESTAMPTZ,
    trial_ends_at  TIMESTAMPTZ,
    source         TEXT,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO subscription (id, plan, since)
VALUES (1, 'free', now())
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT        PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def normalise_dsn(url: str) -> str:
    """Clean a provider URL into something libpq accepts.

    Strips Prisma/pgbouncer-only query params, and requires SSL for any remote
    host (Neon rejects unencrypted connections; forcing it locally would break
    a plain local server).
    """
    parsed = urlparse(url)
    params = [(k, v) for k, v in parse_qsl(parsed.query) if k not in _STRIP_PARAMS]

    host = (parsed.hostname or "").lower()
    if host not in _LOCAL_HOSTS and not any(k == "sslmode" for k, _ in params):
        params.append(("sslmode", "require"))

    scheme = "postgresql" if parsed.scheme in ("postgres", "postgresql") else parsed.scheme
    return urlunparse(parsed._replace(scheme=scheme, query=urlencode(params)))


def get_database_url() -> str | None:
    """The configured database URL, or None to use file storage."""
    for var in _URL_ENV_VARS:
        value = os.environ.get(var)
        if value and value.strip():
            return normalise_dsn(value.strip())
    return None


def configured_from() -> str | None:
    for var in _URL_ENV_VARS:
        if os.environ.get(var, "").strip():
            return var
    return None


def is_configured() -> bool:
    return get_database_url() is not None


def redact(url: str) -> str:
    """A URL safe to put in a log line or an API response."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "?"
        db = (parsed.path or "/").lstrip("/") or "?"
        return f"postgresql://***@{host}/{db}"
    except Exception:
        return "postgresql://***"


class Database:
    """Lazily-opened connection pool."""

    def __init__(self, dsn: str | None = None):
        self._dsn = dsn or get_database_url()
        self._pool = None
        self._lock = threading.Lock()
        self._schema_ready = False

    @property
    def dsn(self) -> str | None:
        return self._dsn

    def _get_pool(self):
        if self._pool is not None:
            return self._pool
        with self._lock:
            if self._pool is not None:
                return self._pool
            if not self._dsn:
                raise StorageError("No database URL is configured.")
            try:
                from psycopg_pool import ConnectionPool
            except ImportError as exc:  # pragma: no cover
                raise StorageError(
                    "psycopg is not installed; add 'psycopg[binary,pool]' to "
                    "requirements.txt."
                ) from exc
            try:
                # min_size=0 so constructing the pool never opens a socket --
                # a bad URL surfaces on first query, not at import.
                #
                # check_connection matters on serverless: instances freeze
                # between invocations and the server can drop the connection
                # meanwhile, so a pooled handle is validated before reuse
                # rather than failing the request with "connection closed".
                self._pool = ConnectionPool(
                    self._dsn, min_size=0, max_size=4, timeout=15,
                    max_idle=60, open=True,
                    check=ConnectionPool.check_connection,
                )
            except Exception as exc:
                raise StorageError(f"Could not create the database pool: {exc}") from exc
            return self._pool

    def connection(self):
        """Context manager yielding a pooled connection."""
        return self._get_pool().connection()

    def ensure_schema(self, force: bool = False) -> None:
        """Create tables if they do not exist. Safe to call repeatedly."""
        if self._schema_ready and not force:
            return
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
                cur.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s) "
                    "ON CONFLICT (version) DO NOTHING",
                    (SCHEMA_VERSION,),
                )
            conn.commit()
        self._schema_ready = True
        log.info("Database schema ready (%s)", redact(self._dsn or ""))

    def healthcheck(self) -> dict:
        try:
            with self.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                    cur.execute("SELECT count(*) FROM holdings")
                    count = cur.fetchone()[0]
            return {"connected": True, "holdings": int(count),
                    "url": redact(self._dsn or "")}
        except Exception as exc:
            return {"connected": False, "error": str(exc),
                    "url": redact(self._dsn or "")}

    def close(self) -> None:
        with self._lock:
            if self._pool is not None:
                try:
                    self._pool.close()
                finally:
                    self._pool = None
                    self._schema_ready = False


# Process-wide instance, created without connecting.
_default: Database | None = None
_default_lock = threading.Lock()


def get_db() -> Database:
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = Database()
    return _default


def reset_db() -> None:
    """Drop the cached instance (used by tests and after env changes)."""
    global _default
    with _default_lock:
        if _default is not None:
            _default.close()
        _default = None
