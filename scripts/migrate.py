"""Apply database migrations, and optionally import an existing JSON portfolio.

    python scripts/migrate.py              # create/verify schema
    python scripts/migrate.py --status     # show what is applied
    python scripts/migrate.py --import-json data/portfolio.json

Reads the database URL from the environment (WEALTHTRACK_DATABASE_URL,
POSTGRES_URL, DATABASE_URL, ...), loading a local .env if present.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

MIGRATIONS_DIR = ROOT / "migrations"


def load_dotenv(path: Path = ROOT / ".env") -> int:
    """Minimal .env loader so the script works without extra dependencies."""
    if not path.is_file():
        return 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        # Do not clobber variables already set in the real environment.
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def main() -> int:
    parser = argparse.ArgumentParser(description="WealthTrack database migrations")
    parser.add_argument("--status", action="store_true", help="show applied migrations")
    parser.add_argument("--import-json", metavar="PATH",
                        help="import holdings from an existing portfolio.json")
    args = parser.parse_args()

    loaded = load_dotenv()
    if loaded:
        print(f"Loaded {loaded} variable(s) from .env")

    from app import db

    url = db.get_database_url()
    if not url:
        print("No database URL found.\n")
        print("Set one of these (a local .env works):")
        for var in db._URL_ENV_VARS:
            print(f"  {var}")
        print("\nExample for local Postgres:")
        print("  WEALTHTRACK_DATABASE_URL=postgresql://postgres:pw@127.0.0.1:5432/wealthtrack")
        return 2

    print(f"Database: {db.redact(url)}  (from {db.configured_from()})")
    database = db.Database(url)

    if args.status:
        with database.connection() as conn, conn.cursor() as cur:
            cur.execute("SELECT to_regclass('schema_migrations')")
            if cur.fetchone()[0] is None:
                print("schema_migrations table does not exist -- nothing applied yet.")
                return 0
            cur.execute("SELECT version, applied_at FROM schema_migrations ORDER BY version")
            rows = cur.fetchall()
        if not rows:
            print("No migrations applied.")
        for version, applied_at in rows:
            print(f"  {version}  applied {applied_at}")
        return 0

    # Apply every .sql file in order. All are written to be idempotent.
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        print(f"No .sql files in {MIGRATIONS_DIR}")
        return 1

    for path in files:
        version = path.stem
        sql = path.read_text(encoding="utf-8")
        with database.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (version) VALUES (%s) "
                    "ON CONFLICT (version) DO NOTHING", (version,))
            conn.commit()
        print(f"  applied {version}")

    with database.connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM holdings")
        print(f"Schema ready. holdings rows: {cur.fetchone()[0]}")

    if args.import_json:
        source = Path(args.import_json)
        if not source.is_file():
            print(f"No such file: {source}")
            return 1
        data = json.loads(source.read_text(encoding="utf-8"))
        holdings = data.get("holdings", [])
        if not holdings:
            print("Nothing to import.")
            return 0

        from app.pg_storage import PostgresPortfolioStore

        store = PostgresPortfolioStore(database)
        existing = {(h["symbol"], h["quantity"], h["purchase_price"])
                    for h in store.list_holdings()}
        imported = skipped = 0
        for h in holdings:
            key = (h["symbol"], float(h["quantity"]), float(h["purchase_price"]))
            if key in existing:
                skipped += 1
                continue
            store.add_holding(
                symbol=h["symbol"], quantity=h["quantity"],
                purchase_price=h["purchase_price"],
                purchase_date=h.get("purchase_date"), notes=h.get("notes"),
                name=h.get("name"))
            imported += 1
        print(f"Imported {imported} holding(s); skipped {skipped} already present.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
