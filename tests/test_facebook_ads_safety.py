from __future__ import annotations

import os
import sqlite3
import unittest
from datetime import date
from unittest.mock import Mock, patch

import requests

from integrations import facebook_ads_client as client
from integrations.facebook_ads_sync import _acquire_sync_lock, _instagram_followers_from_insight, _log_start


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

    @patch.dict(os.environ, {"META_ACCESS_TOKEN": "top-secret"})
    @patch.object(client.time, "sleep")
    @patch.object(client.requests, "get")
    def test_connection_reset_waits_and_retries(self, request_get: Mock, sleep: Mock) -> None:
        success = Mock(ok=True)
        success.json.return_value = {"data": []}
        request_get.side_effect = [requests.ConnectionError("connection reset"), success]

        result = client._get("act_123/insights")

        self.assertEqual(result, {"data": []})
        sleep.assert_called_once_with(2.0)
        self.assertEqual(request_get.call_count, 2)

    @patch.object(client, "_batch_get")
    def test_only_selected_ads_and_creatives_are_requested(self, batch_get: Mock) -> None:
        batch_get.return_value = []

        client.get_ads_by_ids(["101", "102"])

        paths = batch_get.call_args.args[0]
        self.assertEqual(len(paths), 2)
        self.assertTrue(paths[0].startswith("101?fields="))
        self.assertNotIn("ids=", paths[0])
        self.assertIn("campaign%7B", paths[0])
        self.assertIn("instagram_user_id", paths[0])

    @patch.dict(os.environ, {"META_ACCESS_TOKEN": "top-secret"})
    @patch.object(client.requests, "post")
    def test_batch_requests_use_authorization_header(self, request_post: Mock) -> None:
        response = Mock(ok=True)
        response.json.return_value = [{"code": 200, "body": '{"id":"101"}'}]
        request_post.return_value = response

        result = client._batch_get(["101?fields=id"])

        _, kwargs = request_post.call_args
        self.assertEqual(kwargs["headers"], {"Authorization": "Bearer top-secret"})
        self.assertNotIn("access_token", kwargs["data"])
        self.assertEqual(result, [{"id": "101"}])

    @patch.dict(os.environ, {"META_ACCESS_TOKEN": "top-secret"})
    @patch.object(client.time, "sleep")
    @patch.object(client.requests, "post")
    def test_batch_rate_limit_waits_before_retrying(self, request_post: Mock, sleep: Mock) -> None:
        limited = Mock(ok=True)
        limited.json.return_value = [
            {"code": 400, "body": '{"error":{"code":17,"message":"rate limited"}}'}
        ]
        success = Mock(ok=True)
        success.json.return_value = [{"code": 200, "body": '{"id":"101"}'}]
        request_post.side_effect = [limited, success]

        result = client._batch_get(["101?fields=id"])

        self.assertEqual(result, [{"id": "101"}])
        sleep.assert_called_once_with(2.0)
        self.assertEqual(request_post.call_count, 2)

    @patch.object(client.time, "sleep")
    @patch.object(client, "_get_all_pages")
    def test_insights_are_chunked_and_overloaded_windows_are_split(
        self, get_all_pages: Mock, sleep: Mock
    ) -> None:
        def fake_get_all_pages(_path, params):
            time_range = params["time_range"]
            if '"since":"2026-09-01"' in time_range and '"until":"2026-09-07"' in time_range:
                raise client.FacebookApiError("reduce data", code=1)
            return [{"ad_id": time_range}]

        get_all_pages.side_effect = fake_get_all_pages

        rows = client.get_insights("act_123", "2026-09-01", "2026-09-10")

        self.assertEqual(len(rows), 3)
        requested_ranges = [call.args[1]["time_range"] for call in get_all_pages.call_args_list]
        self.assertIn('{"since":"2026-09-01","until":"2026-09-07"}', requested_ranges)
        self.assertIn('{"since":"2026-09-01","until":"2026-09-04"}', requested_ranges)
        self.assertIn('{"since":"2026-09-08","until":"2026-09-10"}', requested_ranges)
        sleep.assert_called_once_with(client.DATA_REDUCTION_DELAY_SECONDS)

    @patch.object(client, "_get_all_pages")
    def test_insights_filter_selected_region_at_api_level(self, get_all_pages: Mock) -> None:
        get_all_pages.return_value = []

        client.get_insights("act_123", "2026-09-01", "2026-09-01", region_code="es")

        params = get_all_pages.call_args.args[1]
        self.assertIn("actions", params["fields"])
        self.assertIn("cost_per_action_type", params["fields"])
        self.assertEqual(
            params["filtering"],
            '[{"field":"campaign.name","operator":"CONTAIN","value":"[r:es]"}]',
        )

    def test_instagram_followers_are_read_only_from_api_follow_actions(self) -> None:
        row = {
            "actions": [
                {"action_type": "onsite_conversion.instagram_profile_follow", "value": "7"},
                {"action_type": "link_click", "value": "90"},
                {"action_type": "instagram_follows", "value": "2"},
            ]
        }

        self.assertEqual(_instagram_followers_from_insight(row), 9.0)
        self.assertEqual(_instagram_followers_from_insight({}), 0.0)

    @patch.object(client.time, "sleep")
    @patch.object(client, "_batch_get")
    def test_overloaded_ad_detail_batch_is_split(self, batch_get: Mock, sleep: Mock) -> None:
        def fake_batch_get(paths):
            if len(paths) > 1:
                raise client.FacebookApiError("reduce data", code=1)
            ad_id = paths[0].split("?", 1)[0]
            return [{"id": ad_id}]

        batch_get.side_effect = fake_batch_get

        rows = client.get_ads_by_ids(["101", "102"])

        self.assertEqual([row["id"] for row in rows], ["101", "102"])
        sleep.assert_called_once_with(client.DATA_REDUCTION_DELAY_SECONDS)

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
