"""General Amazon discounts must not be gated by CeX or price-error alerts."""
import tempfile
import unittest
from pathlib import Path

from app import database as db
from app.config import settings
from app.bestseller_sections import CrawlSection, config_for_all_sections, config_for_section
from app.deals import export_deals, merge_section_deals
from app.scraper.bestsellers import BestsellerItem
from app.services.bestsellers import CrawlConfig, CrawlResult, ProcessResult, process_crawl


ASIN = "B000000001"


def scan(price, asin=ASIN):
    return CrawlResult(items={
        asin: (BestsellerItem(
            asin, "Sony Headphones", price, 1,
            f"https://www.amazon.ie/dp/{asin}"), "Electronics")
    })


class TestAmazonGeneralDrops(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.before = settings.database_path
        settings.database_path = Path(self.tmp.name) / "tracker.db"
        db.init_db()
        self.config = CrawlConfig(roots=[], general_drop_percent=10, report_drop_percent=30)

    def tearDown(self):
        settings.database_path = self.before
        self.tmp.cleanup()

    def test_15_percent_drop_visible_without_cex_or_price_error_alert(self):
        for _ in range(3):
            prior = process_crawl(scan(100), self.config)
            self.assertEqual(prior.general_drops, [])
        result = process_crawl(scan(85), self.config)
        self.assertEqual(len(result.general_drops), 1)
        drop = result.general_drops[0]
        self.assertAlmostEqual(drop["drop_percent"], 15.0, places=1)
        self.assertEqual(drop["savings_eur"], 15.0)
        self.assertEqual(drop["reference_eur"], 100.0)
        self.assertEqual(drop["price_eur"], 85.0)
        self.assertEqual(result.drops, [], "30% shortlist is separate")
        self.assertEqual(result.cex_lookups, [], "CeX not required to report a drop")

    def test_deep_drop_in_both_feeds_reference_stays_stable(self):
        for _ in range(3):
            process_crawl(scan(100), self.config)
        first = process_crawl(scan(65), self.config)
        self.assertEqual(len(first.general_drops), 1)
        self.assertEqual(len(first.drops), 1)
        self.assertEqual(first.general_drops[0]["savings_eur"], 35)
        for _ in range(8):
            again = process_crawl(scan(65), self.config)
        self.assertAlmostEqual(again.general_drops[0]["drop_percent"], 35.0)

    def test_section_scoping_preserves_general_threshold(self):
        self.config.general_drop_percent = 18
        sections = [CrawlSection("gaming", "Gaming", []), CrawlSection("home", "Home", [])]
        self.assertEqual(config_for_section(self.config, sections[0]).general_drop_percent, 18)
        self.assertEqual(config_for_all_sections(self.config, sections).general_drop_percent, 18)

    def test_export_merge_unlimited_drops_and_found_at(self):
        processed = ProcessResult(
            general_drops=[{
                "asin": f"A{i:09d}", "title": f"Test product {i}",
                "url": f"https://www.amazon.ie/dp/A{i:09d}",
                "price_eur": 75, "reference_eur": 100,
                "savings_eur": 25, "drop_percent": 25,
            } for i in range(90)]
        )
        payload = export_deals(scan(75), processed, github_repo="example/tracker",
                               config=self.config, section_id="gaming", section_name="Gaming")
        self.assertEqual(len(payload["general_drops"]), 90, "No old 50-drop limit")
        merged = merge_section_deals(None, payload)
        self.assertEqual(len(merged["general_drops"]), 90)
        old_first = merged["sections"]["gaming"]["general_drops"][0]["found_at"]
        new_payload = {**payload, "generated_at": "2026-11-02T22:10:00+00:00",
                       "general_drops": [{**r, "found_at": "2026-11-02T22:10:00+00:00"}
                                         for r in payload["general_drops"]]}
        newer = merge_section_deals(merged, new_payload)
        self.assertEqual(newer["sections"]["gaming"]["general_drops"][0]["found_at"], old_first)
        home = {**new_payload, "section_id": "home", "section_name": "Home",
                "general_drops": [{
                    "asin": "B000000001", "price_eur": 40, "reference_eur": 50,
                    "savings_eur": 10, "drop_percent": 20,
                }]}
        combined = merge_section_deals(newer, home)
        self.assertEqual(len(combined["general_drops"]), 91)
        self.assertEqual(len(combined["sections"]["gaming"]["general_drops"]), 90)
        self.assertEqual(combined["general_drop_percent"], 10)


if __name__ == "__main__":
    unittest.main()
