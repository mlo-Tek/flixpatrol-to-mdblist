import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import flixpatrol_to_mdblist as sync
import mlo_patches


JOY_STREAMING_HTML = """
<html><body>
<h2>Joyn TOP 10 in Germany on August 22, 2026</h2>
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
<h2>Netflix TOP 10 in Germany on August 22, 2026</h2>
<h3>TOP 10 Movies</h3>
<table><tr><td>1.</td><td><a href="/title/wrong/">Wrong Provider</a></td></tr></table>
</body></html>
"""


class MloAliasTests(unittest.TestCase):
    def test_colon_subtitle_generates_safe_canonical_alias(self):
        self.assertEqual(
            mlo_patches._title_aliases(
                sync, "13 Days, 13 Nights: In the Hell of Kabul"
            ),
            ["13 Days, 13 Nights"],
        )

    def test_movie_part_and_dash_subtitle_generate_demon_slayer_alias(self):
        aliases = mlo_patches._title_aliases(
            sync,
            "Demon Slayer: Kimetsu no Yaiba Infinity Castle Movie 1 - Akaza's Revenge",
        )
        self.assertIn(
            "Demon Slayer: Kimetsu no Yaiba Infinity Castle",
            aliases,
        )
        self.assertNotIn("Demon Slayer", aliases)

    def test_standalone_number_generates_word_alias(self):
        aliases = mlo_patches._title_aliases(
            sync, "Fantastic 4: Rise of the Silver Surfer"
        )
        self.assertIn(
            "Fantastic Four: Rise of the Silver Surfer",
            aliases,
        )

    def test_short_ambiguous_titles_are_not_shortened(self):
        self.assertEqual(mlo_patches._title_aliases(sync, "From"), [])
        self.assertEqual(mlo_patches._title_aliases(sync, "It: Welcome to Derry"), [])


class MDBListQueryCompatibilityTests(unittest.TestCase):
    def test_problematic_punctuation_is_normalized_for_search_only(self):
        self.assertEqual(
            mlo_patches._mdblist_safe_query(
                sync, "Die Landarztpraxis – Team Sonnenhof"
            ),
            "Die Landarztpraxis - Team Sonnenhof",
        )
        self.assertEqual(
            mlo_patches._mdblist_safe_query(
                sync, "Born Famous - Fluch oder Segen?"
            ),
            "Born Famous - Fluch oder Segen",
        )


class VerifiedIdTests(unittest.TestCase):
    def test_the_lord_of_the_skies_has_verified_ids(self):
        result = mlo_patches._verified_id_match(
            sync, "The Lord of the Skies", 2013, "show"
        )
        self.assertEqual(result["imdb_id"], "tt2777882")
        self.assertEqual(result["tmdb_id"], 44953)

    def test_dschungel_divas_has_verified_tmdb_id(self):
        result = mlo_patches._verified_id_match(
            sync, "Dschungel Divas - Luxus hat seinen Preis", 2026, "show"
        )
        self.assertEqual(result["tmdb_id"], 329186)

    def test_verified_ids_require_exact_year_and_type(self):
        self.assertIsNone(
            mlo_patches._verified_id_match(
                sync, "The Lord of the Skies", 2014, "show"
            )
        )
        self.assertIsNone(
            mlo_patches._verified_id_match(
                sync, "The Lord of the Skies", 2013, "movie"
            )
        )


class JoynOverviewTests(unittest.TestCase):
    def test_provider_parser_stops_before_next_provider(self):
        scraper = sync.FlixPatrolScraper(
            sync.FileCache(Path("/tmp/unused-mlo-cache"), enabled=False)
        )
        scraper.solver = None
        soup = sync.BeautifulSoup(JOY_STREAMING_HTML, "html.parser")

        sections = mlo_patches._parse_provider_sections(
            sync, scraper, soup, "joyn"
        )

        self.assertEqual(
            [item["title"] for item in sections[("movies", False)]],
            ["Movie One", "Movie Two"],
        )
        self.assertEqual(
            [item["title"] for item in sections[("shows", False)]],
            ["Show One", "Show Two"],
        )
        self.assertNotIn(
            "Wrong Provider",
            [
                item["title"]
                for items in sections.values()
                for item in items
            ],
        )
        scraper.close()

    def test_duplicate_rows_are_removed_by_url(self):
        items = [
            {"title": "Dschungel Divas", "url": "https://example/title/dschungel"},
            {"title": "Dschungel Divas", "url": "https://example/title/dschungel"},
            {"title": "NCIS", "url": "https://example/title/ncis"},
        ]
        deduped = mlo_patches._dedupe_ranking_items(sync, items)
        self.assertEqual(
            [item["title"] for item in deduped],
            ["Dschungel Divas", "NCIS"],
        )


if __name__ == "__main__":
    unittest.main()
