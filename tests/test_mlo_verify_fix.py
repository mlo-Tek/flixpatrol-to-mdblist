import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import mlo_verify_fix as verify


class VerifyWriteIdTests(unittest.TestCase):
    def test_verified_xfiles_cut_uses_original_imdb_id(self):
        sync = Mock()
        sync.logger = Mock()
        item = {
            "title": "The X-Files: I Want to Believe – Vrach Frankenshteyn",
            "year": 2026,
            "type": "movie",
            "tmdb_id": 1754069,
        }

        movies, shows, rows = verify._target_payload(sync, [item])

        self.assertEqual(movies, [{"imdb": "tt0443701"}])
        self.assertEqual(shows, [])
        self.assertEqual(rows[0]["imdb"], "tt0443701")
        self.assertIsNone(rows[0]["tmdb"])

    def test_tmdb_external_ids_enrich_tmdb_only_match(self):
        sync = Mock()
        sync.TMDB_API_BASE = "https://api.themoviedb.org/3"
        sync.logger = Mock()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"imdb_id": "tt7654321"}
        sync.requests.get.return_value = response
        sync.requests.RequestException = Exception

        item = {
            "title": "Example Show",
            "year": 2026,
            "type": "show",
            "tmdb_id": 328337,
        }

        with patch.dict(os.environ, {"TMDB_API_KEY": "secret"}):
            movies, shows, rows = verify._target_payload(sync, [item])

        self.assertEqual(movies, [])
        self.assertEqual(shows, [{"imdb": "tt7654321"}])
        self.assertEqual(rows[0]["imdb"], "tt7654321")
        sync.requests.get.assert_called_once_with(
            "https://api.themoviedb.org/3/tv/328337/external_ids",
            params={"api_key": "secret"},
            timeout=15,
        )

    def test_tmdb_only_stays_tmdb_when_no_external_imdb_exists(self):
        sync = Mock()
        sync.TMDB_API_BASE = "https://api.themoviedb.org/3"
        sync.logger = Mock()
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"imdb_id": None}
        sync.requests.get.return_value = response
        sync.requests.RequestException = Exception

        item = {
            "title": "Bluey Compilations",
            "year": 2026,
            "type": "show",
            "tmdb_id": 328337,
        }

        with patch.dict(os.environ, {"TMDB_API_KEY": "secret"}):
            movies, shows, rows = verify._target_payload(sync, [item])

        self.assertEqual(movies, [])
        self.assertEqual(shows, [{"tmdb": 328337}])
        self.assertIsNone(rows[0]["imdb"])
        self.assertEqual(rows[0]["tmdb"], 328337)


if __name__ == "__main__":
    unittest.main()
