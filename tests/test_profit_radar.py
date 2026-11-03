import unittest
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from app.scraper.product_identity import amazon_identifiers, normalise_barcode, normalise_model
from app.scraper.cex_ie import CexPrices
from app.services.cex_arbitrage import build_cex_row
from scripts.scan_hotlist import candidates


class TestIdentifiers(unittest.TestCase):
    def test_valid_ean13(self):
        self.assertEqual(normalise_barcode("4006381333931"), "04006381333931")
        self.assertIsNone(normalise_barcode("4006381333932"))
        self.assertIsNone(normalise_barcode("12345"))

    def test_detail_not_review_is_source(self):
        page = BeautifulSoup(
            '<div id="productDetails_techSpec_section_1"><table><tr><th>EAN</th>'
            '<td>4006381333931</td></tr><tr><th>Item model number</th>'
            '<td>WH-1000XM5</td></tr></table></div><div>UPC 12345 review</div>',
            "lxml")
        code, model = amazon_identifiers(page, "B000000001")
        self.assertEqual(code, "04006381333931")
        self.assertEqual(model, "wh1000xm5")


class TestNetProfit(unittest.TestCase):
    def test_ceiling_and_roi_account_for_costs(self):
        item = {"price_eur": 100.0}
        prices = CexPrices("B", "Sony Headset", None, 150, 160, 180, "https://ie.webuy.com")
        row = build_cex_row(item, prices, min_profit_eur=15, min_profit_percent=10,
                            extra_cost_eur=5, delivery_eur=4, min_roi_percent=20, payout_haircut_percent=10)
        self.assertEqual(row["cex_expected_cash_eur"], 135.0)
        self.assertEqual(row["cex_cash_profit_eur"], 26.0)
        self.assertEqual(row["cex_break_even_buy_eur"], 126.0)
        self.assertLess(row["cex_max_buy_eur"], row["cex_break_even_buy_eur"])
        self.assertTrue(row["cex_cash_flip"])

    def test_too_small_roi_is_not_flip(self):
        prices = CexPrices("B", "Sony Headset", None, 110, 120, 180, "https://ie.webuy.com")
        row = build_cex_row({"price_eur": 100}, prices, min_profit_eur=5, min_profit_percent=0,
                            min_roi_percent=15)
        self.assertFalse(row["cex_cash_flip"])


class TestHotlist(unittest.TestCase):
    def test_recent_observed_drops_do_not_require_seller(self):
        now = datetime.now(timezone.utc)
        data = {"drops": [
            {"asin": "A", "title": "Sony headset", "price_eur": 150, "reference_eur": 290, "drop_percent": 48, "found_at": now.isoformat()},
            {"asin": "B", "title": "Sony headset", "price_eur": 140, "reference_eur": 650, "drop_percent": 78, "found_at": now.isoformat()},
            {"asin": "C", "title": "Sony game", "price_eur": 4, "reference_eur": 70, "drop_percent": 94, "found_at": now.isoformat()},
        ]}
        self.assertEqual([r["asin"] for r in candidates(data, now)], ["B"])  # Cheap games excluded


if __name__ == "__main__":
    unittest.main()
