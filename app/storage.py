"""Local JSON persistence for the portfolio.

Deliberately a plain file rather than a database: the whole portfolio is a
handful of rows, and a readable ``data/portfolio.json`` the user can back up,
diff or hand-edit is worth more here than a schema. Writes are atomic (write a
temp file, then replace) so a crash mid-save cannot leave a truncated file.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .errors import HoldingNotFoundError, StorageError, ValidationError

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "portfolio.json"

# os.replace can fail on Windows with WinError 32 when a virus scanner or the
# search indexer momentarily opens the temp file we just wrote. The lock clears
# in milliseconds, so retry briefly rather than failing the save.
_REPLACE_ATTEMPTS = 6
_REPLACE_BACKOFF_S = 0.01


def atomic_write_json(path: Path, data: dict, *, what: str = "file") -> None:
    """Write ``data`` as JSON to ``path`` atomically.

    Writes to a temp file in the same directory, fsyncs it, then renames over
    the target, so a crash mid-write cannot truncate the real file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())

        last_error: Exception | None = None
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as exc:  # Windows: file transiently locked
                last_error = exc
                if attempt < _REPLACE_ATTEMPTS - 1:
                    time.sleep(_REPLACE_BACKOFF_S * (2 ** attempt))
        raise last_error  # type: ignore[misc]
    except Exception as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise StorageError(f"Could not save the {what}: {exc}") from exc

MAX_QUANTITY = 1_000_000_000
MAX_PRICE = 10_000_000


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class PortfolioStore:
    def __init__(self, path: Path | str = DEFAULT_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"holdings": [], "created_at": _utcnow_iso()})

    # ---------------------------------------------------------------- io --

    def _read(self) -> dict:
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {"holdings": []}
        except json.JSONDecodeError as exc:
            raise StorageError(
                f"Portfolio file at {self.path} is corrupt and could not be read: {exc}"
            ) from exc
        if not isinstance(data, dict) or not isinstance(data.get("holdings"), list):
            raise StorageError(f"Portfolio file at {self.path} has an unexpected shape.")
        return data

    def _write(self, data: dict) -> None:
        atomic_write_json(self.path, data, what="portfolio")

    # ------------------------------------------------------------ helpers --

    @staticmethod
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

    # --------------------------------------------------------------- crud --

    def list_holdings(self) -> list[dict]:
        with self._lock:
            return list(self._read().get("holdings", []))

    def get_holding(self, holding_id: str) -> dict:
        for h in self.list_holdings():
            if h["id"] == holding_id:
                return h
        raise HoldingNotFoundError(f"No holding with id {holding_id}.")

    def add_holding(
        self,
        symbol: str,
        quantity,
        purchase_price,
        purchase_date: str | None = None,
        notes: str | None = None,
        name: str | None = None,
    ) -> dict:
        q, p = self._validate(quantity, purchase_price)
        holding = {
            "id": uuid.uuid4().hex[:12],
            "symbol": symbol.strip().upper(),
            "name": name,
            "quantity": q,
            "purchase_price": p,
            "purchase_date": purchase_date,
            "notes": (notes or "").strip() or None,
            "created_at": _utcnow_iso(),
            "updated_at": _utcnow_iso(),
        }
        with self._lock:
            data = self._read()
            data.setdefault("holdings", []).append(holding)
            self._write(data)
        return holding

    def update_holding(self, holding_id: str, **fields) -> dict:
        with self._lock:
            data = self._read()
            holdings = data.setdefault("holdings", [])
            for idx, h in enumerate(holdings):
                if h["id"] != holding_id:
                    continue
                updated = dict(h)
                if "symbol" in fields and fields["symbol"]:
                    updated["symbol"] = str(fields["symbol"]).strip().upper()
                if "name" in fields:
                    updated["name"] = fields["name"]
                if fields.get("quantity") is not None:
                    updated["quantity"] = fields["quantity"]
                if fields.get("purchase_price") is not None:
                    updated["purchase_price"] = fields["purchase_price"]
                if "purchase_date" in fields:
                    updated["purchase_date"] = fields["purchase_date"]
                if "notes" in fields:
                    updated["notes"] = (fields["notes"] or "").strip() or None
                q, p = self._validate(updated["quantity"], updated["purchase_price"])
                updated["quantity"], updated["purchase_price"] = q, p
                updated["updated_at"] = _utcnow_iso()
                holdings[idx] = updated
                self._write(data)
                return updated
        raise HoldingNotFoundError(f"No holding with id {holding_id}.")

    def delete_holding(self, holding_id: str) -> dict:
        with self._lock:
            data = self._read()
            holdings = data.setdefault("holdings", [])
            for idx, h in enumerate(holdings):
                if h["id"] == holding_id:
                    removed = holdings.pop(idx)
                    self._write(data)
                    return removed
        raise HoldingNotFoundError(f"No holding with id {holding_id}.")

    def clear(self) -> int:
        with self._lock:
            data = self._read()
            count = len(data.get("holdings", []))
            data["holdings"] = []
            self._write(data)
        return count

    def positions(self) -> dict[str, float]:
        """Total quantity per symbol, merging multiple lots of the same stock."""
        totals: dict[str, float] = {}
        for h in self.list_holdings():
            totals[h["symbol"]] = totals.get(h["symbol"], 0.0) + float(h["quantity"])
        return totals
