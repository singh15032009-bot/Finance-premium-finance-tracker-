"""Calculation tests.

The expected values here are worked out by hand and written as literals on
purpose -- if they were computed with the same code under test the test would
prove nothing.
"""

import pytest

from app.calculations import (
    compute_holding,
    compute_portfolio,
    current_value,
    invested_amount,
    profit_loss,
    return_percentage,
    verify_totals,
)


def q(symbol, price, **extra):
    base = {"price": price, "currency": "USD", "short_name": symbol,
            "market_time": "2026-09-04T15:35:24+00:00",
            "fetched_at": "2026-09-04T15:35:30+00:00", "stale": False}
    base.update(extra)
    return base


# ------------------------------------------------------------- primitives --


def test_invested_amount_hand_checked():
    # 10 shares at 185.50 = 1855.00
    assert float(invested_amount(10, 185.50)) == 1855.00
    # 3 shares at 33.33 = 99.99
    assert float(invested_amount(3, 33.33)) == 99.99


def test_invested_amount_fractional_shares():
    # 0.5 shares at 401.11 = 200.555 -> rounds half up to 200.56
    assert float(invested_amount(0.5, 401.11)) == 200.56


def test_current_value_hand_checked():
    # 10 shares at 319.51 = 3195.10
    assert float(current_value(10, 319.51)) == 3195.10


def test_profit_loss_positive_and_negative():
    assert float(profit_loss(3195.10, 1855.00)) == 1340.10
    assert float(profit_loss(900.00, 1000.00)) == -100.00


def test_return_percentage_hand_checked():
    # 1340.10 / 1855.00 = 0.722426... -> 72.24%
    assert float(return_percentage(1340.10, 1855.00)) == 72.24
    # a clean 50% loss
    assert float(return_percentage(-500, 1000)) == -50.00
    # exact doubling
    assert float(return_percentage(1000, 1000)) == 100.00


def test_return_percentage_undefined_on_zero_cost():
    assert return_percentage(100, 0) is None


def test_rounding_is_half_up_not_bankers():
    # Python's round() would give 2.66 here (banker's rounding); money should be 2.67
    assert float(invested_amount(1, 2.665)) == 2.67


def test_sub_cent_purchase_price_is_preserved():
    """Fractional-share fills really do have 4dp prices; do not round them away."""
    row = compute_holding(
        {"id": "x", "symbol": "X", "quantity": 3, "purchase_price": 33.335}, None
    )
    assert row["purchase_price"] == 33.335
    # and the displayed price still reproduces the displayed invested amount
    assert row["invested"] == 100.01          # 3 x 33.335 = 100.005 -> half-up


def test_float32_artefacts_from_yahoo_are_trimmed():
    """Yahoo returns 319.510009765625 for a stock quoted at 319.51."""
    row = compute_holding(
        {"id": "x", "symbol": "AAPL", "quantity": 1000, "purchase_price": 300.00},
        q("AAPL", 319.510009765625),
    )
    assert row["current_price"] == 319.51
    # 1000 x 319.51 exactly -- not 319510.01 from the float noise
    assert row["current_value"] == 319510.00


# ---------------------------------------------------------------- holding --


def test_compute_holding_full_row():
    row = compute_holding(
        {"id": "a1", "symbol": "AAPL", "quantity": 10, "purchase_price": 185.50},
        q("AAPL", 319.51, previous_close=317.00),
    )
    assert row["invested"] == 1855.00
    assert row["current_value"] == 3195.10
    assert row["profit_loss"] == 1340.10
    assert row["return_pct"] == 72.24
    assert row["price_available"] is True
    # day move: (319.51 - 317.00) * 10 = 25.10
    assert row["day_change"] == 25.10


def test_compute_holding_without_price_never_invents_one():
    row = compute_holding(
        {"id": "b2", "symbol": "AAPL", "quantity": 10, "purchase_price": 185.50}, None
    )
    assert row["price_available"] is False
    assert row["current_price"] is None
    assert row["current_value"] is None
    assert row["profit_loss"] is None
    assert row["return_pct"] is None
    # cost basis is still known -- it does not depend on the market
    assert row["invested"] == 1855.00


# -------------------------------------------------------------- portfolio --


def test_portfolio_totals_hand_checked():
    holdings = [
        {"id": "1", "symbol": "AAPL", "quantity": 10, "purchase_price": 185.50},
        {"id": "2", "symbol": "MSFT", "quantity": 5, "purchase_price": 400.00},
        {"id": "3", "symbol": "TSLA", "quantity": 20, "purchase_price": 250.00},
    ]
    quotes = {"AAPL": q("AAPL", 319.51), "MSFT": q("MSFT", 500.38), "TSLA": q("TSLA", 353.42)}
    p = compute_portfolio(holdings, quotes)
    t = p["totals"]

    # invested:  1855.00 + 2000.00 + 5000.00 = 8855.00
    assert t["total_invested"] == 8855.00
    # value:     3195.10 + 2501.90 + 7068.40 = 12765.40
    assert t["total_value"] == 12765.40
    # P/L:       12765.40 - 8855.00 = 3910.40
    assert t["total_profit_loss"] == 3910.40
    # return:    3910.40 / 8855.00 = 0.441604... -> 44.16%
    assert t["total_return_pct"] == 44.16
    assert verify_totals(p) == []


