# WealthTrack

A local wealth-portfolio tracker. Add your stocks, quantities and purchase
prices; WealthTrack pulls **real** market prices from Yahoo Finance and shows
what your portfolio is worth, what it cost, and what you have made or lost.

No API key. No paid service. No simulated prices.

![stack](https://img.shields.io/badge/python-3.12-blue) ![stack](https://img.shields.io/badge/FastAPI-0.141-009688) ![stack](https://img.shields.io/badge/yfinance-1.7-purple)

---

## Quick start

```powershell
.\run.ps1
```

Then open <http://127.0.0.1:8000>.

The script creates the virtual environment and installs dependencies on first
run. To use a different port: `.\run.ps1 -Port 9000`.

<details>
<summary>Manual setup</summary>

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

`requirements.txt` is production-only (FastAPI + yfinance);
`requirements-dev.txt` adds uvicorn, pytest and httpx.
</details>

---

## What it does

| | |
|---|---|
| **Live prices** | Real quotes from Yahoo Finance via `yfinance`, refreshed automatically every 60 seconds |
| **Last updated** | Every row shows Yahoo's own market timestamp, not just when we asked |
| **Full P&L** | Invested, current value, profit/loss, return %, and today's move — per holding and portfolio-wide |
| **Charts** | Portfolio value over time (1M/3M/6M/1Y) and allocation by holding, drawn as inline SVG |
| **Invalid tickers** | Rejected at entry with a clear message, and never saved |
| **Outages** | Reported honestly — a price we cannot fetch is shown as unavailable, never invented |
| **Responsive** | Table collapses to cards on mobile; light and dark themes follow the OS |

---

## The one rule

**WealthTrack never fabricates a price.**

Every number in the market columns came from Yahoo Finance. When a price cannot
be fetched, the app does not fall back to the purchase price, to zero, or to a
guess. Instead:

- the holding is marked **no price**, and its cost basis is still shown
  (what you paid does not depend on the market);
- it is **excluded from the totals**, and a banner names the affected symbols,
  so the portfolio value is never quietly understated;
- the return percentage is computed only over positions that could actually be
  priced.

There is one deliberate nuance. If Yahoo becomes unreachable and we hold a
price we previously fetched *from Yahoo*, that price is reused for up to 12
hours, tagged **stale**, and keeps its original timestamp. It is a real Yahoo
price, clearly labelled as old — not a fresh invention. An invalid ticker is
never served from cache this way.

---

## Do the numbers add up?

Yes, and the app checks its own work.

Money is computed with `Decimal` and rounded half-up to cents. Per-share prices
are quantised to 4 decimals first — both because fractional-share fills really
do have sub-cent prices, and because Yahoo returns float32 artefacts (`AAPL`
arrives as `319.510009765625` for a stock quoted at `$319.51`). Quantising
before multiplying is what makes the arithmetic on screen hold:

> **displayed price × displayed quantity = displayed value**, exactly.

Portfolio totals are the sum of the already-rounded rows, so adding up the
Invested column by hand gives precisely the Total invested tile — no half-cent
drift.

`calculations.verify_totals()` re-derives every row and every total from
scratch on **every API response**. If anything fails to reconcile the response
carries `totals_verified: false` plus the specific discrepancies, and the
dashboard footer says so. The footer on a healthy portfolio reads:

```
Source: Yahoo Finance (yfinance) · Totals self-check: passed · Last refresh …
```

---

## Deploying (Vercel)

`vercel.json` routes every request to the FastAPI app and bundles `static/`
alongside it. Deploy from the repo root; Vercel installs `requirements.txt`.

### Read the storage caveat first

**On Vercel, your holdings will not persist.** Serverless functions get a
read-only filesystem with only `/tmp` writable, and `/tmp` is wiped when the
instance is recycled and is not shared between concurrent instances. WealthTrack
detects this, keeps working, and shows a warning banner rather than letting you
believe a holding was saved.

That is fine for a demo. For anything real, give it durable storage:

| Option | How |
|---|---|
| **Any host with a disk** (Railway, Fly.io, Render, a VPS, Docker) | Works as-is; set `WEALTHTRACK_DATA_DIR` to a mounted volume |
| **Vercel + a database** | Replace the JSON reads/writes in `storage.py` with Vercel KV / Postgres. The store is a small, self-contained class precisely so this is a localised change |

Set `WEALTHTRACK_DATA_DIR` to point storage anywhere writable. When it is unset,
the app probes: project `data/` if writable, otherwise a temp directory, which it
flags as ephemeral. `GET /api/health` reports exactly what it chose:

```json
"storage": { "data_dir": "...", "ephemeral": true, "resolved_from": "temp directory (read-only filesystem)" }
```

### Bundle size

`yfinance` pulls in pandas and numpy, which is most of Vercel's 250 MB limit.
`requirements.txt` is therefore production-only; dev tools live in
`requirements-dev.txt`. If the bundle is rejected, that split is where to look.

---

## WealthTrack Pro

The dashboard carries a premium tier. Scroll to the pricing section, or click
the plan badge in the header.

| | Free | Pro |
|---|---|---|
| Holdings | Up to 10 | Unlimited |
| Price refresh | 60 seconds | 10 seconds |
| Price history | 1 year | 10 years + full |
| Dividend income & yield on cost | — | ✓ |
| Sector, country & beta exposure | — | ✓ |
| Benchmark vs S&P 500 | — | ✓ |
| CSV export | — | ✓ |

**$7.99/month, $5.99/month billed annually, or $149 once for lifetime.**

The three headline Pro features are **real and working**, not mockups — they
are computed from live Yahoo Finance data:

- **Dividend income** — forward annual payout per holding, portfolio income,
  yield on cost, and genuinely-upcoming ex-dividend dates. A holding that pays
  nothing reports "no dividend", never a confident `$0.00`.
- **Sector & risk** — sector/country/industry weights, value-weighted portfolio
  beta, and a diversification score.
- **Benchmark** — your return against the S&P 500 (or NASDAQ / Dow) rebased to
  100 so the two are directly comparable.

On the free plan these appear as blurred teasers behind a lock. The blurred
figures are generic samples, never the user's own data.

### About the diversification score

It is derived from the *effective* number of sectors (1/HHI) measured against
eight. It is deliberately **not** normalised against the minimum concentration
for the number of sectors you happen to hold — that flatters an even two-sector
split to ~100/100, which would be a misleading number to sell someone. Two
sectors is not diversification however evenly you split it, and the score says
so (~13/100). `effective_sectors` is returned alongside it so the figure is
explainable rather than a black box.

### Payments

**There is no payment processor connected, and nothing in this app collects
card details.** The upgrade dialog has no input fields at all — that is
asserted in the test suite.

- `POST /api/subscription/trial` — starts a 14-day Pro trial locally
- `POST /api/checkout` — returns **501** with an explanation, rather than
  pretending to charge
- `POST /api/subscription/activate` — the hook a real provider's success
  webhook would call

To take real payments, integrate a provider's hosted checkout (so card data
never touches this server) and call `/api/subscription/activate` from its
webhook. Note the current subscription is a single local flag in
`data/subscription.json` — a real product needs accounts and server-side
entitlement, since anyone who can reach this API can unlock Pro themselves.

