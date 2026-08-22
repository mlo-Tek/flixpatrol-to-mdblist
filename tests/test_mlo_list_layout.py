import sys
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
