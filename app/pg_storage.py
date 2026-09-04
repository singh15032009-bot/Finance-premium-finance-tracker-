"""PostgreSQL-backed stores.

These expose exactly the same public API as the JSON file stores in
``storage.py`` and ``premium.py``, so the rest of the app -- routes,
calculations, tests -- is unchanged regardless of which backend is live.

Rows are converted to plain dicts with float quantities and prices, matching
the file backend's contract byte for byte. Storage is NUMERIC (money must not
be a float on disk); the conversion happens at the boundary.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from .db import Database, get_db
from .errors import HoldingNotFoundError, StorageError, ValidationError

log = logging.getLogger("wealthtrack.pg")

MAX_QUANTITY = 1_000_000_000
MAX_PRICE = 10_000_000

_COLUMNS = (
    "id, symbol, name, quantity, purchase_price, purchase_date, notes, "
    "created_at, updated_at"
)


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _num(value) -> float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _row_to_holding(row) -> dict:
    return {
        "id": row[0],
        "symbol": row[1],
        "name": row[2],
        "quantity": _num(row[3]),
        "purchase_price": _num(row[4]),
        "purchase_date": _iso(row[5]),
        "notes": row[6],
        "created_at": _iso(row[7]),
        "updated_at": _iso(row[8]),
    }


def _parse_date(value):
    """Accept an ISO date string, a date, or None. Reject anything else."""
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise ValidationError(
            f"'{value}' is not a valid date. Use YYYY-MM-DD."
        ) from exc


def _validate(quantity, purchase_price) -> tuple[float, float]:
    try:
        q = float(quantity)
        p = float(purchase_price)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Quantity and purchase price must be numbers.") from exc
    if q <= 0:
        raise ValidationError("Quantity must be greater than zero.")
    if p <= 0:
        raise ValidationError("Purchase price must be greater than zero.")
    if q > MAX_QUANTITY:
        raise ValidationError(f"Quantity must be below {MAX_QUANTITY:,}.")
    if p > MAX_PRICE:
        raise ValidationError(f"Purchase price must be below {MAX_PRICE:,}.")
    return q, p


def _wrap(exc: Exception, action: str) -> StorageError:
    return StorageError(f"Database error while {action}: {exc}")


class PostgresPortfolioStore:
    """Holdings persisted in PostgreSQL."""

    backend = "postgres"
    in_memory_only = False
    is_durable = True

    def __init__(self, db: Database | None = None):
        self.db = db or get_db()
        self.path = None  # no file; kept for interface parity

    def _ready(self) -> Database:
        self.db.ensure_schema()
        return self.db

    # --------------------------------------------------------------- crud --

    def list_holdings(self) -> list[dict]:
        try:
            with self._ready().connection() as conn, conn.cursor() as cur:
                cur.execute(f"SELECT {_COLUMNS} FROM holdings ORDER BY created_at, id")
                return [_row_to_holding(r) for r in cur.fetchall()]
        except StorageError:
            raise
        except Exception as exc:
            raise _wrap(exc, "listing holdings") from exc

    def get_holding(self, holding_id: str) -> dict:
        try:
            with self._ready().connection() as conn, conn.cursor() as cur:
                cur.execute(f"SELECT {_COLUMNS} FROM holdings WHERE id = %s", (holding_id,))
                row = cur.fetchone()
        except StorageError:
            raise
        except Exception as exc:
            raise _wrap(exc, "reading a holding") from exc
        if row is None:
            raise HoldingNotFoundError(f"No holding with id {holding_id}.")
        return _row_to_holding(row)

    def add_holding(self, symbol, quantity, purchase_price,
                    purchase_date=None, notes=None, name=None) -> dict:
        q, p = _validate(quantity, purchase_price)
        holding_id = uuid.uuid4().hex[:12]
        try:
            with self._ready().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"INSERT INTO holdings (id, symbol, name, quantity, "
                        f"purchase_price, purchase_date, notes) "
                        f"VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING {_COLUMNS}",
                        (holding_id, str(symbol).strip().upper(), name,
                         Decimal(str(q)), Decimal(str(p)),
                         _parse_date(purchase_date),
                         (notes or "").strip() or None),
                    )
                    row = cur.fetchone()
                conn.commit()
        except (StorageError, ValidationError):
            raise
        except Exception as exc:
            raise _wrap(exc, "adding a holding") from exc
        return _row_to_holding(row)

    def update_holding(self, holding_id: str, **fields) -> dict:
        current = self.get_holding(holding_id)

        symbol = fields["symbol"].strip().upper() if fields.get("symbol") else current["symbol"]
        name = fields["name"] if "name" in fields else current["name"]
        quantity = fields["quantity"] if fields.get("quantity") is not None else current["quantity"]
        price = (fields["purchase_price"] if fields.get("purchase_price") is not None
                 else current["purchase_price"])
        purchase_date = (fields["purchase_date"] if "purchase_date" in fields
                         else current["purchase_date"])
        notes = fields["notes"] if "notes" in fields else current["notes"]

        q, p = _validate(quantity, price)
        try:
            with self._ready().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"UPDATE holdings SET symbol=%s, name=%s, quantity=%s, "
                        f"purchase_price=%s, purchase_date=%s, notes=%s, "
                        f"updated_at=now() WHERE id=%s RETURNING {_COLUMNS}",
                        (symbol, name, Decimal(str(q)), Decimal(str(p)),
                         _parse_date(purchase_date),
                         (notes or "").strip() or None if notes is not None else None,
                         holding_id),
                    )
                    row = cur.fetchone()
                conn.commit()
        except (StorageError, ValidationError, HoldingNotFoundError):
            raise
        except Exception as exc:
            raise _wrap(exc, "updating a holding") from exc
        if row is None:
            raise HoldingNotFoundError(f"No holding with id {holding_id}.")
        return _row_to_holding(row)

    def delete_holding(self, holding_id: str) -> dict:
        try:
            with self._ready().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        f"DELETE FROM holdings WHERE id = %s RETURNING {_COLUMNS}",
                        (holding_id,))
                    row = cur.fetchone()
                conn.commit()
        except StorageError:
            raise
        except Exception as exc:
            raise _wrap(exc, "deleting a holding") from exc
        if row is None:
            raise HoldingNotFoundError(f"No holding with id {holding_id}.")
        return _row_to_holding(row)

    def clear(self) -> int:
        try:
            with self._ready().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM holdings")
                    count = cur.rowcount
                conn.commit()
        except StorageError:
            raise
        except Exception as exc:
            raise _wrap(exc, "clearing the portfolio") from exc
        return int(count or 0)

    def positions(self) -> dict[str, float]:
        """Total quantity per symbol -- grouped in SQL rather than in Python."""
        try:
            with self._ready().connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT symbol, SUM(quantity) FROM holdings GROUP BY symbol")
                return {row[0]: _num(row[1]) for row in cur.fetchall()}
        except StorageError:
            raise
        except Exception as exc:
            raise _wrap(exc, "summing positions") from exc


class PostgresSubscriptionStore:
    """Plan state in the single-row ``subscription`` table."""

    backend = "postgres"
    in_memory_only = False
    is_durable = True

    def __init__(self, db: Database | None = None):
        self.db = db or get_db()
        self.path = None

    def _ready(self) -> Database:
        self.db.ensure_schema()
        return self.db

    def _read(self) -> dict:
        try:
            with self._ready().connection() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT plan, since, trial_ends_at, source FROM subscription WHERE id = 1")
                row = cur.fetchone()
        except Exception as exc:
            log.warning("Could not read subscription (%s); treating as free.", exc)
            return {"plan": "free"}
        if row is None:
            return {"plan": "free"}
        return {
            "plan": row[0] or "free",
            "since": _iso(row[1]),
            "trial_ends_at": _iso(row[2]),
            "source": row[3],
        }

    def _write(self, data: dict) -> None:
        try:
            with self._ready().connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO subscription (id, plan, since, trial_ends_at, source, updated_at) "
                        "VALUES (1, %s, %s, %s, %s, now()) "
                        "ON CONFLICT (id) DO UPDATE SET plan = EXCLUDED.plan, "
                        "since = EXCLUDED.since, trial_ends_at = EXCLUDED.trial_ends_at, "
                        "source = EXCLUDED.source, updated_at = now()",
                        (data.get("plan", "free"), data.get("since"),
                         data.get("trial_ends_at"), data.get("source")),
                    )
                conn.commit()
        except Exception as exc:
            raise _wrap(exc, "saving the subscription") from exc

    # The plan logic itself lives in premium.py; this class only stores state.
    def state(self) -> dict:
        from .premium import derive_state
        return derive_state(self._read())

    def is_premium(self) -> bool:
        return self.state()["is_premium"]

    def activate(self, plan_id: str, *, trial: bool = False, source: str = "local") -> dict:
        from datetime import timedelta

        from .premium import PRO_PLAN_IDS, TRIAL_DAYS
        from .errors import PremiumRequiredError

        if plan_id not in PRO_PLAN_IDS:
            raise PremiumRequiredError(f"Unknown plan '{plan_id}'.", feature=None)
        now = datetime.now(timezone.utc)
        record = {"plan": plan_id, "since": now.isoformat(), "source": source,
                  "trial_ends_at": None}
        if trial:
            record["trial_ends_at"] = (now + timedelta(days=TRIAL_DAYS)).isoformat()
        self._write(record)
        return self.state()

    def cancel(self) -> dict:
        self._write({"plan": "free", "since": datetime.now(timezone.utc).isoformat(),
                     "trial_ends_at": None, "source": None})
        return self.state()
