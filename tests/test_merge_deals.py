import unittest

from app.bestseller_sections import rotate_section_id, CrawlSection
from app.deals import merge_section_deals


class TestMergeDeals(unittest.TestCase):
    def test_merge_two_sections(self):
        gaming = {
            "section_id": "gaming",
            "section_name": "Gaming",
            "generated_at": "2026-11-02T12:00:00+00:00",
            "items_seen": 100,
            "items_priced": 90,
            "items_with_history": 10,
            "categories_crawled": 5,
            "requests": 20,
            "errors": 0,
            "blocked": False,
            "drops": [{"asin": "A1", "drop_percent": 50, "price_eur": 10}],
            "deep_drops": {"40": [{"asin": "A1", "drop_percent": 50}]},
            "deep_drop_tiers": [40, 50, 66],
            "cex_flips": [],
            "cex_arbitrage": [{"asin": "G1", "cex_cash_profit_eur": 20}],
            "cex_near_margin": [],
            "cex_lookups": [],
            "cex_stats": {"lookups": 1, "arbitrage": 1},
            "cex_enabled": True,
            "cex_min_cash_profit_eur": 5,
            "cex_min_cash_profit_percent": 10,
            "alerts": [],
        }
        home = {
            **gaming,
            "section_id": "home",
            "section_name": "Home",
            "generated_at": "2026-11-02T12:30:00+00:00",
            "drops": [{"asin": "B1", "drop_percent": 45, "price_eur": 20}],
            "cex_arbitrage": [],
        }
        merged = merge_section_deals(merge_section_deals(None, gaming), home)
        self.assertIn("gaming", merged["sections"])
        self.assertIn("home", merged["sections"])
        self.assertEqual(merged["last_section_id"], "home")
        self.assertEqual(len(merged["drops"]), 2)
        self.assertEqual(len(merged["cex_arbitrage"]), 1)


class TestRotate(unittest.TestCase):
    def test_cycles(self):
        sections = [
            CrawlSection("gaming", "G", []),
            CrawlSection("home", "H", []),
        ]
        self.assertEqual(rotate_section_id(sections, 0), "gaming")
        self.assertEqual(rotate_section_id(sections, 1), "home")
        self.assertEqual(rotate_section_id(sections, 2), "gaming")


if __name__ == "__main__":
    unittest.main()