### Free-plan limits

Limits are constants at the top of `app/premium.py` (`FREE_MAX_HOLDINGS`,
`FREE_MAX_HISTORY_PERIOD`, `FREE_REFRESH_SECONDS`) — change or remove them
there. Downgrading never deletes holdings you already added; you simply cannot
add more until you upgrade. That is covered by a test.

`PLANS` and `FEATURE_MATRIX` in the same module are the single source of truth:
the pricing table, the comparison grid and the server-side gate all read them,
so the marketing copy cannot drift from what is actually enforced.

---

## Testing

```powershell
.\.venv\Scripts\python.exe -m pytest              # everything (76 tests)
.\.venv\Scripts\python.exe -m pytest -m "not live"  # offline only
.\.venv\Scripts\python.exe -m pytest -m live        # only the live Yahoo tests
```

| Suite | Covers |
|---|---|
| `test_calculations.py` | Arithmetic against hand-computed expected values, rounding policy, allocation summing to exactly 100% |
| `test_api.py` | Every endpoint, CRUD, validation, persistence, error codes (Yahoo stubbed) |
| `test_market_errors.py` | Network failures, unknown tickers, bad prices, the stale-cache fallback and its limits |
| `test_market_live.py` | Real calls to Yahoo Finance (marked `live`) |
| `test_premium.py` | Plan gating, holding limit, trial expiry, dividend/sector maths, checkout honesty |
| `test_readonly_deploy.py` | Read-only filesystem: path probing, in-memory fallback, app imports, no 500s |

