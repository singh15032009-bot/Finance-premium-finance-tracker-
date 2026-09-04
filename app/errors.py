"""Error types shared by the market, storage and API layers.

Every error carries a machine-readable ``code`` so the frontend can react
differently to "you typed a bad ticker" versus "Yahoo is unreachable".
"""


class WealthTrackError(Exception):
    """Base class for all application errors."""

    code = "error"
    status_code = 400

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class InvalidSymbolError(WealthTrackError):
    """The ticker does not exist on Yahoo Finance (or has no price data)."""

    code = "invalid_symbol"
    status_code = 400


class MarketUnavailableError(WealthTrackError):
    """Yahoo Finance could not be reached, or returned nothing usable.

    This is deliberately distinct from InvalidSymbolError: the symbol may be
    perfectly valid, we just could not get a real price right now. We never
    substitute an invented price in this case.
    """

    code = "market_unavailable"
    status_code = 503


class HoldingNotFoundError(WealthTrackError):
    code = "not_found"
    status_code = 404


class PremiumRequiredError(WealthTrackError):
    """A Pro-only feature was requested on the free plan.

    Carries the feature id and the upgrade copy so the UI can show a targeted
    prompt rather than a generic error.
    """

    code = "premium_required"
    status_code = 402

    def __init__(self, message: str, *, feature: str | None = None, details: dict | None = None):
        super().__init__(message, details={**(details or {}), "feature": feature})
        self.feature = feature


class ValidationError(WealthTrackError):
    code = "validation_error"
    status_code = 422


class StorageError(WealthTrackError):
    code = "storage_error"
    status_code = 500
