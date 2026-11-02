"""CeX quote cache, HTTP throttling and verified Amazon price alerts."""
import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from app import database as db
from app.config import settings
from app.scraper import cex_ie
from app.scraper.amazon_ie import ProductSnapshot
from app.scraper.cex_ie import CexPrices
from app.scraper.bestsellers import BestsellerItem
from app.services.bestsellers import CrawlConfig, CrawlResult
from app.services.catalogue import make_section_catalogue, apply_batch_results
from app.services.cex_arbitrage import enrich_with_cex
from app.services.cex_cache import cache_valid, cache_price, quote_entry, status_entry
from app.services.monitor import detect_verified_price
from app.services.price_history import observe_price_reference

NOW = datetime(2026, 11, 2, 20, tzinfo=timezone.utc)
TITLE = "SteelSeries Arctis 7P Wireless Gaming Headset"
PRICE = CexPrices(
    "cex1", "SteelSeries Arctis 7P Headset Black", "Accessories",
    165.0, 180.0, 220.0, "https://ie.webuy.com/product-detail?id=cex1",
)
ITEM = {
    "asin": "B000000001", "title": TITLE, "price_eur": 100.0,
    "url": "https://www.amazon.ie/dp/B000000001",
    "seller_verified": True, "seller_name": "Amazon", "seller_type": "amazon_retail",
}

def config(**kwargs):
    return CrawlConfig(
        roots=[], cex_request_delay_seconds=0,
        cex_live_lookups_per_run=10, **kwargs
    )

