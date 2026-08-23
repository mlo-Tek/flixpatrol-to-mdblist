import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import mlo_list_layout as layout
import mlo_static_fix as static_fix


class StaticIdSeparationTests(unittest.TestCase):
    def test_generic_metadata_id_is_not_treated_as_static_id(self):
        record = {
            "id": 139604,
            "name": "Top 10 Amazon Prime Movies Today",
        }
        self.assertIsNone(static_fix.explicit_static_id(record))
        self.assertEqual(static_fix.metadata_id(record), 139604)

    def test_explicit_static_id_is_used_for_item_writes(self):
        record = {
            "id": 139604,
            "static_list_id": 203885,
            "name": "Top 10 Amazon Prime Movies Today",
        }
        self.assertEqual(static_fix.metadata_id(record), 139604)
        self.assertEqual(static_fix.explicit_static_id(record), 203885)

    def test_matching_non_static_record_is_ignored_and_new_static_created(self):
        sync = Mock()
        sync.DRY_RUN = False
        sync.logger = Mock()
        sync.sync_items = Mock()
        mdb = Mock()

        mdb.get_my_lists.side_effect = [
            [{
                "id": 139604,
                "name": "Top 10 Amazon Prime Movies Today",
            }],
            [{
                "id": 139999,
                "static_list_id": 220123,
                "name": "Top 10 Amazon Prime Movies Today",
            }],
        ]
        mdb._req.return_value = {"id": 139999}

        layout.LIST_METADATA.clear()
        layout.LIST_METADATA["top 10 amazon prime movies today"] = {
            "description": "Top 10 Amazon Prime movies in Germany",
            "legacy_names": [],
        }

        static_fix.install(sync)
        with patch.object(static_fix.time, "sleep"):
            list_id = sync.find_or_create_list(
                mdb,
                "Top 10 Amazon Prime Movies Today",
                "top-10-amazon-prime-movies-today",
            )

        self.assertEqual(list_id, 220123)
        mdb._req.assert_called_once_with(
            "POST",
            "/lists/user/add",
            json={
                "name": "Top 10 Amazon Prime Movies Today",
                "description": "Top 10 Amazon Prime movies in Germany",
            },
        )

    def test_metadata_update_uses_generic_id_but_returns_static_id(self):
        sync = Mock()
        sync.DRY_RUN = False
        sync.logger = Mock()
        sync.sync_items = Mock()
        mdb = Mock()
        mdb.get_my_lists.return_value = [{
            "id": 139604,
            "static_list_id": 203885,
            "name": "Amazon Prime Top 10 Movies Germany",
            "description": "old",
        }]
        mdb._req.return_value = {}

        layout.LIST_METADATA.clear()
        layout.LIST_METADATA["top 10 amazon prime movies today"] = {
            "description": "Top 10 Amazon Prime movies in Germany",
            "legacy_names": ["Amazon Prime Top 10 Movies Germany"],
        }

        static_fix.install(sync)
        list_id = sync.find_or_create_list(
            mdb,
            "Top 10 Amazon Prime Movies Today",
            "top-10-amazon-prime-movies-today",
        )

        self.assertEqual(list_id, 203885)
        mdb._req.assert_called_once_with(
            "PUT",
            "/lists/139604",
            json={
                "name": "Top 10 Amazon Prime Movies Today",
                "description": "Top 10 Amazon Prime movies in Germany",
            },
        )


class MissingItemDiagnosticsTests(unittest.TestCase):
    def test_failed_add_logs_exact_missing_title_and_ids(self):
        sync = Mock()
        sync.DRY_RUN = False
        sync.logger = Mock()

        def original_sync_items(mdb, list_id, items, name):
            mdb.get_list_items(list_id)
            mdb.add_items(
                list_id,
                [{"imdb": "tt1111111"}, {"imdb": "tt2222222", "tmdb": 222}],
                None,
            )
            mdb.get_list_items(list_id)
            return False

        sync.sync_items = original_sync_items
        static_fix._patch_missing_item_diagnostics(sync)

        mdb = Mock()
        mdb.get_list_items.side_effect = [
            {"movies": [], "shows": []},
            {
                "movies": [{"imdb_id": "tt1111111"}],
                "shows": [],
            },
        ]
        mdb.add_items.return_value = {"added": {"movies": 1, "shows": 0}}

        items = [
            {
                "title": "Kept Movie",
                "type": "movie",
                "year": 2026,
                "imdb_id": "tt1111111",
            },
            {
                "title": "Rejected Movie",
                "type": "movie",
                "year": 2025,
                "imdb_id": "tt2222222",
                "tmdb_id": 222,
            },
        ]

        result = sync.sync_items(mdb, 220123, items, "Top 10 Test Movies Today")

        self.assertFalse(result)
        error_calls = [call.args for call in sync.logger.error.call_args_list]
        self.assertTrue(
            any(
                args
                and args[0].startswith("    Missing item:")
                and "Rejected Movie" in args
                and "tt2222222" in args
                and 222 in args
                for args in error_calls
            )
        )
        self.assertFalse(
            any(args and "Kept Movie" in args for args in error_calls)
        )

    def test_no_missing_item_diagnostics_when_failure_happens_before_add(self):
        sync = Mock()
        sync.logger = Mock()

        def original_sync_items(mdb, list_id, items, name):
            mdb.get_list_items(list_id)
            return False

        sync.sync_items = original_sync_items
        static_fix._patch_missing_item_diagnostics(sync)

        mdb = Mock()
        mdb.get_list_items.return_value = {"movies": [], "shows": []}

        sync.sync_items(
            mdb,
            220123,
            [{"title": "Example", "type": "movie", "imdb_id": "tt1234567"}],
            "Top 10 Test Movies Today",
        )

        self.assertFalse(
            any(
                call.args and "Missing item:" in call.args[0]
                for call in sync.logger.error.call_args_list
            )
        )


if __name__ == "__main__":
    unittest.main()
