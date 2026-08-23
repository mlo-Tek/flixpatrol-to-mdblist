import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import mlo_list_layout as layout


class ListLayoutTests(unittest.TestCase):
    def test_names_descriptions_and_alphabetical_order(self):
        entries = [
            {
                "platform": "netflix",
                "location": "germany",
                "type": "shows",
                "name": "Netflix Top 10 Shows Germany",
            },
            {
                "platform": "amazon-prime",
                "location": "germany",
                "type": "movies",
                "name": "Amazon Prime Top 10 Movies Germany",
            },
            {
                "platform": "apple-tv",
                "location": "germany",
                "type": "movies",
            },
        ]

        result = layout._normalize_entries(entries)

        self.assertEqual(
            [entry["platform"] for entry in result],
            ["amazon-prime", "apple-tv", "netflix"],
        )
        self.assertEqual(result[0]["name"], "Top 10 Amazon Prime Movies Today")
        self.assertEqual(
            result[0]["description"],
            "Top 10 Amazon Prime movies in Germany",
        )
        self.assertIn(
            "Amazon Prime Top 10 Movies Germany",
            result[0]["legacyNames"],
        )
        self.assertEqual(result[1]["name"], "Top 10 Apple TV+ Movies Today")
        self.assertEqual(result[2]["name"], "Top 10 Netflix TV Shows Today")
        self.assertEqual(
            result[2]["description"],
            "Top 10 Netflix TV shows in Germany",
        )

    def test_us_charts_keep_correct_country_in_description(self):
        name, description = layout._desired_metadata(
            {
                "platform": "hulu",
                "location": "united-states",
                "type": "movies",
            }
        )
        self.assertEqual(name, "Top 10 Hulu Movies Today")
        self.assertEqual(description, "Top 10 Hulu movies in United States")

    def test_crunchyroll_overall_has_neutral_title(self):
        name, description = layout._desired_metadata(
            {
                "platform": "crunchyroll",
                "location": "germany",
                "type": "overall",
            }
        )
        self.assertEqual(name, "Top 10 Crunchyroll Today")
        self.assertEqual(description, "Top 10 Crunchyroll titles in Germany")

    def test_static_list_id_fields_are_preferred_over_generic_id(self):
        self.assertEqual(
            layout._candidate_ids(
                {"id": 139604, "static_list_id": 203885, "name": "Example"}
            ),
            [203885, 139604],
        )

    def test_generic_id_remains_supported_for_real_static_lists(self):
        self.assertEqual(layout._candidate_ids({"id": 203922}), [203922])

    def test_resolver_uses_static_items_endpoint_not_metadata_endpoint(self):
        sync = Mock()
        mdb = Mock()
        mdb.get_list_items.side_effect = [None, {"movies": [], "shows": []}]

        list_id = layout._resolve_static_list_id(
            sync,
            mdb,
            {"id": 139604, "static_list_id": 203885},
        )

        self.assertEqual(list_id, 139604)
        self.assertEqual(
            [call.args[0] for call in mdb.get_list_items.call_args_list],
            [203885, 139604],
        )

    def test_matching_non_static_list_is_ignored_and_static_list_is_created(self):
        sync = Mock()
        sync.DRY_RUN = False
        sync.logger = Mock()
        mdb = Mock()
        mdb.get_my_lists.side_effect = [
            [{"id": 139602, "name": "Top 10 Disney+ Movies Today"}],
            [{"static_list_id": 220001, "name": "Top 10 Disney+ Movies Today"}],
        ]
        mdb.get_list_items.side_effect = [
            None,
            {"movies": [], "shows": []},
        ]
        mdb._req.return_value = {}

        layout.LIST_METADATA.clear()
        layout.LIST_METADATA["top 10 disney+ movies today"] = {
            "description": "Top 10 Disney+ movies in Germany",
            "legacy_names": [],
        }
        layout._patch_static_list_metadata(sync)

        with patch.object(layout.time, "sleep"):
            list_id = sync.find_or_create_list(
                mdb,
                "Top 10 Disney+ Movies Today",
                "top-10-disney-movies-today",
            )

        self.assertEqual(list_id, 220001)
        mdb._req.assert_called_once_with(
            "POST",
            "/lists/user/add",
            json={
                "name": "Top 10 Disney+ Movies Today",
                "description": "Top 10 Disney+ movies in Germany",
            },
        )

    def test_verified_sync_aborts_before_add_for_invalid_static_id(self):
        sync = Mock()
        sync.DRY_RUN = False
        sync.logger = Mock()
        layout._patch_static_list_sync(sync)

        mdb = Mock()
        mdb.get_list_items.return_value = None

        result = sync.sync_items(
            mdb,
            139602,
            [{"type": "movie", "imdb_id": "tt1234567"}],
            "Top 10 Disney+ Movies Today",
        )

        self.assertFalse(result)
        mdb.add_items.assert_not_called()
        mdb.remove_items.assert_not_called()

    def test_verified_sync_checks_final_contents_before_logging_success(self):
        sync = Mock()
        sync.DRY_RUN = False
        sync.logger = Mock()
        layout._patch_static_list_sync(sync)

        mdb = Mock()
        mdb.get_list_items.side_effect = [
            {"movies": [], "shows": []},
            {
                "movies": [{"imdb_id": "tt1234567"}],
                "shows": [],
            },
        ]

        result = sync.sync_items(
            mdb,
            220001,
            [{"type": "movie", "imdb_id": "tt1234567"}],
            "Top 10 Disney+ Movies Today",
        )

        self.assertTrue(result)
        mdb.add_items.assert_called_once_with(
            220001,
            [{"imdb": "tt1234567"}],
            None,
        )


if __name__ == "__main__":
    unittest.main()
