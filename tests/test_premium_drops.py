import unittest

from app.services.bestsellers import CrawlConfig
from app.services.premium_drops import (
    observe_premium_history_drop,
    observe_rapid_scan_drop,
    observation_to_row,
)


class TestPremiumDrops(unittest.TestCase):
    def setUp(self):
        self.config = CrawlConfig(
            roots=[],
            premium_min_price_eur=100,
            premium_history_drop_percent=45,
            rapid_drop_min_price_eur=80,
            rapid_drop_percent=20,
            rapid_drop_min_saving_eur=30,
        )

    def test_rapid_drop_on_expensive_item(self):
        obs = observe_rapid_scan_drop(
            asin="B001",
            title="TV",
            url="https://www.amazon.ie/dp/B001",
            category="Electronics",
            rank=1,
            price_eur=160.0,
            previous_eur=220.0,
            config=self.config,
        )
        self.assertIsNotNone(obs)
        self.assertEqual(obs.drop_kind, "rapid_scan")
        self.assertGreaterEqual(obs.drop_percent, 20)

    def test_rapid_drop_ignored_for_cheap_items(self):
        self.assertIsNone(
            observe_rapid_scan_drop(
                asin="B002",
                title="Cable",
                url="https://www.amazon.ie/dp/B002",
                category=None,
                rank=2,
                price_eur=15.0,
                previous_eur=25.0,
                config=self.config,
            )
        )

    def test_premium_history_between_45_and_75(self):
        obs = observe_premium_history_drop(
            asin="B003",
            title="Console",
            url="https://www.amazon.ie/dp/B003",
            category="Gaming",
            rank=3,
            price_eur=120.0,
            reference_eur=240.0,
            config=self.config,
        )
        self.assertIsNotNone(obs)
        row = observation_to_row(obs)
        self.assertEqual(row["drop_kind"], "premium_history")
        self.assertGreaterEqual(row["drop_percent"], 45)


if __name__ == "__main__":
    unittest.main()
