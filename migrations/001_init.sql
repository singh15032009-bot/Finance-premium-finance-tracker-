-- WealthTrack initial schema.
--
-- Fields mirror the JSON store's holding record exactly, so the two backends
-- are interchangeable:
--   id, symbol, name, quantity, purchase_price, purchase_date, notes,
--   created_at, updated_at
--
-- Money is NUMERIC, never float: the app computes with Decimal and rounds
-- half-up, and a float column would reintroduce the drift that care avoids.
--   quantity       NUMERIC(24,8) -- fractional shares
--   purchase_price NUMERIC(20,4) -- matches the app's 4dp price precision
--
-- Idempotent: safe to run repeatedly. The app also applies this at startup.

CREATE TABLE IF NOT EXISTS holdings (
    id              TEXT          PRIMARY KEY,
    symbol          TEXT          NOT NULL,
    name            TEXT,
    quantity        NUMERIC(24,8) NOT NULL CHECK (quantity > 0),
    purchase_price  NUMERIC(20,4) NOT NULL CHECK (purchase_price > 0),
    purchase_date   DATE,
    notes           TEXT,
    created_at      TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ   NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS holdings_symbol_idx  ON holdings (symbol);
CREATE INDEX IF NOT EXISTS holdings_created_idx ON holdings (created_at);

-- Single-row table holding the local plan state.
CREATE TABLE IF NOT EXISTS subscription (
    id             SMALLINT    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    plan           TEXT        NOT NULL DEFAULT 'free',
    since          TIMESTAMPTZ,
    trial_ends_at  TIMESTAMPTZ,
    source         TEXT,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO subscription (id, plan, since)
VALUES (1, 'free', now())
ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT        PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
