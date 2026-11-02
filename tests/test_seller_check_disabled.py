"""Seller checks are disabled; scraped Amazon/CeX prices produce research leads."""
import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services.bestsellers import CrawlConfig, CrawlResult, send_cex_alerts
from app.services.catalogue import (
    apply_batch_results, catalogue_progress, make_section_catalogue,
    merge_catalogue, select_check_batch,
)
from app.scraper.bestsellers import BestsellerItem


def listings():
    return CrawlResult(items={
        "B000000001": (BestsellerItem(
            "B000000001", "Sony Headset", 50.0, 1,
            "https://www.amazon.ie/dp/B000000001",
        ), "Gaming")
    })


class TestSellerCheckDisabled(unittest.TestCase):
    def test_scan_uses_observed_prices_without_seller_verification(self):
        script = Path("scripts/crawl_bestsellers.py").read_text(encoding="utf-8")
        self.assertNotIn("verify_candidates(", script)
        self.assertNotIn("fetch_product(", script)
        self.assertIn("allow_alerts=True", script)
        self.assertIn("enrich_with_cex(", script)

    def test_old_verified_sections_become_unverified(self):
        section = make_section_catalogue(
            listings(), None, section_id="gaming", section_name="Gaming"
        )
        old = {
            **section,
            "items": [{**section["items"][0],
                "verification_status": "verified",
                "seller_name": "Amazon", "seller_type": "amazon_retail",
                "cex_status": "matched"}]
        }
        fresh = make_section_catalogue(
            listings(), old, section_id="gaming", section_name="Gaming"
        )
        merged = merge_catalogue({"sections": {"other": old}}, fresh)
        self.assertFalse(merged["seller_verification_enabled"])
        for item in merged["sections"]["other"]["items"] + merged["sections"]["gaming"]["items"]:
            self.assertEqual(item["verification_status"], "disabled")
            self.assertNotIn("seller_name", item)
            self.assertNotEqual(item.get("cex_status"), "matched")

    def test_unverified_cex_profit_can_send_research_alert(self):
        row = {
            "asin": "B000000001", "title": "Sony Headset",
            "url": "https://www.amazon.ie/dp/B000000001", "price_eur": 40,
            "cex_cash_eur": 100, "cex_cash_profit_eur": 45, "cex_retail_eur": 130,
            "cex_min_profit_eur": 10, "cex_max_buy_eur": 75,
            "cex_break_even_buy_eur": 90, "cex_net_roi_percent": 112.5,
            "cex_url": "https://ie.webuy.com/product-detail?id=123",
            "cex_quote_fresh_for_alert": True,
            "cex_quote_checked_at": "2026-11-02T20:00:00+00:00",
            "match_confidence": "title_only",
        }

        async def check():
            with patch("app.services.bestsellers.send_notification", new_callable=AsyncMock) as send:
                send.return_value = (True, False)
                count = await send_cex_alerts(
                    [row], CrawlConfig(roots=[]),
                    dashboard_url="https://example.invalid", deep_drop=False,
                )
                self.assertEqual(count, 1)
                self.assertIn("NOT checked", send.await_args.args[0])
        asyncio.run(check())

    def test_deferred_cex_research_stays_in_queue(self):
        section = make_section_catalogue(
            listings(), None, section_id="gaming", section_name="Gaming"
        )
        batch = select_check_batch(section, limit=1)
        apply_batch_results(section, batch, [], {}, [], cex_enabled=True)
        self.assertEqual(catalogue_progress(section)["remaining"], 1)
        self.assertEqual(len(select_check_batch(section, limit=1)), 1)

    def test_completed_cex_no_match_advances_queue(self):
        section = make_section_catalogue(
            listings(), None, section_id="gaming", section_name="Gaming"
        )
        batch = select_check_batch(section, limit=1)
        apply_batch_results(section, batch, [], {}, [], cex_enabled=True,
                            quote_cache={"B000000001": {
                                "status": "no_match", "checked_at": "2026-11-02T20:00:00+00:00"
                            }})
        self.assertEqual(catalogue_progress(section)["remaining"], 0)
        self.assertEqual(section["items"][0]["verification_status"], "disabled")


if __name__ == "__main__":
    unittest.main()
