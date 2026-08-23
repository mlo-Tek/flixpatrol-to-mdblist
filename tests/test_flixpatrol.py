import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import flixpatrol_to_mdblist as sync


RANKING_HTML = """
<html><head><title>FlixPatrol</title><script src="/cdn-cgi/challenge-platform/managed.js"></script></head><body>
<h3>TOP 10 Movies</h3>
<table>
  <tr><td>1.</td><td><a href="/title/movie-one/">Movie One</a></td></tr>
  <tr><td>2.</td><td><a href="/title/movie-two/">Movie Two</a></td></tr>
</table>
<h3>TOP 10 TV Shows</h3>
<table>
  <tr><td>1.</td><td><a href="/title/show-one/">Show One</a></td></tr>
  <tr><td>2.</td><td><a href="/title/show-two/">Show Two</a></td></tr>
</table>
<h3>TOP 10 Overall (from Amazon Channels)</h3>
<table>
  <tr><td>1.</td><td><a href="/title/overall-one/">Overall One</a></td></tr>
  <tr><td>2.</td><td><a href="/title/overall-two/">Overall Two</a></td></tr>
</table>
</body></html>
"""

CHALLENGE_HTML = """
<html><head><title>Just a moment...</title></head>
<body><script src="/cdn-cgi/challenge-platform/test"></script></body></html>
"""


def response_with(data):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = data
    return response


class CloudflareDetectionTests(unittest.TestCase):
    def test_detects_header_and_html_challenges(self):
        self.assertTrue(sync._is_cloudflare_challenge(
            "<html>normal</html>", {"cf-mitigated": "challenge"}
        ))
        self.assertTrue(sync._is_cloudflare_challenge(CHALLENGE_HTML))
        self.assertFalse(sync._is_cloudflare_challenge(RANKING_HTML))

    def test_direct_request_rejects_challenge_page(self):
        scraper = sync.FlixPatrolScraper(
            sync.FileCache(Path("/tmp/unused-cache"), enabled=False)
        )
        scraper.solver = None
        response = Mock(text=CHALLENGE_HTML, headers={"cf-mitigated": "challenge"})
        scraper.session.get = Mock(return_value=response)

        self.assertIsNone(scraper._get("https://flixpatrol.com/top10/netflix/germany"))
        response.raise_for_status.assert_not_called()
        scraper.close()


class FlareSolverrClientTests(unittest.TestCase):
    def test_reuses_session_and_closes_it(self):
        client = sync.FlareSolverrClient("http://solver:8191", max_timeout=12)
        client.http.post = Mock(side_effect=[
            response_with({"status": "ok", "session": client.session_id}),
            response_with({
                "status": "ok",
                "solution": {
                    "status": 200,
                    "headers": {"content-type": "text/html"},
                    "response": RANKING_HTML,
                },
            }),
            response_with({"status": "ok"}),
        ])

        html = client.fetch("https://flixpatrol.com/top10/netflix/germany")
        client.close()

        self.assertEqual(html, RANKING_HTML)
        payloads = [call.kwargs["json"] for call in client.http.post.call_args_list]
        self.assertEqual(
            [payload["cmd"] for payload in payloads],
            ["sessions.create", "request.get", "sessions.destroy"],
        )
        self.assertEqual(payloads[1]["session"], client.session_id)
        self.assertEqual(payloads[1]["maxTimeout"], 12000)

    def test_rejects_unsolved_challenge(self):
        client = sync.FlareSolverrClient("http://solver:8191")
        client.started = True
        client.http.post = Mock(return_value=response_with({
            "status": "ok",
            "solution": {
                "status": 200,
                "headers": {"cf-mitigated": "challenge"},
                "response": CHALLENGE_HTML,
            },
        }))

        with self.assertRaisesRegex(sync.FlareSolverrError, "was not solved"):
            client.fetch("https://flixpatrol.com/top10/netflix/germany")


class MDBListClientTests(unittest.TestCase):
    def test_search_removes_invisible_unicode_formatting_characters(self):
        client = sync.MDBListClient("secret")
        client._req = Mock(return_value={"search": []})

        client.search("Chompoo: Lost\u200b &\u200b Forgotten\u200b", "movie", 2026)

        self.assertEqual(
            client._req.call_args.kwargs["params"]["query"],
            "Chompoo: Lost & Forgotten",
        )

    def test_api_errors_do_not_log_api_key(self):
        api_key = "super-secret-api-key"
        client = sync.MDBListClient(api_key)
        response = sync.requests.Response()
        response.status_code = 400
        response._content = b'{"message":"Invalid search query"}'
        response.url = f"https://api.mdblist.com/search/movie?apikey={api_key}"
        client.session.request = Mock(side_effect=sync.requests.HTTPError(
            f"400 Client Error for url: {response.url}", response=response
        ))

        with self.assertLogs(sync.logger, level="ERROR") as captured:
            client.search("Invalid", "movie")

        logs = "\n".join(captured.output)
        self.assertNotIn(api_key, logs)
        self.assertIn("HTTP 400", logs)


