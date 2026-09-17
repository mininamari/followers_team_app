from __future__ import annotations

import unittest
from unittest.mock import patch

import pandas as pd

from screens.facebook_ads import _ads_overview_df


class FacebookAdsOverviewTests(unittest.TestCase):
    @patch("screens.facebook_ads.db_df")
    def test_query_groups_every_selected_non_aggregate_column(self, db_df) -> None:
        db_df.return_value = pd.DataFrame()

        _ads_overview_df(["act_123"])

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
        }

        self.assertEqual(params, ["act_123"])
        for column in expected_columns:
            self.assertIn(column, group_by)


if __name__ == "__main__":
    unittest.main()
