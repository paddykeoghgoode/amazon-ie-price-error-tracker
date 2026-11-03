import json
import tempfile
import unittest
from pathlib import Path

from app.bestseller_sections import config_for_section, load_sections
from app.scraper.bestsellers import BestsellerItem
from app.services.bestsellers import CrawlConfig, CategoryRoot, load_config, passes_signal_gates


class TestSectionFocus(unittest.TestCase):
    def test_bestsellers_json_has_focus_sections(self):
        sections = load_sections(Path("bestsellers.json"))
        self.assertEqual(
            [s.id for s in sections],
            [
                "videogames",
                "pc_hardware",
                "cex_priority",
                "coffee",
                "home",
                "lifestyle",
                "general",
            ],
        )
        coffee = next(s for s in sections if s.id == "coffee")
        self.assertEqual(coffee.signal_min_price_eur, 100.0)
        self.assertEqual(coffee.signal_min_review_count, 100)
        cex = next(s for s in sections if s.id == "cex_priority")
        self.assertEqual(cex.cex_live_lookups_per_run, 22)
        self.assertEqual(cex.catalogue_checks_per_run, 150)

    def test_config_for_section_applies_overrides(self):
        base = load_config(Path("bestsellers.json"))
        coffee = next(s for s in load_sections(Path("bestsellers.json")) if s.id == "coffee")
        scoped = config_for_section(base, coffee)
        self.assertEqual(scoped.signal_min_price_eur, 100.0)
        self.assertEqual(scoped.signal_min_review_count, 100)
        self.assertFalse(any(r.path == "/gp/movers-and-shakers" for r in scoped.roots))

    def test_signal_gates_for_coffee(self):
        config = CrawlConfig(
            roots=[],
            signal_min_price_eur=100.0,
            signal_min_review_count=100,
        )
        cheap = BestsellerItem("A", "Pod", 80.0, 1, "https://example", review_count=500)
        low_reviews = BestsellerItem("B", "Sage", 400.0, 2, "https://example", review_count=40)
        ok = BestsellerItem("C", "Sage barista", 400.0, 3, "https://example", review_count=250)
        self.assertFalse(passes_signal_gates(cheap, config))
        self.assertFalse(passes_signal_gates(low_reviews, config))
        self.assertTrue(passes_signal_gates(ok, config))

    def test_section_discovery_feeds_merge_without_global(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cfg.json"
            path.write_text(
                json.dumps(
                    {
                        "sections": [
                            {
                                "id": "videogames",
                                "name": "VG",
                                "categories": [{"path": "/gp/bestsellers/videogames", "depth": 1}],
                                "include_global_discovery_feeds": False,
                                "discovery_feeds": [
                                    {
                                        "path": "/gp/movers-and-shakers/videogames",
                                        "depth": 0,
                                        "pages": 1,
                                    }
                                ],
                            }
                        ],
                        "discovery_feeds": [
                            {"path": "/gp/movers-and-shakers/electronics", "depth": 0, "pages": 1}
                        ],
                    }
                ),
                encoding="utf-8",
            )
            base = load_config(path)
            section = load_sections(path)[0]
            scoped = config_for_section(base, section)
            paths = [r.path for r in scoped.roots]
            self.assertIn("/gp/bestsellers/videogames", paths)
            self.assertIn("/gp/movers-and-shakers/videogames", paths)
            self.assertNotIn("/gp/movers-and-shakers/electronics", paths)


if __name__ == "__main__":
    unittest.main()
