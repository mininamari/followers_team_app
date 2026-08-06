"""One-time, fail-safe migration from a SQLite backup to Railway PostgreSQL.

Usage:
  SOURCE_SQLITE_PATH=/path/to/backup.db DATABASE_URL=postgresql://... \
    python3 scripts/migrate_sqlite_to_postgres.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.database import IS_POSTGRES, connect_db  # noqa: E402
from core.db import init_db  # noqa: E402


TABLES = [
    "users", "uploads", "meta_publications", "pr_ads", "follower_overrides",
    "final_results", "monthly_follower_totals", "system_settings",
    "fb_ad_accounts", "fb_campaigns", "fb_ads", "fb_creatives",
    "fb_insights", "fb_sync_log",
]


def main() -> None:
    source_path = Path(os.environ.get("SOURCE_SQLITE_PATH", "")).expanduser()
    if not IS_POSTGRES:
        raise SystemExit("DATABASE_URL must point to PostgreSQL.")
    if not source_path.is_file():
        raise SystemExit(f"SQLite backup not found: {source_path}")

    # Never create a bootstrap admin before copying the real users table.
    os.environ.pop("FOLLOWERS_ADMIN_USERNAME", None)
    os.environ.pop("FOLLOWERS_ADMIN_PASSWORD", None)
    init_db()
    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    source_tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    with connect_db() as target:
        non_empty = []
        for table in TABLES:
            if table in source_tables and target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]:
                non_empty.append(table)
        if non_empty:
            raise RuntimeError("PostgreSQL is not empty; migration stopped: " + ", ".join(non_empty))

        results = []
        for table in TABLES:
            if table not in source_tables:
                continue
            cursor = source.execute(f"SELECT * FROM {table}")
            columns = [item[0] for item in cursor.description]
            rows = [tuple(row[column] for column in columns) for row in cursor.fetchall()]
            if rows:
                placeholders = ",".join("?" for _ in columns)
                target.executemany(
                    f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders}) ON CONFLICT DO NOTHING",
                    rows,
                )
            target_count = int(target.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            if target_count != len(rows):
                raise RuntimeError(f"Validation failed for {table}: SQLite={len(rows)}, PostgreSQL={target_count}")
            results.append((table, target_count))

        for table in ("users", "uploads", "fb_sync_log"):
            if table in source_tables:
                target.execute(
                    "SELECT setval(pg_get_serial_sequence(?, 'id'), COALESCE((SELECT MAX(id) FROM " + table + "), 1), true)",
                    (table,),
                )

    source.close()
    print("Migration verified successfully:")
    for table, count in results:
        print(f"  {table}: {count}")


if __name__ == "__main__":
    main()
