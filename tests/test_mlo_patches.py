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

    def test_short_ambiguous_titles_are_not_shortened(self):
        self.assertEqual(mlo_patches._title_aliases(sync, "From"), [])
        self.assertEqual(mlo_patches._title_aliases(sync, "It: Welcome to Derry"), [])


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


if __name__ == "__main__":
    unittest.main()