class ConfigTests(unittest.TestCase):
    def test_missing_config_is_created_from_bundled_default(self):
        bundled = Path(__file__).resolve().parents[1] / "config" / "default.json"
        with tempfile.TemporaryDirectory() as temp_dir:
            config_dir = Path(temp_dir) / "config"
            config_file = config_dir / "default.json"
            with (
                patch.object(sync, "CONFIG_DIR", config_dir),
                patch.object(sync, "CONFIG_FILE", config_file),
                patch.object(sync, "BUNDLED_CONFIG_FILE", bundled),
                self.assertRaises(SystemExit),
            ):
                sync.load_config()

            installed = json.loads(config_file.read_text())
            expected = json.loads(bundled.read_text())
            self.assertEqual(installed, expected)


class Top10ParsingTests(unittest.TestCase):
    def test_both_applies_limit_per_media_type(self):
        scraper = sync.FlixPatrolScraper(
            sync.FileCache(Path("/tmp/unused-cache"), enabled=False)
        )
        scraper.solver = None
        scraper._get = Mock(return_value=sync.BeautifulSoup(RANKING_HTML, "html.parser"))

        items = scraper.get_top10(
            "netflix", "germany", media_type="both", limit=1
        )

        self.assertEqual(
            [(item["title"], item["type"]) for item in items],
            [("Movie One", "movie"), ("Show One", "show")],
        )
        scraper.close()

    def test_top10_lists_are_defined_in_json_config_only(self):
        config_path = Path(__file__).resolve().parents[1] / "config" / "default.json"
        entries = json.loads(config_path.read_text())["FlixPatrolTop10"]

        self.assertTrue(entries)
        us_only = {"hulu", "peacock"}
        for entry in entries:
            expected_location = (
                "united-states" if entry["platform"] in us_only else "germany"
            )
            self.assertEqual(entry["location"], expected_location)

        configured = {
            (entry["platform"], entry["type"], entry["location"])
            for entry in entries
        }
        self.assertIn(("netflix", "movies", "germany"), configured)
        self.assertIn(("netflix", "shows", "germany"), configured)
        self.assertIn(("hulu", "movies", "united-states"), configured)
        self.assertIn(("hulu", "shows", "united-states"), configured)
        self.assertIn(("peacock", "movies", "united-states"), configured)
        self.assertIn(("peacock", "shows", "united-states"), configured)
        self.assertIn(("crunchyroll", "overall", "germany"), configured)
        self.assertEqual(sync.DEFAULT_CONFIG["FlixPatrolTop10"], [])
        self.assertFalse(hasattr(sync, "GERMANY_TOP10_PROVIDERS"))

    def test_overall_chart_is_parsed_and_marked_for_later_classification(self):
        scraper = sync.FlixPatrolScraper(
            sync.FileCache(Path("/tmp/unused-cache"), enabled=False)
        )
        scraper.solver = None
        scraper._get = Mock(return_value=sync.BeautifulSoup(RANKING_HTML, "html.parser"))

        items = scraper.get_top10(
            "crunchyroll", "germany", media_type="overall", limit=2
        )

        self.assertEqual([item["type"] for item in items], ["overall", "overall"])
        scraper.close()

    def test_overall_items_use_mdblist_metadata_for_movie_or_show(self):
        scraper = Mock()
        scraper.get_title_info.side_effect = [
            {"year": 2025, "imdb_id": None, "media_type_hint": "movie"},
            {"year": 2024, "imdb_id": None, "media_type_hint": "movie"},
        ]
        matcher = Mock()
        matcher.find.side_effect = [
            {"imdb_id": "tt0000001", "_media_type": "movie", "_src": "test"},
            {"imdb_id": "tt0000002", "_media_type": "show", "_src": "test"},
        ]

        with patch.object(sync.time, "sleep"):
            matched = sync._match_all([
                {"title": "Movie", "url": "/movie", "type": "overall"},
                {"title": "Series", "url": "/series", "type": "overall"},
            ], scraper, matcher)

        self.assertEqual([item["type"] for item in matched], ["movie", "show"])
        self.assertEqual(
            [call.args[2] for call in matcher.find.call_args_list],
            ["overall", "overall"],
        )

    def test_overall_matcher_uses_mdblist_any_result_type(self):
        mdb = Mock()
        mdb.search.return_value = [{
            "title": "Example Series",
            "release_year": 2024,
            "mediatype": "show",
            "ids": {"imdb": "tt0000003"},
        }]
        matcher = sync.TitleMatcher(
            mdb, sync.FileCache(Path("/tmp/unused-cache"), enabled=False)
        )

        result = matcher.find(
            "Example Series",
            {"year": 2024, "imdb_id": "tt-wrong-type", "media_type_hint": "movie"},
            "overall",
        )

        self.assertEqual(result["_media_type"], "show")
        self.assertEqual(result["imdb_id"], "tt0000003")
        mdb.search.assert_called_once_with("Example Series", "any", 2024)


if __name__ == "__main__":
    unittest.main()