There is also an end-to-end acceptance check that builds a real portfolio
through the HTTP API and verifies every figure **twice** — against an
independent `yfinance` call and against arithmetic recomputed from scratch:

```powershell
# with the server running
.\.venv\Scripts\python.exe tests\qa_live_check.py
```

Two frontend checks run under Node:

```powershell
node tests\frontend_check.js            # no dependencies: ids, a11y, chart geometry
cd tests; npm install jsdom
node frontend_render_check.js          # renders the real dashboard headlessly
node frontend_premium_check.js         # renders the premium area, free AND Pro
```

Note the render check expects the server up with a 5-holding portfolio, and
`qa_live_check.py` deliberately edits and deletes holdings — so run the render
check *before* the QA script, or re-seed in between.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/health` | Service status |
| `GET` | `/api/holdings` | Stored holdings (no market data) |
| `POST` | `/api/holdings` | Add a holding — validates the ticker against Yahoo first |
| `PUT` | `/api/holdings/{id}` | Edit quantity, price, date or symbol |
| `DELETE` | `/api/holdings/{id}` | Remove one holding |
| `DELETE` | `/api/holdings` | Clear the portfolio |
| `GET` | `/api/portfolio` | Full computed portfolio: rows, totals, allocation, warnings |
| `POST` | `/api/portfolio/refresh` | Force a fresh Yahoo pull, bypassing the 60s cache |
| `GET` | `/api/portfolio/history?period=1mo` | Portfolio value over time from real closes |
| `GET` | `/api/quote/{symbol}` | One live quote |
| `GET` | `/api/history/{symbol}?period=1mo` | One symbol's closes |
| `GET` | `/api/validate/{symbol}` | Check a ticker without saving |

Interactive docs at <http://127.0.0.1:8000/docs>.

Errors are JSON with a machine-readable code, so the UI can tell a typo from an
outage:

```json
{ "error": { "code": "invalid_symbol",
             "message": "'NOTAREAL' is not a symbol Yahoo Finance recognises. …",
             "details": { "symbol": "NOTAREAL" } } }
```

`invalid_symbol` → 400 · `market_unavailable` → 503 · `not_found` → 404 ·
`validation_error` → 422

---

## Layout

```
app/
  main.py           FastAPI routes, error handlers, static mount
  market.py         Yahoo Finance layer — the only module that talks to yfinance
  calculations.py   Pure portfolio maths + the self-verifier (no I/O)
  storage.py        Atomic JSON persistence
  models.py         Request schemas
  errors.py         Typed errors with HTTP status codes
static/
  index.html  styles.css  app.js      Dashboard (no CDN, no build step)
tests/            Four pytest suites + the live acceptance check
data/portfolio.json                   Your portfolio (created on first run)
```

Your portfolio lives in `data/portfolio.json` — plain, readable JSON you can
back up, diff or hand-edit. Writes are atomic, so a crash mid-save cannot
truncate it.

---

## Notes

- **Fractional shares** are supported; quantity and price both accept decimals.
- **Multiple lots** of the same stock are tracked separately and priced once.
- **Non-US tickers** work with Yahoo's suffixes (`BMW.DE`, `TSCO.L`, `RELIANCE.NS`).
- **Mixed currencies** are displayed per row from Yahoo's own currency field.
  Totals are summed numerically without FX conversion, so a mixed-currency
  portfolio's grand total is only meaningful if the holdings share a currency.
- The dashboard pauses auto-refresh while the browser tab is hidden.