class TestCexCache(unittest.TestCase):
    def test_cached_quote_reprices_new_amazon_cost_without_request(self):
        previous = quote_entry(TITLE, PRICE, NOW - timedelta(hours=2))
        self.assertTrue(cache_valid(previous, title=TITLE, now=NOW))
        self.assertEqual(cache_price(previous).cash_eur, 165.0)
        cache = {ITEM["asin"]: previous}
        async def check():
            with (
                patch.object(cex_ie, "search_boxes", new_callable=AsyncMock) as search,
                patch.object(cex_ie, "fetch_box_prices", new_callable=AsyncMock) as detail,
            ):
                rows, flips = await enrich_with_cex(
                    [{**ITEM, "price_eur": 95.0}], config(),
                    client=object(), cache=cache, run_state={}, now=NOW,
                )
                search.assert_not_called()
                detail.assert_not_called()
                return rows, flips
        rows, flips = asyncio.run(check())
        self.assertEqual(len(flips), 1)
        self.assertEqual(rows[0]["cex_cash_profit_eur"], 70.0)
        self.assertEqual(rows[0]["cex_quote_source"], "cache")
        self.assertTrue(rows[0]["cex_quote_fresh_for_alert"])
        self.assertEqual(cache[ITEM["asin"]]["checked_at"], previous["checked_at"])

    def test_negative_quote_has_week_long_ttl(self):
        cache = {ITEM["asin"]: status_entry(TITLE, "no_match", NOW - timedelta(days=1))}
        state = {}
        async def check():
            with patch.object(cex_ie, "search_boxes", new_callable=AsyncMock) as search:
                rows, flips = await enrich_with_cex(
                    [ITEM], config(), client=object(), cache=cache,
                    run_state=state, now=NOW,
                )
                search.assert_not_called()
                return rows, flips
        rows, flips = asyncio.run(check())
        self.assertFalse(rows)
        self.assertFalse(flips)
        self.assertEqual(state["live_lookups"], 0)
        self.assertEqual(state["cache_hits"], 1)

    def test_cached_profitable_quote_refreshes_one_box_after_12h(self):
        cache = {ITEM["asin"]: quote_entry(TITLE, PRICE, NOW - timedelta(hours=18))}
        state = {}
        async def check():
            with (
                patch.object(cex_ie, "search_boxes", new_callable=AsyncMock) as search,
                patch.object(cex_ie, "fetch_box_prices", new_callable=AsyncMock) as detail,
            ):
                detail.return_value = PRICE
                rows, flips = await enrich_with_cex(
                    [ITEM], config(), client=object(), cache=cache,
                    run_state=state, now=NOW,
                )
                search.assert_not_called()
                detail.assert_awaited_once()
                self.assertEqual(detail.await_args.args[1], "cex1")
                return rows, flips
        rows, flips = asyncio.run(check())
        self.assertEqual(len(flips), 1)
        self.assertEqual(rows[0]["cex_quote_source"], "live")
        self.assertEqual(state["live_lookups"], 1)
        self.assertEqual(cache[ITEM["asin"]]["checked_at"], NOW.isoformat())

    def test_never_alert_on_stale_quote_when_budget_exhausted(self):
        cache = {ITEM["asin"]: quote_entry(TITLE, PRICE, NOW - timedelta(hours=20))}
        state = {"live_lookups": 10}
        async def check():
            with patch.object(cex_ie, "fetch_box_prices", new_callable=AsyncMock) as fetch:
                rows, flips = await enrich_with_cex(
                    [ITEM], config(), client=object(), cache=cache,
                    run_state=state, now=NOW,
                )
                fetch.assert_not_called()
                return rows, flips
        rows, flips = asyncio.run(check())
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["cex_quote_fresh_for_alert"])
        self.assertFalse(rows[0]["cex_cash_flip"])
        self.assertEqual(flips, [])

    def test_403_provider_block_pauses_future_queries(self):
        request = httpx.Request("POST", "https://search.webuy.io/1/indexes/test/query")
        response = httpx.Response(403, request=request)
        error = httpx.HTTPStatusError("Forbidden", request=request, response=response)
        state = {}
        async def check():
            with patch.object(cex_ie, "search_boxes", new_callable=AsyncMock) as search:
                search.side_effect = error
                return await enrich_with_cex(
                    [ITEM, {**ITEM, "asin": "B000000002"}], config(),
                    client=object(), cache={}, run_state=state, now=NOW,
                ), search.await_count
        (_, flips), count = asyncio.run(check())
        self.assertEqual(count, 1)
        self.assertFalse(flips)
        self.assertTrue(state["rate_limited"])

    def test_cex_http_429_stops_further_queries(self):
        other = {**ITEM, "asin": "B000000002", "title": TITLE}
        request = httpx.Request("POST", "https://search.webuy.io/1/indexes/test/query")
        response = httpx.Response(429, request=request)
        error = httpx.HTTPStatusError("Rate limited", request=request, response=response)
        cache, state = {}, {}
        async def check():
            with patch.object(cex_ie, "search_boxes", new_callable=AsyncMock) as search:
                search.side_effect = error
                answer = await enrich_with_cex(
                    [ITEM, other], config(), client=object(),
                    cache=cache, run_state=state, now=NOW,
                )
                return answer, search.await_count
        (rows, flips), count = asyncio.run(check())
        self.assertEqual((rows, flips), ([], []))
        self.assertEqual(count, 1)
        self.assertTrue(state["rate_limited"])
        self.assertEqual(state["live_lookups"], 1)
        self.assertEqual(cache[ITEM["asin"]]["status"], "error")
        self.assertNotIn(other["asin"], cache)

    def test_price_change_keeps_cex_quote_but_not_old_seller_proof(self):
        def crawl(price, title=TITLE):
            return CrawlResult(items={"B000000001": (
                BestsellerItem("B000000001", title, price, 1, ITEM["url"]), "Gaming"
            )})
        original = make_section_catalogue(
            crawl(100), None, section_id="gaming", section_name="Gaming",
            observed_at=NOW.isoformat(),
        )
        original["items"][0].update(
            cex_quote_cache=quote_entry(TITLE, PRICE, NOW),
            verification_status="verified", seller_name="Amazon",
            last_checked_at=NOW.isoformat(),
        )
        changed = make_section_catalogue(
            crawl(50), original, section_id="gaming", section_name="Gaming",
            observed_at=(NOW + timedelta(hours=1)).isoformat(),
        )
        row = changed["items"][0]
        self.assertIn("cex_quote_cache", row)
        self.assertIsNone(row["last_checked_at"])
        self.assertNotIn("seller_name", row)
        other = make_section_catalogue(
            crawl(50, "Another product revision"), changed,
            section_id="gaming", section_name="Gaming",
            observed_at=(NOW + timedelta(hours=2)).isoformat(),
        )
        self.assertNotIn("cex_quote_cache", other["items"][0])

    def test_persists_actual_quote_date_not_amazon_check_date(self):
        prev = quote_entry(TITLE, PRICE, NOW - timedelta(hours=5))
        rows = make_section_catalogue(
            CrawlResult(items={"B000000001": (
                BestsellerItem("B000000001", TITLE, 100, 1, ITEM["url"]), "Gaming"
            )}), None, section_id="gaming", section_name="Gaming",
        )
        apply_batch_results(
            rows, [{**ITEM}], [{**ITEM, "seller_type": "amazon_retail"}], {"B000000001": "verified"},
            [{**ITEM, "cex_title": PRICE.box_name, "cex_url": PRICE.url,
              "cex_cash_eur": PRICE.cash_eur, "cex_cash_profit_eur": 65,
              "cex_quote_checked_at": prev["checked_at"], "cex_quote_source": "cache",
              "cex_quote_fresh_for_alert": True}],
            cex_enabled=True, checked_at=NOW.isoformat(),
            quote_cache={"B000000001": prev},
        )
        row = rows["items"][0]
        self.assertEqual(row["cex_checked_at"], prev["checked_at"])
        self.assertEqual(row["cex_quote_cache"]["status"], "matched")


