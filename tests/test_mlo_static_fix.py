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


if __name__ == "__main__":
    unittest.main()
