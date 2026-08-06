from __future__ import annotations

import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import csv_import


PERIOD = ("2026-07-01", "2026-07-31")


class Upload:
    name = "july.csv"

    def __init__(self, text: str):
        self._data = text.encode("utf-8-sig")

    def getvalue(self) -> bytes:
        return self._data


class PaidOnlyFollowerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.connections: list[sqlite3.Connection] = []
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE meta_publications (
                    account TEXT NOT NULL, account_name TEXT, period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL, month TEXT NOT NULL, publication_date TEXT,
                    publication_id TEXT NOT NULL, publication_link TEXT, post_reach INTEGER NOT NULL,
                    meta_followers INTEGER NOT NULL, meta_filename TEXT, uploaded_by TEXT NOT NULL,
                    uploaded_at TEXT NOT NULL,
                    PRIMARY KEY(account, period_start, period_end, publication_id)
                );
                CREATE TABLE pr_ads (
                    account TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
                    month TEXT NOT NULL, publication_id TEXT NOT NULL, pr_followers INTEGER NOT NULL,
                    spend_usd REAL NOT NULL, pr_filename TEXT, uploaded_by TEXT NOT NULL,
                    uploaded_at TEXT NOT NULL,
                    PRIMARY KEY(account, period_start, period_end, publication_id)
                );
                CREATE TABLE follower_overrides (
                    account TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
                    publication_id TEXT NOT NULL, manual_pr_followers INTEGER NOT NULL,
                    updated_by TEXT NOT NULL, updated_at TEXT NOT NULL,
                    PRIMARY KEY(account, period_start, period_end, publication_id)
                );
                CREATE TABLE final_results (
                    account TEXT NOT NULL, account_name TEXT, period_start TEXT NOT NULL,
                    period_end TEXT NOT NULL, month TEXT NOT NULL, publication_date TEXT,
                    publication_id TEXT NOT NULL, publication_link TEXT, post_reach INTEGER NOT NULL,
                    meta_followers INTEGER NOT NULL, imported_pr_followers INTEGER NOT NULL,
                    manual_pr_followers INTEGER, pr_followers INTEGER NOT NULL,
                    final_followers INTEGER NOT NULL, spend_usd REAL NOT NULL, cpf_usd REAL,
                    warning TEXT, meta_uploaded_by TEXT, pr_uploaded_by TEXT,
                    override_updated_by TEXT, override_updated_at TEXT, updated_at TEXT NOT NULL,
                    PRIMARY KEY(account, period_start, period_end, publication_id)
                );
                CREATE TABLE monthly_follower_totals (
                    account TEXT NOT NULL, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
                    month TEXT NOT NULL, imported_total_followers INTEGER NOT NULL,
                    imported_paid_followers INTEGER NOT NULL, paid_only_followers INTEGER NOT NULL,
                    manual_total_followers INTEGER, manual_paid_followers INTEGER,
                    total_followers INTEGER NOT NULL, paid_followers INTEGER NOT NULL,
                    organic_followers INTEGER NOT NULL, updated_by TEXT, updated_at TEXT NOT NULL,
                    PRIMARY KEY(account, period_start, period_end)
                );
                CREATE TABLE uploads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, file_type TEXT NOT NULL, account TEXT,
                    period_start TEXT NOT NULL, period_end TEXT NOT NULL, filename TEXT NOT NULL,
                    stored_path TEXT, uploaded_by TEXT NOT NULL, uploaded_at TEXT NOT NULL,
                    rows_saved INTEGER NOT NULL, warnings TEXT
                );
                """
            )
        self.connect_patch = patch.object(csv_import, "connect_db", self.connect)
        self.connect_patch.start()
        self.addCleanup(self.temp_dir.cleanup)
        self.addCleanup(self.close_connections)
        self.addCleanup(self.connect_patch.stop)

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        self.connections.append(conn)
        return conn

    def close_connections(self) -> None:
        for conn in self.connections:
            conn.close()

    def seed_matched(self, meta_total: int = 100, paid: int = 60, manual_paid: int | None = 70) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO meta_publications VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    "novakiditalia", "Novakid Italia", *PERIOD, "2026-07", "2026-07-10",
                    "matched", "https://example.com/matched", 1000, meta_total,
                    "meta.csv", "meta_user", "2026-08-01T00:00:00Z",
                ),
            )
            conn.execute(
                "INSERT INTO pr_ads VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "novakiditalia", *PERIOD, "2026-07", "matched", paid, 120.0,
                    "pr.csv", "pr_user", "2026-08-01T00:00:00Z",
                ),
            )
            if manual_paid is not None:
                conn.execute(
                    "INSERT INTO follower_overrides VALUES(?,?,?,?,?,?,?)",
                    (
                        "novakiditalia", *PERIOD, "matched", manual_paid,
                        "editor", "2026-08-02T00:00:00Z",
                    ),
                )

    def monthly(self):
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM monthly_follower_totals WHERE account=? AND period_start=? AND period_end=?",
                ("novakiditalia", *PERIOD),
            ).fetchone()

    def test_paid_only_row_increases_paid_and_total_but_keeps_organic(self) -> None:
        self.seed_matched()
        csv_import.recalc_final("novakiditalia", *PERIOD)
        before = self.monthly()
        self.assertEqual((before["total_followers"], before["paid_followers"], before["organic_followers"]), (100, 70, 30))

        with self.connect() as conn:
            conn.execute(
                "INSERT INTO pr_ads VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "novakiditalia", *PERIOD, "2026-07", "paid-only", 40, 80.0,
                    "july.csv", "pr_user", "2026-08-03T00:00:00Z",
                ),
            )
        csv_import.recalc_final("novakiditalia", *PERIOD)

        after = self.monthly()
        self.assertEqual(after["paid_only_followers"], 40)
        self.assertEqual((after["total_followers"], after["paid_followers"], after["organic_followers"]), (140, 110, 30))
        self.assertEqual(after["total_followers"], after["paid_followers"] + after["organic_followers"])
        with self.connect() as conn:
            paid_only = conn.execute(
                "SELECT * FROM final_results WHERE publication_id='paid-only'"
            ).fetchone()
            override = conn.execute(
                "SELECT manual_pr_followers FROM follower_overrides WHERE publication_id='matched'"
            ).fetchone()
        self.assertEqual((paid_only["meta_followers"], paid_only["pr_followers"], paid_only["final_followers"]), (40, 40, 0))
        self.assertEqual(paid_only["warning"], "")
        self.assertEqual(override[0], 70)

    def test_total_expands_when_matched_paid_exceeds_meta(self) -> None:
        self.seed_matched(meta_total=50, paid=80, manual_paid=None)
        csv_import.recalc_final("novakiditalia", *PERIOD)
        monthly = self.monthly()
        self.assertEqual((monthly["total_followers"], monthly["paid_followers"], monthly["organic_followers"]), (80, 80, 0))
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM final_results WHERE publication_id='matched'").fetchone()
        self.assertEqual(row["meta_followers"], row["pr_followers"] + row["final_followers"])

    def test_add_only_keeps_existing_manual_row_and_adds_unmatched_as_paid_only(self) -> None:
        self.seed_matched()
        csv_import.recalc_final("novakiditalia", *PERIOD)
        source = "\n".join(
            [
                "Дата начала отчетности,Окончание отчетности,Название объявления,Название Страницы,Подписки IG,Потраченная сумма (USD)",
                "2026-07-01,2026-07-31,matched,Novakid Italia,80,150",
                "2026-07-01,2026-07-31,paid-only,Novakid Italia,40,80",
            ]
        )
        with patch.object(csv_import, "save_uploaded_file", return_value="/tmp/july.csv"):
            saved, warnings = csv_import.import_pr(
                Upload(source),
                {"username": "admin", "role": "admin"},
                account="",
                auto_detect_accounts=True,
                page_account_map={"Novakid Italia": "novakiditalia"},
                add_only=True,
            )

        self.assertEqual(saved, 1)
        self.assertTrue(any("Paid-only" in warning for warning in warnings))
        with self.connect() as conn:
            matched = conn.execute("SELECT pr_followers FROM pr_ads WHERE publication_id='matched'").fetchone()
            override = conn.execute("SELECT manual_pr_followers FROM follower_overrides WHERE publication_id='matched'").fetchone()
        self.assertEqual(matched[0], 60)
        self.assertEqual(override[0], 70)
        monthly = self.monthly()
        self.assertEqual((monthly["total_followers"], monthly["paid_followers"], monthly["organic_followers"]), (140, 110, 30))


if __name__ == "__main__":
    unittest.main()
