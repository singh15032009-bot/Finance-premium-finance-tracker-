"""Portfolio maths.

All money is computed with ``Decimal`` and rounded half-up to 2 decimal
places. The rounding policy is deliberate and load-bearing:

  * every per-holding figure is rounded to cents first,
  * portfolio totals are the sum of those already-rounded figures.

That ordering is what makes the displayed numbers reconcile -- if you add up
the "Invested" column by hand you get exactly the "Total invested" tile, with
no half-cent drift. ``verify_totals`` re-checks that property on every
response so a regression here shows up immediately.

This module is pure: no network, no I/O, no globals. That is what makes it
straightforward to verify by hand and in tests.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, InvalidOperation

CENTS = Decimal("0.01")
PERCENT = Decimal("0.01")
# Per-share prices keep 4 decimals. Two reasons: fractional-share fills really
# do have sub-cent prices, and Yahoo returns float32 artefacts (AAPL comes back
# as 319.510009765625 when the quoted price is 319.51) which this trims away.
# Quantizing *before* multiplying is what makes "price x qty = value" hold for
# the numbers actually shown on screen.
PRICE = Decimal("0.0001")


def _dec(value) -> Decimal:
    """Convert to Decimal via str so float artefacts do not leak in."""
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"Not a number: {value!r}") from exc


def _money(value) -> Decimal:
    return _dec(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def _pct(value) -> Decimal:
    return _dec(value).quantize(PERCENT, rounding=ROUND_HALF_UP)


def price(value) -> Decimal:
    """Normalise a per-share price to the precision we store and display."""
    return _dec(value).quantize(PRICE, rounding=ROUND_HALF_UP)


def invested_amount(quantity, purchase_price) -> Decimal:
    """What the position cost: quantity x purchase price, rounded to cents."""
    return _money(_dec(quantity) * price(purchase_price))


def current_value(quantity, market_price) -> Decimal:
    """What the position is worth now: quantity x live price, rounded to cents."""
    return _money(_dec(quantity) * price(market_price))


def profit_loss(current, invested) -> Decimal:
    """Absolute gain or loss. Negative means a loss."""
    return _money(_dec(current) - _dec(invested))


def return_percentage(profit, invested) -> Decimal | None:
    """Return as a percentage of the amount invested.

    ``None`` when nothing was invested -- a return on zero cost is undefined,
    and reporting 0% there would be a lie.
    """
    invested_d = _dec(invested)
    if invested_d == 0:
        return None
    return _pct(_dec(profit) / invested_d * Decimal(100))


def compute_holding(holding: dict, quote: dict | None) -> dict:
    """Compute one row of the portfolio.

    ``quote`` is None when no real price could be fetched. In that case the
    market-dependent fields stay None and ``price_available`` is False -- we
    never fall back to the purchase price or zero to fill the gap.
    """
    quantity = _dec(holding["quantity"])
    purchase_price = price(holding["purchase_price"])
    invested = invested_amount(quantity, purchase_price)

    row = {
        "id": holding.get("id"),
        "symbol": holding["symbol"],
        "name": holding.get("name"),
        "quantity": float(quantity),
        "purchase_price": float(purchase_price),
        "purchase_date": holding.get("purchase_date"),
        "notes": holding.get("notes"),
        "invested": float(invested),
        "price_available": quote is not None,
        "current_price": None,
        "current_value": None,
        "profit_loss": None,
        "return_pct": None,
        "day_change": None,
        "day_change_pct": None,
        "currency": None,
        "market_time": None,
        "fetched_at": None,
        "stale": False,
        "price_error": holding.get("price_error"),
    }

    if quote is None:
        return row

    market_price = price(quote["price"])
    value = current_value(quantity, market_price)
    pl = profit_loss(value, invested)

    row.update(
        {
            "current_price": float(market_price),
            "current_value": float(value),
            "profit_loss": float(pl),
            "return_pct": (lambda r: float(r) if r is not None else None)(
                return_percentage(pl, invested)
            ),
            "currency": quote.get("currency"),
            "market_time": quote.get("market_time"),
            "fetched_at": quote.get("fetched_at"),
            "stale": bool(quote.get("stale", False)),
            "name": holding.get("name") or quote.get("short_name"),
        }
    )

    prev_close = quote.get("previous_close")
    if prev_close not in (None, 0):
        prev = price(prev_close)
        day_move = _money((market_price - prev) * quantity)
        row["day_change"] = float(day_move)
        row["day_change_pct"] = float(_pct((market_price - prev) / prev * Decimal(100)))

    return row


def _allocation(rows: list[dict], total_value: Decimal) -> list[dict]:
    """Percentage of portfolio value per holding, summing to exactly 100.00.

    Uses largest-remainder so the slices add up instead of landing on 99.99.
    """
    priced = [r for r in rows if r["price_available"] and r["current_value"] is not None]
    if not priced or total_value <= 0:
        return []

    raw = []
    for r in priced:
        share = _dec(r["current_value"]) / total_value * Decimal(100)
        floored = share.quantize(PERCENT, rounding=ROUND_DOWN)
        raw.append({"row": r, "exact": share, "pct": floored, "rem": share - floored})

    allocated = sum(item["pct"] for item in raw)
    shortfall = (Decimal(100) - allocated).quantize(PERCENT)
    steps = int((shortfall / PERCENT).to_integral_value())
    for item in sorted(raw, key=lambda i: i["rem"], reverse=True)[: max(steps, 0)]:
        item["pct"] += PERCENT

    return [
        {
            "symbol": item["row"]["symbol"],
            "value": item["row"]["current_value"],
            "pct": float(item["pct"]),
        }
        for item in sorted(raw, key=lambda i: i["row"]["current_value"], reverse=True)
    ]


def compute_portfolio(
    holdings: list[dict],
    quotes: dict[str, dict],
    price_errors: dict[str, dict] | None = None,
) -> dict:
    """Build the full portfolio view: per-holding rows, totals and allocation."""
    price_errors = price_errors or {}
    rows: list[dict] = []

    for h in holdings:
        symbol = h["symbol"]
        quote = quotes.get(symbol)
        enriched = dict(h)
        if quote is None and symbol in price_errors:
            enriched["price_error"] = price_errors[symbol].get("message")
        rows.append(compute_holding(enriched, quote))

    # Totals are the sum of the rounded per-row figures, so the columns add up.
    total_invested = sum((_dec(r["invested"]) for r in rows), Decimal(0))
    priced_rows = [r for r in rows if r["price_available"]]
    total_value = sum((_dec(r["current_value"]) for r in priced_rows), Decimal(0))
    # Cost basis of only the positions we could actually price -- the honest
    # denominator for the return percentage.
    priced_invested = sum((_dec(r["invested"]) for r in priced_rows), Decimal(0))
    total_pl = profit_loss(total_value, priced_invested)
    total_return = return_percentage(total_pl, priced_invested)

    day_rows = [r for r in priced_rows if r["day_change"] is not None]
    total_day_change = sum((_dec(r["day_change"]) for r in day_rows), Decimal(0))
    prior_value = total_value - total_day_change
    day_change_pct = (
        _pct(total_day_change / prior_value * Decimal(100)) if prior_value > 0 else None
    )

    unpriced = [r["symbol"] for r in rows if not r["price_available"]]
    warnings: list[str] = []
    if unpriced:
        warnings.append(
            "No live price for "
            + ", ".join(sorted(set(unpriced)))
            + ". These holdings are excluded from the totals below."
        )
    if any(r["stale"] for r in priced_rows):
        stale_syms = sorted({r["symbol"] for r in priced_rows if r["stale"]})
        warnings.append(
            "Showing the last price we successfully fetched from Yahoo Finance for "
            + ", ".join(stale_syms)
            + " -- Yahoo was unreachable on the latest refresh."
        )

    totals = {
        "holdings_count": len(rows),
        "priced_count": len(priced_rows),
        "unpriced_count": len(rows) - len(priced_rows),
        "total_invested": float(_money(total_invested)),
        "priced_invested": float(_money(priced_invested)),
        "total_value": float(_money(total_value)),
        "total_profit_loss": float(total_pl),
        "total_return_pct": float(total_return) if total_return is not None else None,
        "day_change": float(_money(total_day_change)) if day_rows else None,
        "day_change_pct": float(day_change_pct) if day_change_pct is not None and day_rows else None,
    }

    return {
        "holdings": rows,
        "totals": totals,
        "allocation": _allocation(rows, total_value),
        "warnings": warnings,
        "unpriced_symbols": sorted(set(unpriced)),
    }


def verify_totals(portfolio: dict) -> list[str]:
    """Self-check the arithmetic. Returns a list of problems; empty means correct.

    Run on every ``/api/portfolio`` response and asserted in the test suite, so
    a rounding regression surfaces as a visible warning rather than silently
    wrong money.
    """
    problems: list[str] = []
    rows = portfolio["holdings"]
    totals = portfolio["totals"]

    # 1. Every row's own arithmetic.
    for r in rows:
        expected_invested = invested_amount(r["quantity"], r["purchase_price"])
        if _money(r["invested"]) != expected_invested:
            problems.append(
                f"{r['symbol']}: invested {r['invested']} != "
                f"{r['quantity']} x {r['purchase_price']} = {expected_invested}"
            )
        if not r["price_available"]:
            if r["current_value"] is not None or r["profit_loss"] is not None:
                problems.append(f"{r['symbol']}: unpriced row carries market values")
            continue
        expected_value = current_value(r["quantity"], r["current_price"])
        if _money(r["current_value"]) != expected_value:
            problems.append(
                f"{r['symbol']}: current value {r['current_value']} != "
                f"{r['quantity']} x {r['current_price']} = {expected_value}"
            )
        expected_pl = profit_loss(expected_value, expected_invested)
        if _money(r["profit_loss"]) != expected_pl:
            problems.append(
                f"{r['symbol']}: P/L {r['profit_loss']} != "
                f"{expected_value} - {expected_invested} = {expected_pl}"
            )
        expected_ret = return_percentage(expected_pl, expected_invested)
        actual_ret = _pct(r["return_pct"]) if r["return_pct"] is not None else None
        if expected_ret != actual_ret:
            problems.append(
                f"{r['symbol']}: return {r['return_pct']}% != expected {expected_ret}%"
            )

    # 2. Totals equal the sum of the rows.
    sum_invested = sum((_dec(r["invested"]) for r in rows), Decimal(0))
    if _money(totals["total_invested"]) != _money(sum_invested):
        problems.append(
            f"total_invested {totals['total_invested']} != sum of rows {_money(sum_invested)}"
        )

    priced = [r for r in rows if r["price_available"]]
    sum_value = sum((_dec(r["current_value"]) for r in priced), Decimal(0))
    if _money(totals["total_value"]) != _money(sum_value):
        problems.append(
            f"total_value {totals['total_value']} != sum of rows {_money(sum_value)}"
        )

    sum_priced_invested = sum((_dec(r["invested"]) for r in priced), Decimal(0))
    expected_total_pl = profit_loss(sum_value, sum_priced_invested)
    if _money(totals["total_profit_loss"]) != expected_total_pl:
        problems.append(
            f"total_profit_loss {totals['total_profit_loss']} != {expected_total_pl}"
        )

    expected_total_ret = return_percentage(expected_total_pl, sum_priced_invested)
    actual_total_ret = (
        _pct(totals["total_return_pct"]) if totals["total_return_pct"] is not None else None
    )
    if expected_total_ret != actual_total_ret:
        problems.append(
            f"total_return_pct {totals['total_return_pct']} != expected {expected_total_ret}"
        )

    # 3. Allocation slices add to 100%.
    alloc = portfolio.get("allocation") or []
    if alloc:
        alloc_sum = sum((_dec(a["pct"]) for a in alloc), Decimal(0))
        if _pct(alloc_sum) != Decimal("100.00"):
            problems.append(f"allocation percentages sum to {alloc_sum}, expected 100.00")

    return problems