class TestVerifiedAlerts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.original = settings.database_path
        settings.database_path = Path(self.temp.name) / "alerts.db"
        db.init_db()

    def tearDown(self):
        settings.database_path = self.original
        self.temp.cleanup()

    def test_deep_bestseller_drop_issues_one_alert_after_verification(self):
        product_id = db.add_product("B000000001", ITEM["url"], TITLE)
        for price in (200.0, 200.0, 200.0):
            observe_price_reference(product_id, price)
            db.record_price_check(product_id, price, None, "EUR")
        observe_price_reference(product_id, 20.0)
        db.record_price_check(product_id, 20.0, None, "EUR")
        history_count = len(db.price_history(product_id, limit=100))
        snap = ProductSnapshot(
            asin="B000000001", url=ITEM["url"], title=TITLE,
            price_eur=20.0, list_price_eur=None, currency="EUR",
            availability="In stock", seller_name="Amazon", seller_status="amazon",
        )
        alert = detect_verified_price(product_id, snap, known_title=TITLE)
        self.assertIsNotNone(alert)
        self.assertEqual(len(db.price_history(product_id, limit=100)), history_count)
        retry = detect_verified_price(product_id, snap, known_title=TITLE)
        self.assertIsNotNone(retry)
        self.assertEqual(retry.alert_id, alert.alert_id, "Retry an unsent alert instead of creating duplicate")
        self.assertEqual(len(db.recent_alerts()), 1)
        db.mark_alert_notified(alert.alert_id, telegram=True)
        self.assertIsNone(detect_verified_price(product_id, snap, known_title=TITLE))

    def test_unknown_seller_is_not_alertable(self):
        product_id = db.add_product("B000000001", ITEM["url"], TITLE)
        db.save_price_baseline(
            product_id, 200, confirmed_at=NOW.isoformat(), last_seen_at=NOW.isoformat(),
        )
        snap = ProductSnapshot(
            asin="B000000001", url=ITEM["url"], title=TITLE,
            price_eur=20.0, list_price_eur=None, currency="EUR",
            availability=None, seller_name=None, seller_status="unknown",
        )
        self.assertIsNone(detect_verified_price(product_id, snap, known_title=TITLE))


if __name__ == "__main__":
    unittest.main()
