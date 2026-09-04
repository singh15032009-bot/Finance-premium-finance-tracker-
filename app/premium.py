"""Plans, pricing and the free/Pro feature gate.

This module is the single source of truth for what each plan includes. The
pricing table the user sees is generated from ``PLANS`` and ``FEATURE_MATRIX``,
and the server-side gate reads the same constants -- so the marketing copy and
the actual entitlement can never drift apart.

Note on payments: activating Pro here flips a local flag. There is no payment
processor wired up, and this module deliberately does not collect card details.
See ``CHECKOUT_NOTE`` and the README for what connecting Stripe would involve.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import SUBSCRIPTION_PATH
from .errors import PremiumRequiredError, StorageError
from .storage import atomic_write_json

log = logging.getLogger("wealthtrack.premium")

DEFAULT_PATH = SUBSCRIPTION_PATH

# --- free-plan limits ------------------------------------------------------
FREE_MAX_HOLDINGS = 10
FREE_MAX_HISTORY_PERIOD = "1y"
FREE_REFRESH_SECONDS = 60
PRO_REFRESH_SECONDS = 10

# Periods in ascending order; anything past FREE_MAX_HISTORY_PERIOD is Pro-only.
PERIOD_ORDER = ["1d", "5d", "1mo", "3mo", "6mo", "ytd", "1y", "2y", "5y", "10y", "max"]

TRIAL_DAYS = 14

CHECKOUT_NOTE = (
    "Card checkout is not connected in this build. Start the free trial to "
    "unlock Pro locally, or wire up a payment provider to take real payments."
)

# --- plans -----------------------------------------------------------------

PLANS = {
    "free": {
        "id": "free",
        "name": "Free",
        "tagline": "Track a starter portfolio with real market data.",
        "price": 0.00,
        "period": "forever",
        "price_label": "$0",
        "cta": "Your current plan",
        "highlight": False,
    },
    "pro_monthly": {
        "id": "pro_monthly",
        "name": "Pro",
        "tagline": "Everything you need to actually manage a portfolio.",
        "price": 7.99,
        "period": "month",
        "price_label": "$7.99",
        "cta": f"Start {TRIAL_DAYS}-day free trial",
        "highlight": True,
    },
    "pro_annual": {
        "id": "pro_annual",
        "name": "Pro",
        "tagline": "Everything you need to actually manage a portfolio.",
        "price": 71.88,
        "period": "year",
        "price_label": "$5.99",
        "price_sublabel": "per month, billed annually",
        "savings": "Save 25%",
        "cta": f"Start {TRIAL_DAYS}-day free trial",
        "highlight": True,
    },
    "lifetime": {
        "id": "lifetime",
        "name": "Lifetime",
        "tagline": "Pay once. Every Pro feature, including everything we ship next.",
        "price": 149.00,
        "period": "one-off",
        "price_label": "$149",
        "price_sublabel": "one-time payment",
        "cta": "Buy lifetime access",
        "highlight": False,
    },
}

PRO_PLAN_IDS = {"pro_monthly", "pro_annual", "lifetime"}

# --- the headline features that carry the upsell ---------------------------
# Each one is backed by a real, working endpoint -- nothing here is vapour.

PREMIUM_FEATURES = [
    {
        "id": "dividends",
        "icon": "◈",
        "name": "Dividend income tracker",
        "blurb": "See exactly what your portfolio pays you. Annual income, yield "
                 "on cost, per-holding breakdown and the next ex-dividend dates.",
    },
    {
        "id": "sectors",
        "icon": "◐",
        "name": "Sector & risk analytics",
        "blurb": "Find out you are 70% technology before the market tells you. "
                 "Sector and country exposure, portfolio beta and a "
                 "diversification score.",
    },
    {
        "id": "benchmark",
        "icon": "◭",
        "name": "Benchmark vs S&P 500",
        "blurb": "The only question that matters: are you beating the index? "
                 "Track your return against the S&P 500 over any period.",
    },
    {
        "id": "unlimited",
        "icon": "∞",
        "name": "Unlimited holdings",
        "blurb": f"The free plan stops at {FREE_MAX_HOLDINGS} positions. Pro has no "
                 "limit, so your whole portfolio lives in one place.",
    },
    {
        "id": "history",
        "icon": "◱",
        "name": "10-year history",
        "blurb": "Free goes back 1 year. Pro unlocks 2, 5 and 10-year views plus "
                 "the full available history.",
    },
    {
        "id": "refresh",
        "icon": "↻",
        "name": f"{PRO_REFRESH_SECONDS}-second price refresh",
        "blurb": f"Free refreshes every {FREE_REFRESH_SECONDS} seconds. Pro keeps "
                 "your position values close to live through the trading day.",
    },
]

# --- the comparison table --------------------------------------------------

FEATURE_MATRIX = [
    {
        "group": "Portfolio",
        "rows": [
            {"name": "Holdings tracked", "free": f"Up to {FREE_MAX_HOLDINGS}", "pro": "Unlimited"},
            {"name": "Live Yahoo Finance prices", "free": True, "pro": True},
            {"name": "Profit / loss and return %", "free": True, "pro": True},
            {"name": "Allocation chart", "free": True, "pro": True},
            {"name": "Price refresh rate",
             "free": f"{FREE_REFRESH_SECONDS} seconds", "pro": f"{PRO_REFRESH_SECONDS} seconds"},
        ],
    },
    {
        "group": "Insights",
        "rows": [
            {"name": "Dividend income & yield on cost", "free": False, "pro": True},
            {"name": "Next ex-dividend dates", "free": False, "pro": True},
            {"name": "Sector & country exposure", "free": False, "pro": True},
            {"name": "Portfolio beta & diversification score", "free": False, "pro": True},
            {"name": "Benchmark vs S&P 500", "free": False, "pro": True},
        ],
    },
    {
        "group": "Data",
        "rows": [
            {"name": "Price history", "free": "1 year", "pro": "10 years + full history"},
            {"name": "Export to CSV", "free": False, "pro": True},
            {"name": "Portfolio value history", "free": True, "pro": True},
        ],
    },
    {
        "group": "Support",
        "rows": [
            {"name": "Community support", "free": True, "pro": True},
            {"name": "Priority email support", "free": False, "pro": True},
            {"name": "Early access to new features", "free": False, "pro": True},
        ],
    },
]

# Gate messages, keyed by feature id.
_GATE_COPY = {
    "dividends": "Dividend income tracking is a Pro feature.",
    "sectors": "Sector and risk analytics are a Pro feature.",
    "benchmark": "Benchmark comparison is a Pro feature.",
    "export": "CSV export is a Pro feature.",
    "history": f"History beyond {FREE_MAX_HISTORY_PERIOD} is a Pro feature.",
    "unlimited": f"The free plan is limited to {FREE_MAX_HOLDINGS} holdings.",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def derive_state(data: dict) -> dict:
    """Turn a stored subscription record into the plan state the app uses.

    Shared by the file and PostgreSQL backends so entitlement is decided in
    exactly one place regardless of where the record came from.
    """
    plan_id = data.get("plan", "free")
    trial_ends = data.get("trial_ends_at")
    expired = False

    if trial_ends:
        try:
            expired = datetime.fromisoformat(trial_ends) <= _utcnow()
        except (ValueError, TypeError):
            expired = False

    active = plan_id in PRO_PLAN_IDS and not expired
    effective = plan_id if active else "free"

    days_left = None
    if trial_ends and not expired:
        try:
            days_left = max(0, (datetime.fromisoformat(trial_ends) - _utcnow()).days)
        except (ValueError, TypeError):
            days_left = None

    return {
        "plan": effective,
        "plan_name": PLANS.get(effective, PLANS["free"])["name"],
        "is_premium": active,
        "is_trial": bool(trial_ends) and active,
        "trial_ends_at": trial_ends if active else None,
        "trial_days_left": days_left,
        "trial_expired": expired,
        "since": data.get("since"),
        "refresh_seconds": PRO_REFRESH_SECONDS if active else FREE_REFRESH_SECONDS,
        "max_holdings": None if active else FREE_MAX_HOLDINGS,
        "max_history_period": None if active else FREE_MAX_HISTORY_PERIOD,
    }


class SubscriptionStore:
    """Local subscription state. One user, one machine, one JSON file."""

    def __init__(self, path: Path | str = DEFAULT_PATH):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._memory: dict | None = None

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if not self.path.exists():
                self._write({"plan": "free", "since": _utcnow().isoformat()})
        except (OSError, StorageError) as exc:
            log.warning(
                "Subscription storage at %s is not writable (%s); using "
                "in-memory state for this process.", self.path, exc,
            )
            self._memory = {"plan": "free", "since": _utcnow().isoformat()}

    backend = "file"

    @property
    def in_memory_only(self) -> bool:
        return self._memory is not None

    def _read(self) -> dict:
        if self._memory is not None:
            return self._memory
        try:
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {"plan": "free"}
        except json.JSONDecodeError:
            # A corrupt subscription file must not lock the app up; fail to free.
            return {"plan": "free"}
        return data if isinstance(data, dict) else {"plan": "free"}

    def _write(self, data: dict) -> None:
        if self._memory is not None:
            self._memory = data
            return
        atomic_write_json(self.path, data, what="subscription")

    # ------------------------------------------------------------- state --

    def state(self) -> dict:
        with self._lock:
            data = self._read()
        return derive_state(data)

    def is_premium(self) -> bool:
        return self.state()["is_premium"]

    def activate(self, plan_id: str, *, trial: bool = False, source: str = "local") -> dict:
        if plan_id not in PRO_PLAN_IDS:
            raise PremiumRequiredError(f"Unknown plan '{plan_id}'.", feature=None)
        record = {
            "plan": plan_id,
            "since": _utcnow().isoformat(),
            "source": source,
        }
        if trial:
            record["trial_ends_at"] = (_utcnow() + timedelta(days=TRIAL_DAYS)).isoformat()
        with self._lock:
            self._write(record)
        return self.state()

    def cancel(self) -> dict:
        with self._lock:
            self._write({"plan": "free", "since": _utcnow().isoformat()})
        return self.state()


# --- gating helpers --------------------------------------------------------


def require(store: SubscriptionStore, feature: str) -> None:
    """Raise PremiumRequiredError unless the current plan includes ``feature``."""
    if store.is_premium():
        return
    raise PremiumRequiredError(
        _GATE_COPY.get(feature, "That is a Pro feature."),
        feature=feature,
        details={"upgrade_url": "/#premium"},
    )


def check_holding_limit(store: SubscriptionStore, current_count: int) -> None:
    if store.is_premium():
        return
    if current_count >= FREE_MAX_HOLDINGS:
        raise PremiumRequiredError(
            f"The free plan tracks up to {FREE_MAX_HOLDINGS} holdings and you "
            f"already have {current_count}. Upgrade to Pro for unlimited holdings.",
            feature="unlimited",
            details={"limit": FREE_MAX_HOLDINGS, "current": current_count},
        )


def check_history_period(store: SubscriptionStore, period: str) -> None:
    if store.is_premium():
        return
    try:
        requested = PERIOD_ORDER.index(period)
        allowed = PERIOD_ORDER.index(FREE_MAX_HISTORY_PERIOD)
    except ValueError:
        return  # unknown period; let the market layer reject it
    if requested > allowed:
        raise PremiumRequiredError(
            f"The free plan shows up to {FREE_MAX_HISTORY_PERIOD} of history. "
            f"Upgrade to Pro for 2, 5 and 10-year views.",
            feature="history",
            details={"requested": period, "max_free": FREE_MAX_HISTORY_PERIOD},
        )


def create_subscription_store():
    """PostgreSQL when configured, otherwise the local file store."""
    from . import db

    if db.is_configured():
        from .pg_storage import PostgresSubscriptionStore

        return PostgresSubscriptionStore()
    return SubscriptionStore()


def pricing_payload(store: SubscriptionStore) -> dict:
    return {
        "plans": PLANS,
        "premium_features": PREMIUM_FEATURES,
        "feature_matrix": FEATURE_MATRIX,
        "trial_days": TRIAL_DAYS,
        "checkout_note": CHECKOUT_NOTE,
        "subscription": store.state(),
    }
