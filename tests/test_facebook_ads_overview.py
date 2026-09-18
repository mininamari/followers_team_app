from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd

from screens.facebook_ads import (
    _ads_overview_df,
    _instagram_profile_options,
    _profile_from_ad_names,
    _sync_validation_error,
)


class FacebookAdsOverviewTests(unittest.TestCase):
    def test_sync_button_reports_validation_problem_instead_of_being_disabled(self) -> None:
        self.assertEqual(
            _sync_validation_error(True, "Select no more than 90 days."),
            "Select no more than 90 days.",
        )
        self.assertIn("META_ACCESS_TOKEN", _sync_validation_error(False, ""))
        self.assertEqual(_sync_validation_error(True, ""), "")

    @patch("screens.facebook_ads.db_df")
    def test_query_groups_every_selected_non_aggregate_column(self, db_df) -> None:
        db_df.return_value = pd.DataFrame()

        _ads_overview_df(["act_123"], date(2026, 9, 1), date(2026, 9, 17))

        query, params = db_df.call_args.args
        group_by = query.split("GROUP BY", 1)[1].split("ORDER BY", 1)[0]
        expected_columns = {
            "c.account_id",
            "c.campaign_id",
            "c.name",
            "c.objective",
            "c.status",
            "a.ad_id",
            "a.name",
            "a.status",
            "cr.creative_id",
            "cr.title",
            "cr.thumbnail_url",
            "cr.tags",
            "cr.instagram_user_id",
            "ia.username",
        }

        self.assertEqual(params, ["act_123", "2026-09-01", "2026-09-17"])
        self.assertIn("i.date_start >= ?", query)
        self.assertNotIn("cr.instagram_user_id = ?", query)
        self.assertIn("SUM(i.instagram_followers)", query)
        self.assertIn("END AS cpf", query)
        for column in expected_columns:
            self.assertIn(column, group_by)

    def test_profile_is_extracted_from_campaign_naming_convention(self) -> None:
        examples = {
            "[r:es][C:SMM][p:instagram][a:ba_multiplyreels] r:es - novakidespana": "novakidespana",
            "[r:arab][c:smm][p:instagram][a:post_retarget] r:arab - novakid_mena": "novakid_mena",
            "[r:fr][C:SMM][p:instagram][a:ba_multiplereels_2] r:fr - novakid_france": "novakid_france",
        }
        for name, expected in examples.items():
            with self.subTest(name=name):
                self.assertEqual(_profile_from_ad_names(name), expected)

    def test_known_region_without_profile_suffix_uses_fallback(self) -> None:
        self.assertEqual(_profile_from_ad_names("[r:pl][c:smm] campaign"), "novakidpolska")

    @patch("screens.facebook_ads.db_df")
    def test_profile_options_keep_region_needed_for_sync(self, db_df) -> None:
        db_df.return_value = pd.DataFrame(
            [
                {
                    "campaign_name": "[r:es][c:smm] r:es - novakidespana",
                    "ad_name": "Spanish ad",
                    "username": None,
                }
            ]
        )

        self.assertEqual(_instagram_profile_options(["act_123"]), {"novakidespana": "es"})

    @patch("screens.facebook_ads.db_df")
    def test_overview_filters_by_resolved_profile_name(self, db_df) -> None:
        db_df.return_value = pd.DataFrame(
            [
                {
                    "campaign_name": "[r:es][c:smm] r:es - novakidespana",
                    "ad_name": "Spanish ad",
                    "instagram_username": None,
                },
                {
                    "campaign_name": "[r:fr][c:smm] r:fr - novakid_france",
                    "ad_name": "French ad",
                    "instagram_username": None,
                },
            ]
        )

        result = _ads_overview_df(
            ["act_123"], date(2026, 9, 1), date(2026, 9, 17), "novakidespana"
        )

        self.assertEqual(result["ad_name"].tolist(), ["Spanish ad"])
        self.assertEqual(result["instagram_username"].tolist(), ["novakidespana"])


if __name__ == "__main__":
    unittest.main()
