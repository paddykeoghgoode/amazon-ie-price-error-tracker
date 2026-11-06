import asyncio
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx

from app.scraper.cex_ie import CexPrices
from app.services.cex_cache import quote_entry
from app.services.cex_retail_compare import refresh_cex_retail_compares, retail_spec_for_box
from app.services.bestsellers import CrawlConfig
from tests.test_retail_comparison import MERCHANT, MPN, EAN, product_html

NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)
QUOTE = CexPrices(
    "box1", "EA Sports FC 27", "Playstation5 Software", 46, 0, 70,
    "https://ie.webuy.com/product-detail?id=box1",
)


def state_with_link(url=MERCHANT):
    from app.services.cex_targets import target_state
    s = target_state({})
    s["items"]["box1"] = {**quote_entry(QUOTE.box_name, QUOTE, NOW), "source": "box_detail"}
    s["user_links"]["box1"] = {
        "retailer_urls": [url],
        "model": MPN,
        "barcode": EAN,
    }
    return s


class CexRetailCompareTests(unittest.TestCase):
    def test_retail_spec_uses_cex_title_when_no_model(self):
        s = state_with_link()
        s["user_links"]["box1"] = {"retailer_urls": [MERCHANT]}
        spec = retail_spec_for_box(s, "box1")
        self.assertEqual(spec["cex_title"], QUOTE.box_name)
        self.assertEqual(spec["cex_category"], QUOTE.category_name)

    def test_refresh_stores_net_and_gross_flip(self):
        async def go():
            s = state_with_link()
            config = CrawlConfig(roots=[], cex_enabled=True, cex_delivery_eur=0, cex_extra_cost_eur=0,
                                 cex_payout_haircut_percent=0, cex_min_cash_profit_eur=5, cex_min_cash_profit_percent=10)
            html = product_html(price="40.00")
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, text=html, headers={"content-type": "text/html; charset=utf-8"})
                )
            ) as client:
                stats = await refresh_cex_retail_compares(s, config, client, max_requests=5, now=NOW)
            self.assertEqual(stats["matched"], 1)
            row = s["retail_compare"]["box1"]
            self.assertEqual(row["matched_count"], 1)
            offer = row["offers"][0]
            self.assertEqual(offer["price_eur"], 40.0)
            self.assertEqual(offer["cex_gross_delta_eur"], 6.0)
            self.assertGreater(offer["cex_net_profit_eur"], 0)
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()
