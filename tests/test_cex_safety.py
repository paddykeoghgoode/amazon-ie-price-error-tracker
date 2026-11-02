"""CeX SKU safety checks: same brand is not enough for the same trade-in price."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from app.scraper import cex_ie
from app.scraper.cex_ie import CexPrices, titles_match_for_trade_in
from app.services.bestsellers import CrawlConfig
from app.services.cex_arbitrage import build_cex_row, enrich_with_cex


class TestCexSkuMatching(unittest.TestCase):
    def test_rejects_different_storage(self):
        self.assertFalse(titles_match_for_trade_in(
            "Apple iPhone 15 Pro 128GB Blue", "Apple iPhone 15 Pro 256GB Blue"
        ))

    def test_accepts_same_storage_and_model(self):
        self.assertTrue(titles_match_for_trade_in(
            "Apple iPhone 15 Pro 128 GB Blue", "Apple iPhone 15 Pro 128GB Black"
        ))

    def test_rejects_missing_storage_variant(self):
        self.assertFalse(titles_match_for_trade_in(
            "Samsung Galaxy S23 256GB", "Samsung Galaxy S23"
        ))

    def test_rejects_wrong_game_year(self):
        self.assertFalse(titles_match_for_trade_in(
            "EA Sports FC 25 PS5", "EA Sports FC 24 (PS5)"
        ))

    def test_accepts_matching_game_and_platform(self):
        self.assertTrue(titles_match_for_trade_in(
            "EA Sports FC 25 PS5", "EA Sports FC 25 (PlayStation 5)"
        ))

    def test_rejects_wrong_game_platform(self):
        self.assertFalse(titles_match_for_trade_in(
            "EA Sports FC 25 PS5", "EA Sports FC 25 Xbox Series X"
        ))

    def test_rejects_wrong_headset_model(self):
        self.assertFalse(titles_match_for_trade_in(
            "SteelSeries Arctis 7P Wireless Gaming Headset",
            "SteelSeries Arctis 7 Wireless Gaming Headset"
        ))

    def test_rejects_wrong_phone_generation(self):
        self.assertFalse(titles_match_for_trade_in(
            "Samsung Galaxy S23 128GB", "Samsung Galaxy S24 128GB"
        ))

    def test_rejects_wrong_gpu(self):
        self.assertFalse(titles_match_for_trade_in(
            "NVIDIA RTX 4070 Ti GPU", "NVIDIA RTX 4060 Ti GPU"
        ))

    def test_rejects_console_edition(self):
        self.assertFalse(titles_match_for_trade_in(
            "Sony PlayStation 5 Pro 1TB Console", "Sony PlayStation 5 Slim 1TB Console"
        ))

    def test_rejects_multipack_for_single(self):
        self.assertFalse(titles_match_for_trade_in(
            "Sony PlayStation 5 2-pack", "Sony PlayStation 5"
        ))

    def test_rejects_digital_code(self):
        self.assertFalse(titles_match_for_trade_in(
            "EA Sports FC 25 PS5 digital download code", "EA Sports FC 25 PS5"
        ))

    def test_accepts_existing_distinctive_model(self):
        self.assertTrue(titles_match_for_trade_in(
            "SteelSeries Arctis 7P Wireless Gaming Headset",
            "SteelSeries Arctis 7P Headset Black"
        ))


class TestCexQuoteValidation(unittest.TestCase):
    def test_margin_uses_costs_and_cash_haircut(self):
        item = {"asin": "B000000001", "title": "Sony PS5", "price_eur": 100.0}
        prices = CexPrices("box", "Sony PS5", None, 125.0, 150.0, 180.0, "https://ie.webuy.com")
        result = build_cex_row(item, prices, min_profit_eur=5, min_profit_percent=10,
                               extra_cost_eur=5, payout_haircut_percent=10)
        self.assertEqual(result["cex_expected_cash_eur"], 112.5)
        self.assertEqual(result["cex_cash_profit_eur"], 7.5)
        self.assertFalse(result["cex_cash_flip"])

    def test_rejects_quote_when_detail_is_for_different_sku(self):
        candidate = {
            "asin": "B000000001",
            "title": "Apple iPhone 15 Pro 128GB",
            "url": "https://www.amazon.ie/dp/B000000001",
            "price_eur": 100.0,
        }
        mismatched_quote = CexPrices(
            box_id="bad",
            box_name="Apple iPhone 15 Pro 256GB",
            category_name="Phones",
            cash_eur=400.0,
            voucher_eur=450.0,
            retail_eur=600.0,
            url="https://ie.webuy.com/product-detail?id=bad",
        )
        async def check():
            with (
                patch.object(cex_ie, "search_boxes", new_callable=AsyncMock) as search,
                patch.object(cex_ie, "fetch_box_prices", new_callable=AsyncMock) as prices,
            ):
                search.return_value = [{"boxId": "bad", "boxName": candidate["title"]}]
                prices.return_value = mismatched_quote
                return await enrich_with_cex(
                    [candidate],
                    CrawlConfig(roots=[], cex_request_delay_seconds=0),
                    client=object(),
                )

        lookups, flips = asyncio.run(check())
        self.assertEqual([], lookups)
        self.assertEqual([], flips)


if __name__ == "__main__":
    unittest.main()
