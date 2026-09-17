from __future__ import annotations

import os
import sqlite3
import unittest
from datetime import date
from unittest.mock import Mock, patch

from integrations import facebook_ads_client as client
from integrations.facebook_ads_sync import _acquire_sync_lock, _log_start


class FacebookAdsSafetyTests(unittest.TestCase):
    @patch.dict(os.environ, {"META_ACCESS_TOKEN": "top-secret"})
    @patch.object(client.requests, "get")
    def test_token_is_sent_only_in_authorization_header(self, request_get: Mock) -> None:
        response = Mock(ok=True)
        response.json.return_value = {"data": []}
        request_get.return_value = response

        client._get("https://graph.facebook.com/v21.0/me?after=cursor&access_token=leaked")

        _, kwargs = request_get.call_args
        self.assertNotIn("access_token", request_get.call_args.args[0])
        self.assertNotIn("access_token", kwargs["params"])
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer top-secret"})

    @patch.object(client, "_get")
    def test_only_selected_ads_and_creatives_are_requested(self, get: Mock) -> None:
        get.return_value = {}

        client.get_ads_by_ids(["101", "102"])

        path, params = get.call_args.args
        self.assertEqual(path, "")
        self.assertEqual(params["ids"], "101,102")
        self.assertIn("campaign{id", params["fields"])
        self.assertIn("creative{id", params["fields"])
        self.assertIn("instagram_user_id", params["fields"])

    def test_account_lock_is_atomic_and_audit_records_user(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE fb_sync_locks(account_id TEXT PRIMARY KEY, acquired_at TEXT NOT NULL)")
        conn.execute(
            """CREATE TABLE fb_sync_log(
                id INTEGER PRIMARY KEY, account_id TEXT, started_at TEXT NOT NULL,
                finished_at TEXT, status TEXT NOT NULL, message TEXT, triggered_by TEXT,
                period_start TEXT, period_end TEXT
            )"""
        )

        acquired, _ = _acquire_sync_lock(conn, "act_123")
        acquired_again, reason = _acquire_sync_lock(conn, "act_123")
        log_id = _log_start(conn, "act_456", "maria", date(2026, 9, 1), date(2026, 9, 17))

        self.assertTrue(acquired)
        self.assertFalse(acquired_again)
        self.assertIn("already running", reason)
        row = conn.execute(
            "SELECT triggered_by, period_start, period_end FROM fb_sync_log WHERE id=?", (log_id,)
        ).fetchone()
        self.assertEqual(row, ("maria", "2026-09-01", "2026-09-17"))
        conn.close()


if __name__ == "__main__":
    unittest.main()
