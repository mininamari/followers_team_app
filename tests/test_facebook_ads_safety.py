from __future__ import annotations

import os
import sqlite3
import unittest
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

    def test_account_lock_is_atomic_and_audit_records_user(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE fb_sync_locks(account_id TEXT PRIMARY KEY, acquired_at TEXT NOT NULL)")
        conn.execute(
            """CREATE TABLE fb_sync_log(
                id INTEGER PRIMARY KEY, account_id TEXT, started_at TEXT NOT NULL,
                finished_at TEXT, status TEXT NOT NULL, message TEXT, triggered_by TEXT
            )"""
        )

        acquired, _ = _acquire_sync_lock(conn, "act_123")
        acquired_again, reason = _acquire_sync_lock(conn, "act_123")
        log_id = _log_start(conn, "act_456", "maria")

        self.assertTrue(acquired)
        self.assertFalse(acquired_again)
        self.assertIn("already running", reason)
        row = conn.execute("SELECT triggered_by FROM fb_sync_log WHERE id=?", (log_id,)).fetchone()
        self.assertEqual(row[0], "maria")
        conn.close()


if __name__ == "__main__":
    unittest.main()
