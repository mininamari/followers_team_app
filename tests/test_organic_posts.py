from __future__ import annotations

import os
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import pandas as pd

from integrations import facebook_ads_client as client
from integrations import instagram_client
from integrations.instagram_sync import media_row
from screens.organic_posts import SOURCE_API, SOURCE_BOTH, SOURCE_EXPORT, format_summary, merge_posts_with_followers


def _api_post(**overrides) -> dict:
    post = {
        "media_id": "18096476390112541", "account": "novakid_israel", "published_at": "2026-04-20T10:00:00",
        "month": "2026-04", "media_type": "IMAGE", "media_product_type": "FEED",
        "permalink": "https://www.instagram.com/p/DXbaG32jU3N/", "caption": "Hello", "preview_url": None,
        "like_count": 50, "comments_count": 5, "reach": 10000, "views": 15000, "saved": 20, "shares": 10,
        "total_interactions": 85, "follows": 40, "profile_visits": 90, "insights_error": None,
    }
    post.update(overrides)
    return post


def _result(**overrides) -> dict:
    row = {
        "account": "novakid_israel", "month": "2026-04", "publication_id": "18096476390112541",
        "publication_date": "2026-04-20", "publication_link": "https://www.instagram.com/p/DXbaG32jU3N/",
        "post_reach": 12956, "meta_followers": 58, "pr_followers": 18, "final_followers": 40,
        "meta_uploaded_by": "maria", "period_end": "2026-04-30", "updated_at": "2026-05-01",
    }
    row.update(overrides)
    return row


class MergePostsTests(unittest.TestCase):
    def test_api_post_gets_calculated_followers_by_media_id(self) -> None:
        merged = merge_posts_with_followers(pd.DataFrame([_api_post()]), pd.DataFrame([_result()]))

        self.assertEqual(len(merged), 1)
        row = merged.iloc[0]
        self.assertEqual(row["source"], SOURCE_BOTH)
        self.assertEqual((row["followers_total"], row["followers_paid"], row["followers_organic"]), (58, 18, 40))
        self.assertAlmostEqual(row["organic_per_1k_reach"], 4.0)
        self.assertAlmostEqual(row["engagement_rate"], 0.85)

    def test_falls_back_to_permalink_shortcode(self) -> None:
        post = _api_post(media_id="other-id", permalink="https://www.instagram.com/novakid_israel/p/DXbaG32jU3N/?igsh=x")
        merged = merge_posts_with_followers(pd.DataFrame([post]), pd.DataFrame([_result()]))

        self.assertEqual(merged["source"].tolist(), [SOURCE_BOTH])
        self.assertEqual(int(merged.iloc[0]["followers_organic"]), 40)

    def test_keeps_unmatched_rows_from_both_sides_but_not_paid_only(self) -> None:
        results = pd.DataFrame([
            _result(publication_id="export-only", publication_link="https://www.instagram.com/reel/ABC/"),
            _result(publication_id="paid-only", publication_link="", meta_uploaded_by=None),
        ])
        merged = merge_posts_with_followers(pd.DataFrame([_api_post()]), results)

        by_source = dict(zip(merged["publication_id"], merged["source"]))
        self.assertEqual(by_source, {"18096476390112541": SOURCE_API, "export-only": SOURCE_EXPORT})
        export_row = merged[merged["source"] == SOURCE_EXPORT].iloc[0]
        self.assertEqual(export_row["format"], "Reels")
        self.assertEqual(export_row["month"], "2026-04")

    def test_newest_report_wins_for_overlapping_uploads(self) -> None:
        results = pd.DataFrame([
            _result(final_followers=10, period_end="2026-04-15"),
            _result(final_followers=40, period_end="2026-04-30"),
        ])
        merged = merge_posts_with_followers(pd.DataFrame([_api_post()]), results)

        self.assertEqual(len(merged), 1)
        self.assertEqual(int(merged.iloc[0]["followers_organic"]), 40)

    def test_zero_reach_does_not_divide(self) -> None:
        merged = merge_posts_with_followers(pd.DataFrame([_api_post(reach=0)]), pd.DataFrame([_result()]))
        self.assertTrue(pd.isna(merged.iloc[0]["organic_per_1k_reach"]))

    def test_format_summary(self) -> None:
        posts = pd.DataFrame([
            _api_post(),
            _api_post(media_id="r1", media_product_type="REELS", media_type="VIDEO", permalink="", reach=5000),
        ])
        summary = format_summary(merge_posts_with_followers(posts, pd.DataFrame([_result()])))
        self.assertEqual(summary.iloc[0]["format"], "Photo")
        self.assertEqual(int(summary.set_index("format").loc["Reels", "posts"]), 1)


