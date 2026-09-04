"""End-to-end QA against a running WealthTrack server and live Yahoo Finance.

Not part of the pytest suite -- this is the acceptance check. It builds a real
portfolio through the HTTP API, then verifies every displayed number two ways:

  1. against an *independent* yfinance call made by this script, and
  2. against arithmetic recomputed here from scratch with Decimal.

Run with the server up:  python tests/qa_live_check.py
"""

import json
import sys
import urllib.error
import urllib.request
from decimal import Decimal, ROUND_HALF_UP

import yfinance as yf

BASE = "http://127.0.0.1:8000"
PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, ok, detail=""):
    results.append((PASS if ok else FAIL, name, detail))
    mark = "[PASS]" if ok else "[FAIL]"
    print(f"{mark} {name}" + (f"\n         {detail}" if detail else ""))
    return ok


def call(method, path, payload=None):
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def money(x):
    return Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def price(x):
    return Decimal(str(x)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


LOTS = [
    {"symbol": "AAPL", "quantity": 10, "purchase_price": 185.50},
    {"symbol": "MSFT", "quantity": 5, "purchase_price": 400.00},
    {"symbol": "TSLA", "quantity": 20, "purchase_price": 250.00},
]

print("=" * 74)
print("WealthTrack QA -- live Yahoo Finance acceptance check")
print("=" * 74)

# -- 0. server up ----------------------------------------------------------
status, health = call("GET", "/api/health")
check("Server is healthy", status == 200 and health.get("status") == "ok", str(health))

# -- 1. clean slate --------------------------------------------------------
call("DELETE", "/api/holdings")
status, body = call("GET", "/api/portfolio")
check("Empty portfolio reports zero totals",
      body["totals"]["total_invested"] == 0 and body["totals"]["total_value"] == 0)

# -- 2. build a real portfolio --------------------------------------------
print("\n--- Building portfolio with real tickers ---")
ids = {}
for lot in LOTS:
    status, body = call("POST", "/api/holdings", lot)
    ok = status == 201
    if ok:
        ids[lot["symbol"]] = body["holding"]["id"]
    check(f"Added {lot['quantity']} x {lot['symbol']} @ {lot['purchase_price']}",
          ok, "" if ok else str(body))

# -- 3. invalid symbols rejected ------------------------------------------
print("\n--- Invalid symbol handling ---")
for bad in ["NOTAREALTICKER123", "ZZZZZZZZ", "FAKE!!!"]:
    status, body = call("POST", "/api/holdings",
                        {"symbol": bad, "quantity": 1, "purchase_price": 10})
    ok = status == 400 and body.get("error", {}).get("code") == "invalid_symbol"
    check(f"Rejected invalid symbol {bad!r} with a clear error", ok,
          body.get("error", {}).get("message", str(body))[:120])

status, body = call("GET", "/api/holdings")
check("Invalid symbols were not saved", len(body["holdings"]) == len(LOTS),
      f"{len(body['holdings'])} holdings stored")

status, body = call("GET", "/api/quote/NOTAREALTICKER123")
check("GET /api/quote on a bad ticker returns 400 invalid_symbol",
      status == 400 and body.get("error", {}).get("code") == "invalid_symbol")

status, body = call("GET", "/api/validate/NOTAREALTICKER123")
check("Validate endpoint reports invalid without raising",
      status == 200 and body.get("valid") is False)

# -- 4. compare prices with an independent Yahoo call ---------------------
print("\n--- Price accuracy vs independent Yahoo Finance call ---")
status, portfolio = call("POST", "/api/portfolio/refresh")
check("Refresh endpoint returned 200", status == 200)

app_prices = {h["symbol"]: h["current_price"] for h in portfolio["holdings"]}
for symbol in app_prices:
    md = yf.Ticker(symbol).get_history_metadata()
    independent = md.get("regularMarketPrice")
    app_price = app_prices[symbol]
    # Same quote, independently fetched. Allow a small drift for ticks between
    # the two calls; anything beyond 1% means we are not showing the real price.
    drift = abs(app_price - independent) / independent * 100
    check(f"{symbol}: app shows {app_price}, Yahoo says {independent} "
          f"(drift {drift:.4f}%)", drift < 1.0)

# -- 5. verify every calculation by hand ----------------------------------
print("\n--- Manual verification of every calculation ---")
total_invested = Decimal("0")
total_value = Decimal("0")

for h in portfolio["holdings"]:
    qty = Decimal(str(h["quantity"]))
    buy = price(h["purchase_price"])
    now = price(h["current_price"])

    exp_invested = money(qty * buy)
    exp_value = money(qty * now)
    exp_pl = money(exp_value - exp_invested)
    exp_ret = (exp_pl / exp_invested * 100).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    total_invested += exp_invested
    total_value += exp_value

    check(f"{h['symbol']} invested: {qty} x {buy} = {exp_invested}",
          money(h["invested"]) == exp_invested,
          f"app said {h['invested']}")
    check(f"{h['symbol']} current value: {qty} x {now} = {exp_value}",
          money(h["current_value"]) == exp_value,
          f"app said {h['current_value']}")
    check(f"{h['symbol']} P/L: {exp_value} - {exp_invested} = {exp_pl}",
          money(h["profit_loss"]) == exp_pl,
          f"app said {h['profit_loss']}")
    check(f"{h['symbol']} return: {exp_pl}/{exp_invested} = {exp_ret}%",
          Decimal(str(h["return_pct"])) == exp_ret,
          f"app said {h['return_pct']}")

exp_total_pl = money(total_value - total_invested)
exp_total_ret = (exp_total_pl / total_invested * 100).quantize(
    Decimal("0.01"), rounding=ROUND_HALF_UP)
t = portfolio["totals"]

print()
check(f"TOTAL invested = {total_invested} (sum of rows)",
      money(t["total_invested"]) == total_invested, f"app said {t['total_invested']}")
check(f"TOTAL value = {total_value} (sum of rows)",
      money(t["total_value"]) == total_value, f"app said {t['total_value']}")
check(f"TOTAL P/L = {exp_total_pl}",
      money(t["total_profit_loss"]) == exp_total_pl, f"app said {t['total_profit_loss']}")
check(f"TOTAL return = {exp_total_ret}%",
      Decimal(str(t["total_return_pct"])) == exp_total_ret,
      f"app said {t['total_return_pct']}")
check("Backend self-check reports totals verified", portfolio.get("totals_verified") is True)
check("Allocation percentages sum to exactly 100.00",
      round(sum(a["pct"] for a in portfolio["allocation"]), 2) == 100.00,
      str([(a["symbol"], a["pct"]) for a in portfolio["allocation"]]))

# -- 6. freshness ----------------------------------------------------------
print("\n--- Price freshness ---")
for h in portfolio["holdings"]:
    has_stamp = bool(h["market_time"] or h["fetched_at"])
    check(f"{h['symbol']} carries a last-updated timestamp", has_stamp,
          f"market_time={h['market_time']} fetched_at={h['fetched_at']}")

# -- 7. history ------------------------------------------------------------
print("\n--- Historical data ---")
status, hist = call("GET", "/api/portfolio/history?period=1mo")
check("Portfolio history returns a real series",
      status == 200 and len(hist["points"]) > 5, f"{len(hist.get('points', []))} points")
check("History has no missing symbols", not hist.get("missing"), str(hist.get("missing")))
if hist.get("points"):
    dates = [p["date"] for p in hist["points"]]
    check("History is chronological", dates == sorted(dates))
    check("History values are positive", all(p["value"] > 0 for p in hist["points"]))
    # Cross-check the final history point against today's computed value.
    last = hist["points"][-1]["value"]
    drift = abs(last - t["total_value"]) / t["total_value"] * 100
    check(f"Latest history point {last} is close to live total {t['total_value']} "
          f"(drift {drift:.3f}%)", drift < 5.0,
          "intraday moves after the last daily close explain small gaps")

# -- 8. edit and delete ----------------------------------------------------
print("\n--- Edit and delete ---")
aapl_id = ids["AAPL"]
status, body = call("PUT", f"/api/holdings/{aapl_id}",
                    {"quantity": 25, "purchase_price": 190.00})
check("Edited AAPL to 25 shares @ 190.00",
      status == 200 and body["holding"]["quantity"] == 25)

status, portfolio = call("GET", "/api/portfolio")
aapl = next(h for h in portfolio["holdings"] if h["symbol"] == "AAPL")
check(f"Edit recalculated invested to {money(Decimal('25') * price(190.00))}",
      money(aapl["invested"]) == money(Decimal("25") * price(190.00)),
      f"app said {aapl['invested']}")

status, _ = call("DELETE", f"/api/holdings/{ids['TSLA']}")
check("Deleted TSLA", status == 200)
status, portfolio = call("GET", "/api/portfolio")
check("TSLA is gone from the portfolio",
      "TSLA" not in [h["symbol"] for h in portfolio["holdings"]])
check("Totals recomputed after delete, still verified",
      portfolio["totals_verified"] is True)

status, body = call("DELETE", f"/api/holdings/{ids['TSLA']}")
check("Deleting an already-deleted holding returns 404", status == 404)

# -- 9. persistence --------------------------------------------------------
print("\n--- Persistence ---")
with open("data/portfolio.json", encoding="utf-8") as fh:
    saved = json.load(fh)
check("Portfolio persisted to data/portfolio.json",
      len(saved["holdings"]) == 2,
      f"{[h['symbol'] for h in saved['holdings']]}")

# -- summary ---------------------------------------------------------------
passed = sum(1 for r in results if r[0] == PASS)
failed = sum(1 for r in results if r[0] == FAIL)
print("\n" + "=" * 74)
print(f"QA RESULT: {passed} passed, {failed} failed")
if failed:
    print("\nFailures:")
    for status_, name, detail in results:
        if status_ == FAIL:
            print(f"  - {name}: {detail}")
print("=" * 74)
sys.exit(1 if failed else 0)
