"""Request/response schemas."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field, field_validator


def _validate_iso_date(v: str | None) -> str | None:
    """Accept YYYY-MM-DD or nothing.

    Validated here rather than in a store so both the file and PostgreSQL
    backends reject the same input -- Postgres would refuse a bad date at the
    column, and the file backend would happily save the garbage.
    """
    if v is None or v == "":
        return None
    try:
        return date.fromisoformat(str(v)[:10]).isoformat()
    except ValueError:
        raise ValueError(f"'{v}' is not a valid date. Use YYYY-MM-DD.") from None


class HoldingCreate(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=20, description="Ticker, e.g. AAPL")
    quantity: float = Field(..., gt=0, description="Number of shares (fractions allowed)")
    purchase_price: float = Field(..., gt=0, description="Price paid per share")
    purchase_date: str | None = Field(None, description="ISO date, e.g. 2024-05-01")
    notes: str | None = Field(None, max_length=500)

    @field_validator("symbol")
    @classmethod
    def _clean_symbol(cls, v: str) -> str:
        cleaned = v.strip().upper()
        if not cleaned:
            raise ValueError("Symbol is required.")
        return cleaned

    @field_validator("purchase_date")
    @classmethod
    def _check_date(cls, v: str | None) -> str | None:
        return _validate_iso_date(v)


class PlanActivate(BaseModel):
    """Names a plan to start a trial on, activate, or check out.

    Deliberately carries no payment fields: WealthTrack does not collect card
    details, and a real integration would hand off to the provider's own
    hosted checkout instead.
    """

    plan_id: str = Field(..., description="pro_monthly, pro_annual or lifetime")


class HoldingUpdate(BaseModel):
    symbol: str | None = Field(None, min_length=1, max_length=20)
    quantity: float | None = Field(None, gt=0)
    purchase_price: float | None = Field(None, gt=0)
    purchase_date: str | None = None
    notes: str | None = Field(None, max_length=500)

    @field_validator("symbol")
    @classmethod
    def _clean_symbol(cls, v: str | None) -> str | None:
        return v.strip().upper() if v else v

    @field_validator("purchase_date")
    @classmethod
    def _check_date(cls, v: str | None) -> str | None:
        return _validate_iso_date(v)