class InstagramClientTests(unittest.TestCase):
    def test_parses_values_and_total_value_shapes(self) -> None:
        payload = {"data": [
            {"name": "reach", "values": [{"value": 120}]},
            {"name": "views", "total_value": {"value": 300}},
            {"name": "follows", "values": [{}]},
        ]}
        self.assertEqual(instagram_client.parse_insights(payload), {"reach": 120, "views": 300})

    def test_reels_do_not_request_feed_only_metrics(self) -> None:
        self.assertNotIn("follows", instagram_client.metrics_for_media({"media_product_type": "REELS"}))
        self.assertIn("follows", instagram_client.metrics_for_media({"media_product_type": "FEED"}))

    @patch.object(instagram_client, "_get")
    def test_media_stops_at_period_start(self, get: Mock) -> None:
        get.side_effect = [
            {"data": [
                {"id": "future", "timestamp": "2026-10-02T00:00:00+0000"},
                {"id": "in", "timestamp": "2026-09-10T00:00:00+0000"},
            ], "paging": {"next": "https://graph.facebook.com/next"}},
            {"data": [
                {"id": "in2", "timestamp": "2026-09-01T00:00:00+0000"},
                {"id": "old", "timestamp": "2026-08-01T00:00:00+0000"},
            ], "paging": {"next": "https://graph.facebook.com/next2"}},
        ]
        media = instagram_client.get_media("1", datetime(2026, 9, 1), datetime(2026, 9, 30, 23, 59))

        self.assertEqual([item["id"] for item in media], ["in", "in2"])
        self.assertEqual(get.call_count, 2)

    @patch.object(instagram_client, "_get")
    @patch.object(instagram_client, "_batch_get")
    def test_invalid_metric_retries_with_fallback_and_keeps_other_errors(self, batch_get: Mock, get: Mock) -> None:
        batch_get.return_value = [
            {"data": [{"name": "reach", "values": [{"value": 5}]}]},
            {"error": {"code": 100, "message": "metric not supported"}},
            {"error": {"code": 10, "message": "no permission"}},
        ]
        get.return_value = {"data": [{"name": "reach", "values": [{"value": 7}]}]}
        media = [{"id": "a"}, {"id": "b"}, {"id": "c"}]

        insights = instagram_client.get_media_insights(media)

        self.assertEqual(insights["a"], {"metrics": {"reach": 5}, "error": None})
        self.assertEqual(insights["b"], {"metrics": {"reach": 7}, "error": None})
        self.assertIn("no permission", insights["c"]["error"])
        self.assertTrue(batch_get.call_args.kwargs["allow_item_errors"])

    @patch.dict(os.environ, {"META_ACCESS_TOKEN": "top-secret"})
    @patch.object(client.requests, "post")
    def test_tolerant_batch_returns_item_errors_in_place(self, request_post: Mock) -> None:
        response = Mock(ok=True)
        response.json.return_value = [
            {"code": 200, "body": '{"data": []}'},
            {"code": 400, "body": '{"error": {"code": 100, "message": "bad metric"}}'},
        ]
        request_post.return_value = response

        result = client._batch_get(["a", "b"], allow_item_errors=True)

        self.assertEqual(result[0], {"data": []})
        self.assertEqual(result[1]["error"]["code"], 100)

    def test_media_row_uses_thumbnail_for_video_and_trims_caption(self) -> None:
        item = {
            "id": "m1", "timestamp": "2026-09-10T08:30:00+0000", "media_type": "VIDEO",
            "media_url": "https://video", "thumbnail_url": "https://thumb", "caption": "x" * 600,
        }
        row = media_row("ig1", "novakid_de", item, {"metrics": {"reach": 3}}, "now")

        self.assertEqual(row[3:5], ("2026-09-10T08:30:00", "2026-09"))
        self.assertEqual(row[9], "https://thumb")
        self.assertEqual(len(row[8]), 500)
        self.assertEqual(row[12], 3)


if __name__ == "__main__":
    unittest.main()