def test_unpriced_holding_excluded_from_totals_but_flagged():
    holdings = [
        {"id": "1", "symbol": "AAPL", "quantity": 10, "purchase_price": 185.50},
        {"id": "2", "symbol": "BROKEN", "quantity": 5, "purchase_price": 100.00},
    ]
    quotes = {"AAPL": q("AAPL", 319.51)}
    errors = {"BROKEN": {"code": "market_unavailable", "message": "Yahoo unreachable"}}
    p = compute_portfolio(holdings, quotes, errors)
    t = p["totals"]

    # cost basis of everything the user owns
    assert t["total_invested"] == 2355.00
    # ...but only the priced position contributes to market value
    assert t["total_value"] == 3195.10
    assert t["priced_invested"] == 1855.00
    assert t["total_profit_loss"] == 1340.10
    assert t["total_return_pct"] == 72.24
    assert t["unpriced_count"] == 1
    assert p["unpriced_symbols"] == ["BROKEN"]
    assert any("BROKEN" in w for w in p["warnings"])
    assert verify_totals(p) == []


def test_totals_are_sum_of_displayed_rows():
    """The columns must add up to the tiles, exactly, with no drift."""
    holdings = [
        {"id": str(i), "symbol": f"S{i}", "quantity": 3, "purchase_price": 33.335}
        for i in range(7)
    ]
    quotes = {f"S{i}": q(f"S{i}", 77.775) for i in range(7)}
    p = compute_portfolio(holdings, quotes)

    assert round(sum(r["invested"] for r in p["holdings"]), 2) == p["totals"]["total_invested"]
    assert round(sum(r["current_value"] for r in p["holdings"]), 2) == p["totals"]["total_value"]
    assert round(sum(r["profit_loss"] for r in p["holdings"]), 2) == p["totals"]["total_profit_loss"]
    assert verify_totals(p) == []


def test_allocation_sums_to_exactly_100():
    # three thirds is the classic case where naive rounding gives 99.99
    holdings = [
        {"id": "1", "symbol": "A", "quantity": 1, "purchase_price": 10},
        {"id": "2", "symbol": "B", "quantity": 1, "purchase_price": 10},
        {"id": "3", "symbol": "C", "quantity": 1, "purchase_price": 10},
    ]
    quotes = {s: q(s, 100.00) for s in ("A", "B", "C")}
    p = compute_portfolio(holdings, quotes)
    assert sum(a["pct"] for a in p["allocation"]) == pytest.approx(100.00)
    assert verify_totals(p) == []


def test_multiple_lots_of_same_symbol():
    holdings = [
        {"id": "1", "symbol": "AAPL", "quantity": 10, "purchase_price": 150.00},
        {"id": "2", "symbol": "AAPL", "quantity": 5, "purchase_price": 200.00},
    ]
    p = compute_portfolio(holdings, {"AAPL": q("AAPL", 319.51)})
    t = p["totals"]
    # 1500 + 1000 = 2500 invested; 15 shares x 319.51 = 4792.65
    assert t["total_invested"] == 2500.00
    assert t["total_value"] == 4792.65
    assert t["total_profit_loss"] == 2292.65
    assert verify_totals(p) == []


def test_loss_making_portfolio():
    holdings = [{"id": "1", "symbol": "X", "quantity": 100, "purchase_price": 50.00}]
    p = compute_portfolio(holdings, {"X": q("X", 30.00)})
    t = p["totals"]
    assert t["total_invested"] == 5000.00
    assert t["total_value"] == 3000.00
    assert t["total_profit_loss"] == -2000.00
    assert t["total_return_pct"] == -40.00
    assert verify_totals(p) == []


def test_empty_portfolio():
    p = compute_portfolio([], {})
    t = p["totals"]
    assert t["total_invested"] == 0
    assert t["total_value"] == 0
    assert t["total_profit_loss"] == 0
    assert t["total_return_pct"] is None
    assert p["allocation"] == []
    assert verify_totals(p) == []


def test_verify_totals_catches_tampering():
    holdings = [{"id": "1", "symbol": "AAPL", "quantity": 10, "purchase_price": 185.50}]
    p = compute_portfolio(holdings, {"AAPL": q("AAPL", 319.51)})
    assert verify_totals(p) == []
    p["totals"]["total_value"] = 9999.99          # corrupt it
    assert verify_totals(p), "verification should have caught the bad total"
