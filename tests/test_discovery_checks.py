import asyncio
import unittest
import httpx
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from app.scraper.amazon_ie import ProductSnapshot
from app.scraper.cex_ie import CexPrices
from app.services.alert_policy import AlertPolicy, qualifies_cex_alert
from app.services.bestsellers import CrawlConfig, passes_signal_gates
from app.scraper.bestsellers import BestsellerItem
from app.services.cex_arbitrage import build_cex_row
from app.services.cex_cache import quote_entry
from app.services.discovery_checks import choose_detail_checks, apply_detail_checks
from app.services.premium_drops import spot_check_list_prices
from app.services.catalogue import make_section_catalogue
from app.services.bestsellers import CrawlResult

TITLE = 'SteelSeries Arctis 7P Wireless Gaming Headset'


class TestDiscoveryChecks(unittest.TestCase):
    def fixture(self):
        item = {'asin': 'B000000001', 'title': TITLE, 'price_eur': 130, 'url': 'https://www.amazon.ie/dp/B000000001', 'active': True}
        prices = CexPrices('box', TITLE, 'Headsets', 250, 280, 300, 'https://ie.webuy.com', model_number='arctis7p')
        lookup = build_cex_row(item, prices, min_profit_eur=5, min_profit_percent=10, min_roi_percent=15, extra_cost_eur=5, payout_haircut_percent=10)
        lookup.update(match_confidence='title_only', cex_quote_fresh_for_alert=True)
        cache = {item['asin']: quote_entry(TITLE, prices, datetime.now(timezone.utc))}
        snap = ProductSnapshot(item['asin'], item['url'], TITLE, 130, None, 'EUR', None, model_number='arctis7p')
        return item, lookup, cache, snap

    def test_existing_page_budget_enables_exact_cex_alert_without_more_cex_calls(self):
        item, lookup, cache, snap = self.fixture()
        section = {'items': [item]}
        self.assertFalse(qualifies_cex_alert(lookup))
        selected = choose_detail_checks(section, [lookup], [], config=CrawlConfig(roots=[]), policy=AlertPolicy())
        counts = apply_detail_checks(section, selected, {snap.asin: snap}, [lookup], cache)
        self.assertEqual(counts['identity_confirmed'], 1)
        self.assertTrue(qualifies_cex_alert(lookup))
        self.assertEqual(item['identity_status'], 'confirmed')

    def test_changed_offer_and_wrong_model_cannot_be_promoted(self):
        for field, value, status in [('price_eur', 140, 'price_changed'), ('model_number', 'arctis7x', 'identity_mismatch')]:
            item, lookup, cache, snap = self.fixture()
            setattr(snap, field, value)
            apply_detail_checks({'items': [item]}, [item], {snap.asin: snap}, [lookup], cache)
            self.assertFalse(qualifies_cex_alert(lookup))
            self.assertFalse(lookup['cex_cash_flip'])
            self.assertEqual(item['identity_status'], status)

    def test_missing_identifiers_do_not_lower_exact_match_requirement(self):
        item, lookup, cache, snap = self.fixture()
        snap.model_number = None
        counts = apply_detail_checks({'items': [item]}, [item], {snap.asin: snap}, [lookup], cache)
        self.assertEqual(counts['identifiers_missing'], 1)
        self.assertFalse(qualifies_cex_alert(lookup))

    def test_cold_start_research_without_price_history_is_bounded_and_rotates(self):
        items = [{'asin': str(i), 'title': 'PC hardware', 'price_eur': 500 + i, 'active': True} for i in range(10)]
        items[-1]['identity_checked_at'] = '2026-11-03T12:00:00Z'
        cfg = CrawlConfig(roots=[], premium_spot_check_limit=3)
        rows = choose_detail_checks({'items': items}, [], [], config=cfg, policy=AlertPolicy())
        self.assertEqual(len(rows), 3)
        self.assertNotIn(items[-1], rows)
        cfg.signal_min_review_count = 100
        self.assertEqual(choose_detail_checks({'items': items}, [], [], config=cfg, policy=AlertPolicy()), [])

    def test_detail_fetches_share_limit_and_return_identifier_snapshots(self):
        item, _, _, snap = self.fixture()
        rows = [dict(item, asin=f'B{i:09d}', url=f'https://www.amazon.ie/dp/B{i:09d}') for i in range(10)]
        snapshots = {}
        async def check():
            with patch('app.scraper.amazon_ie.fetch_product', new_callable=AsyncMock, return_value=snap) as fetch:
                await spot_check_list_prices(object(), rows, config=CrawlConfig(roots=[], premium_spot_check_limit=2), allow_alerts=False, delay_seconds=0, snapshots=snapshots)
                self.assertEqual(fetch.await_count, 2)
        asyncio.run(check())
        self.assertEqual(len(snapshots), 2)

    def test_provider_block_stops_shared_detail_budget_without_retries(self):
        item, _, _, _ = self.fixture()
        error = httpx.HTTPStatusError('blocked', request=httpx.Request('GET', item['url']), response=httpx.Response(429))
        async def check():
            with patch('app.scraper.amazon_ie.fetch_product', new_callable=AsyncMock, side_effect=error) as fetch:
                await spot_check_list_prices(object(), [item, dict(item, asin='B000000002')], config=CrawlConfig(roots=[]), allow_alerts=False, delay_seconds=0)
                self.assertEqual(fetch.await_count, 1)
        asyncio.run(check())

    def test_coffee_history_can_keep_expensive_error_visible_below_current_floor(self):
        item = BestsellerItem('A', 'Coffee machine', 85, 1, 'https://example.com', review_count=200)
        cfg = CrawlConfig(roots=[], signal_min_price_eur=100, signal_min_review_count=100)
        self.assertFalse(passes_signal_gates(item, cfg))
        self.assertTrue(passes_signal_gates(item, cfg, reference_eur=500))
        item.review_count = None
        self.assertFalse(passes_signal_gates(item, cfg, reference_eur=500))

    def test_identity_survives_price_change_but_not_title_change(self):
        item, _, _, _ = self.fixture()
        item.update(model_number='arctis7p', identity_checked_at='2026-11-03T12:00:00Z')
        result = CrawlResult(items={item['asin']: (BestsellerItem(item['asin'], TITLE, 120, 1, item['url']), 'Headsets')})
        section = make_section_catalogue(result, {'items': [item]}, section_id='pc_hardware', section_name='PC')
        self.assertEqual(section['items'][0]['model_number'], 'arctis7p')
        result.items[item['asin']][0].title = 'Different hardware'
        section = make_section_catalogue(result, {'items': [item]}, section_id='pc_hardware', section_name='PC')
        self.assertNotIn('model_number', section['items'][0])
