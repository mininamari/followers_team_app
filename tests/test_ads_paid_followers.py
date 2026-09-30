from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core import csv_import
from integrations import ads_paid_followers as paid
from integrations.facebook_ads_sync import SyncResult

PERIOD = ("2026-07-01", "2026-07-31")
TARGET = {"novakiditalia", "novakid_de"}


def _ad(**overrides) -> dict:
    ad = {
        "ad_id": "1", "ad_name": "111", "campaign_name": "", "effective_instagram_media_id": None,
        "instagram_username": None, "followers": 10.0, "spend": 50.0,
    }
    ad.update(overrides)
    return ad


class ResolvePaidRowsTests(unittest.TestCase):
    def resolve(self, ads, meta=None, media=None):
        return paid.resolve_paid_rows(ads, meta or {}, media or {}, TARGET)

    def test_ad_name_matching_meta_publication_wins(self) -> None:
        rows, unresolved = self.resolve(
            [_ad(ad_name="111", effective_instagram_media_id="222")],
            meta={"111": {"novakiditalia"}, "222": {"novakid_de"}},
        )
        self.assertEqual(rows, {("novakiditalia", "111"): {"followers": 10.0, "spend": 50.0}})
        self.assertEqual(unresolved, [])

    def test_promoted_post_is_fallback_when_name_is_not_an_id(self) -> None:
        rows, _ = self.resolve(
            [_ad(ad_name="Summer promo", effective_instagram_media_id="222")],
            meta={"222": {"novakid_de"}},
        )
        self.assertEqual(list(rows), [("novakid_de", "222")])

    def test_unmatched_ad_stays_paid_only_under_its_name(self) -> None:
        rows, _ = self.resolve([
            _ad(ad_id="1", ad_name="22.04.2026", instagram_username="@NovakidItalia"),
            _ad(ad_id="2", ad_name="dark", campaign_name="[r:de] leads"),
            _ad(ad_id="3", ad_name="story", effective_instagram_media_id="555"),
        ], media={"555": "novakid_de"})
        self.assertEqual(set(rows), {
            ("novakiditalia", "22.04.2026"), ("novakid_de", "dark"), ("novakid_de", "story"),
        })

    def test_ads_of_one_post_are_summed(self) -> None:
        rows, _ = self.resolve(
            [_ad(ad_id="1", followers=3.4), _ad(ad_id="2", followers=4.4, spend=10)],
            meta={"111": {"novakiditalia"}},
        )
        row = rows[("novakiditalia", "111")]
        self.assertAlmostEqual(row["followers"], 7.8)
        self.assertEqual(row["spend"], 60.0)

    def test_unknown_account_is_reported_and_other_regions_are_ignored(self) -> None:
        rows, unresolved = self.resolve([
            _ad(ad_id="1", ad_name="mystery"),
            _ad(ad_id="2", ad_name="x", instagram_username="novakidpolska"),
            _ad(ad_id="3", ad_name="idle", followers=0, spend=0),
        ])
        self.assertEqual(rows, {})
        self.assertEqual([ad["ad_id"] for ad in unresolved], ["1"])

    def test_ambiguous_meta_id_falls_back_to_account_lookup(self) -> None:
        rows, _ = self.resolve(
            [_ad(ad_name="111", instagram_username="novakid_de")],
            meta={"111": {"novakiditalia", "novakid_de"}},
        )
        self.assertEqual(list(rows), [("novakid_de", "111")])


class SavePaidFromAdsApiTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.db_path = Path(temp_dir.name) / "test.db"
        self.connections: list[sqlite3.Connection] = []
        self.addCleanup(lambda: [conn.close() for conn in self.connections])
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE meta_publications (account TEXT, period_start TEXT, period_end TEXT, publication_id TEXT);
                CREATE TABLE pr_ads (
                    account TEXT, period_start TEXT, period_end TEXT, month TEXT, publication_id TEXT,
                    pr_followers INTEGER, spend_usd REAL, pr_filename TEXT, uploaded_by TEXT, uploaded_at TEXT
                );
                CREATE TABLE uploads (
                    id INTEGER PRIMARY KEY, file_type TEXT, account TEXT, period_start TEXT, period_end TEXT,
                    filename TEXT, stored_path TEXT, uploaded_by TEXT, uploaded_at TEXT, rows_saved INTEGER, warnings TEXT
                );
                CREATE TABLE fb_ad_accounts (account_id TEXT, is_active INTEGER);
                CREATE TABLE fb_campaigns (campaign_id TEXT, account_id TEXT, name TEXT);
                CREATE TABLE fb_ads (ad_id TEXT, campaign_id TEXT, name TEXT);
                CREATE TABLE fb_creatives (ad_id TEXT, instagram_user_id TEXT, effective_instagram_media_id TEXT);
                CREATE TABLE fb_instagram_accounts (account_id TEXT, instagram_user_id TEXT, username TEXT);
                CREATE TABLE fb_insights (ad_id TEXT, date_start TEXT, date_stop TEXT, spend REAL, instagram_followers REAL);
                CREATE TABLE ig_media (media_id TEXT, account TEXT);
                INSERT INTO fb_ad_accounts VALUES('act_1', 1);
                INSERT INTO fb_campaigns VALUES('c1', 'act_1', 'campaign');
                INSERT INTO fb_ads VALUES('a1', 'c1', '111');
                INSERT INTO fb_insights VALUES('a1', '2026-07-02', '2026-07-02', 20, 4);
                INSERT INTO fb_insights VALUES('a1', '2026-07-03', '2026-07-03', 30, 5);
                INSERT INTO fb_insights VALUES('a1', '2026-08-01', '2026-08-01', 99, 99);
                INSERT INTO meta_publications VALUES('novakiditalia', '2026-07-01', '2026-07-31', '111');
                INSERT INTO pr_ads VALUES('novakiditalia', '2026-07-01', '2026-07-31', '2026-07', 'old', 7, 1, 'pr.csv', 'u', 't');
                """
            )
        for target in (paid, csv_import):
            patcher = patch.object(target, "connect_db", self.connect)
            patcher.start()
            self.addCleanup(patcher.stop)
        configured = patch.object(paid, "is_configured", return_value=True)
        configured.start()
        self.addCleanup(configured.stop)

    def connect(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        self.connections.append(conn)
        return conn

    def pr_rows(self):
        with self.connect() as conn:
            return [tuple(row) for row in conn.execute(
                "SELECT account, publication_id, pr_followers, spend_usd, pr_filename FROM pr_ads ORDER BY publication_id"
            )]

    @patch.object(paid, "sync_ad_account", return_value=SyncResult(account_id="act_1"))
    def test_replaces_period_paid_rows_with_api_totals(self, sync) -> None:
        saved, warnings = paid.save_paid_from_ads_api(*PERIOD, {"novakiditalia"}, "maria")

        self.assertTrue(saved)
        self.assertEqual(self.pr_rows(), [("novakiditalia", "111", 9, 50.0, paid.API_SOURCE_NAME)])
        self.assertFalse(sync.call_args.kwargs["enforce_cooldown"])
        self.assertIn("9", warnings[0])

    @patch.object(paid, "sync_ad_account", return_value=SyncResult(account_id="act_1", status="error", message="boom"))
    def test_failed_sync_keeps_existing_paid_rows(self, _sync) -> None:
        saved, warnings = paid.save_paid_from_ads_api(*PERIOD, {"novakiditalia"}, "maria")

        self.assertFalse(saved)
        self.assertIn("boom", warnings[0])
        self.assertEqual(self.pr_rows(), [("novakiditalia", "old", 7, 1.0, "pr.csv")])


if __name__ == "__main__":
    unittest.main()
