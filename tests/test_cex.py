import unittest

from app.scraper.cex_ie import CexPrices, search_query_from_title
from app.services.cex_arbitrage import build_cex_row, required_cash_profit
from app.services.drop_tiers import drop_tier, tiered_deep_drops


class TestDropTier(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(drop_tier(70, [40, 50, 66]), 66)
        self.assertEqual(drop_tier(55, [40, 50, 66]), 50)
        self.assertEqual(drop_tier(45, [40, 50, 66]), 40)
        self.assertIsNone(drop_tier(30, [40, 50, 66]))


class TestCexProfit(unittest.TestCase):
    def test_required_profit(self):
        self.assertEqual(required_cash_profit(100, min_eur=15, min_percent=10), 15)
        self.assertEqual(required_cash_profit(200, min_eur=15, min_percent=10), 20)

    def test_build_row_flags_flip(self):
        item = {
            "asin": "B001",
            "title": "Playstation 5",
            "url": "https://www.amazon.ie/dp/B001",
            "price_eur": 200.0,
            "drop_percent": 55.0,
            "drop_tier": 50,
        }
        prices = CexPrices(
            box_id="BOX1",
            box_name="PS5",
            category_name="Gaming",
            cash_eur=250.0,
            voucher_eur=270.0,
            retail_eur=400.0,
            url="https://ie.webuy.com/product-detail?id=BOX1",
        )
        row = build_cex_row(item, prices, min_profit_eur=15, min_profit_percent=10)
        self.assertTrue(row["cex_cash_flip"])
        self.assertEqual(row["cex_cash_profit_eur"], 50.0)

        row2 = build_cex_row(
            item,
            CexPrices(
                box_id="BOX1",
                box_name="PS5",
                category_name="Gaming",
                cash_eur=210.0,
                voucher_eur=230.0,
                retail_eur=400.0,
                url="https://ie.webuy.com/product-detail?id=BOX1",
            ),
            min_profit_eur=15,
            min_profit_percent=10,
        )
        self.assertFalse(row2["cex_cash_flip"])


class TestSearchQuery(unittest.TestCase):
    def test_strips_noise(self):
        q = search_query_from_title("Sony PS5 Console White - Amazon.ie Renewed")
        self.assertIn("Sony", q)
        self.assertNotIn("Amazon", q)


class TestTieredDrops(unittest.TestCase):
    def test_groups_by_highest_tier(self):
        drops = [
            {"asin": "a", "drop_percent": 70, "drop_tier": 66},
            {"asin": "b", "drop_percent": 45, "drop_tier": 40},
        ]
        grouped = tiered_deep_drops(drops, [40, 50, 66])
        self.assertEqual(len(grouped["66"]), 1)
        self.assertEqual(len(grouped["40"]), 1)
        self.assertEqual(grouped["50"], [])


if __name__ == "__main__":
    unittest.main()
