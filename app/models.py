"""Request/response schemas."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


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
