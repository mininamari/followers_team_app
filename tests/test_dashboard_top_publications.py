from __future__ import annotations

import unittest

import pandas as pd

from screens.dashboard import _extract_og_image_url, _is_allowed_host, _top_organic_publications


class DashboardTopPublicationsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = pd.DataFrame(
            [
                {"account": "italy", "month": "2026-07", "publication_id": "a", "publication_link": "https://instagram.com/p/a", "meta_uploaded_by": "maria", "final_followers": 10, "post_reach": 100, "publication_date": "2026-07-10", "period_end": "2026-07-31", "updated_at": "2026-08-01"},
                {"account": "italy", "month": "2026-08", "publication_id": "b", "publication_link": "https://instagram.com/p/b", "meta_uploaded_by": "maria", "final_followers": 40, "post_reach": 200, "publication_date": "2026-08-10", "period_end": "2026-08-31", "updated_at": "2026-09-01"},
                {"account": "spain", "month": "2026-08", "publication_id": "c", "publication_link": "https://instagram.com/p/c", "meta_uploaded_by": "maria", "final_followers": 30, "post_reach": 300, "publication_date": "2026-08-11", "period_end": "2026-08-31", "updated_at": "2026-09-01"},
                {"account": "spain", "month": "2026-08", "publication_id": "paid-only", "publication_link": "", "meta_uploaded_by": None, "final_followers": 100, "post_reach": 0, "publication_date": None, "period_end": "2026-08-31", "updated_at": "2026-09-01"},
            ]
        )

    def test_ranking_follows_region_and_month_filters(self) -> None:
        top = _top_organic_publications(self.rows, ["italy", "spain"], ["2026-08"])
        self.assertEqual(top["publication_id"].tolist(), ["b", "c"])

        italy = _top_organic_publications(self.rows, ["italy"], ["2026-07"])
        self.assertEqual(italy["publication_id"].tolist(), ["a"])

    def test_overlapping_publication_is_shown_once(self) -> None:
        duplicate = self.rows.iloc[[1]].copy()
        duplicate["month"] = "2026-07"
        duplicate["period_end"] = "2026-07-31"
        duplicate["final_followers"] = 20
        rows = pd.concat([self.rows, duplicate], ignore_index=True)

        top = _top_organic_publications(rows, ["italy"], ["2026-07", "2026-08"])
        self.assertEqual(top["publication_id"].tolist(), ["b", "a"])
        self.assertEqual(int(top.iloc[0]["final_followers"]), 40)

    def test_preview_url_validation_blocks_non_instagram_hosts(self) -> None:
        self.assertTrue(_is_allowed_host("https://www.instagram.com/p/abc/", ("instagram.com",)))
        self.assertFalse(_is_allowed_host("http://www.instagram.com/p/abc/", ("instagram.com",)))
        self.assertFalse(_is_allowed_host("https://instagram.com.attacker.test/p/abc/", ("instagram.com",)))
        self.assertFalse(_is_allowed_host("https://example.com/private", ("instagram.com",)))

    def test_extracts_public_open_graph_preview(self) -> None:
        html = '<html><head><meta property="og:image" content="https://scontent.cdninstagram.com/image.jpg"></head></html>'
        self.assertEqual(_extract_og_image_url(html), "https://scontent.cdninstagram.com/image.jpg")


if __name__ == "__main__":
    unittest.main()
