import asyncio
import copy
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from pathlib import Path
from types import SimpleNamespace

import httpx

from app.scraper.cex_ie import CexPrices, search_boxes
from app.scraper.amazon_ie import ProductSnapshot
from app.services.alert_policy import AlertPolicy, qualifies_cex_alert
from app.services.bestsellers import CrawlConfig
from app.services.catalogue import merge_catalogue
from app.services.cex_arbitrage import enrich_with_cex
from app.services.cex_cache import quote_entry
from app.services.cex_targets import (
    TargetConfig, target_state, sync_mappings, discover_targets, attach_target_quotes,
    refresh_mapped_quotes, watch_rows, exact_target_match, prune_targets,
)
from app.services.discovery_checks import choose_detail_checks
from scripts.scan_hotlist import recheck, select_fast_checks

NOW = datetime(2026, 11, 3, 20, tzinfo=timezone.utc)
TITLE = 'Sony WH-1000XM5 Headphones'
PRICE = CexPrices('cex-test', TITLE + ', B', 'Headphones', 300, 320, 350,
                  'https://ie.webuy.com/product-detail?id=cex-test', model_number='wh1000xm5')
OPTIONS = TargetConfig(enabled=True, queries=('Sony WH', 'Canon EOS', 'Nvidia RTX'))


def config(**kw):
    return CrawlConfig(roots=[], request_delay_seconds=0, cex_request_delay_seconds=0,
                       cex_live_lookups_per_run=10, cex_extra_cost_eur=5,
                       cex_payout_haircut_percent=10, **kw)


def item(asin='B000000001', *, exact=True, price=350, when=NOW):
    row = {'asin': asin, 'title': TITLE, 'price_eur': price,
           'url': f'https://www.amazon.ie/dp/{asin}', 'active': True,
           'last_seen_at': when.isoformat(), 'cex_quote_cache': quote_entry(TITLE, PRICE, when)}
    if exact:
        row.update(model_number='wh1000xm5', identity_checked_at=when.isoformat(), identity_status='confirmed')
    return row


def catalogue(rows=None, *, now=NOW):
    state = target_state({})
    sync_mappings(state, [{'items': rows or [item()]}], OPTIONS, now=now)
    return {'cex_targets': state}


def snapshot(asin='B000000001', price=180, model='wh1000xm5'):
    return ProductSnapshot(asin, f'https://www.amazon.ie/dp/{asin}', TITLE, price, None,
                           'EUR', None, model_number=model)


class TestCexTargetCatalogue(unittest.TestCase):
    def test_section_to_published_map_to_fast_first_drop(self):
        from app.scraper.bestsellers import BestsellerItem
        from app.services.bestsellers import CrawlResult, ProcessResult
        from scripts.crawl_bestsellers import crawl_section
        card = BestsellerItem('B000000001', TITLE, 350, 1, item()['url'])
        result = CrawlResult(items={card.asin: (card, 'Headphones')})
        hit = {'boxId': PRICE.box_id, 'boxName': PRICE.box_name, 'cashBuyPrice': 300,
               'boxWebBuyAllowed': True, 'modelNumber': 'WH-1000XM5'}
        async def check_details(client, rows, *, snapshots, **kwargs):
            self.assertLessEqual(len(rows) + 1, 6)
            snapshots[card.asin] = snapshot(price=350)
            kwargs['diagnostics'][card.asin] = 'price_read'
            return []
        async def go():
            with patch('scripts.crawl_bestsellers.crawl', new_callable=AsyncMock, return_value=result), \
                 patch('scripts.crawl_bestsellers.process_crawl', return_value=ProcessResult()), \
                 patch('scripts.crawl_bestsellers.import_target_seed'), \
                 patch('scripts.crawl_bestsellers.discover_amazon_targets', new_callable=AsyncMock, return_value=([], {'search_requests': 1, 'candidates': 0, 'status': 'no_new_candidates'})), \
                 patch('scripts.crawl_bestsellers.spot_check_list_prices', side_effect=check_details), \
                 patch('scripts.crawl_bestsellers.export_deals', return_value={}), \
                 patch('scripts.crawl_bestsellers.db.recent_alerts', return_value=[]), \
                 patch('app.scraper.cex_ie.search_boxes', new_callable=AsyncMock, return_value=[hit]), \
                 patch('app.scraper.cex_ie.fetch_box_prices', new_callable=AsyncMock, return_value=PRICE):
                _, fragment = await crawl_section(config(), section_id='cex_priority', section_name='CeX',
                    args=SimpleNamespace(config=Path('bestsellers.json')), notify=False, published_catalogue={})
            published = merge_catalogue({}, fragment)
            self.assertIn(card.asin, published['cex_targets']['mappings'])
            with patch('scripts.scan_hotlist.fetch_product', new_callable=AsyncMock, return_value=snapshot()), \
                 patch('scripts.scan_hotlist.send_notification', new_callable=AsyncMock, return_value=(True, False)) as notify:
                fast = await recheck({}, {}, catalogue=published, config=config(), notify=True)
                notify.assert_awaited_once()
                self.assertEqual(fast['funnel']['profitable'], 1)
        asyncio.run(go())

    def test_explicit_brand_exclusion_wins_over_exact_mapping(self):
        cat = catalogue()
        with patch('app.services.cex_targets.relevance', return_value=(False, 'explicitly_excluded_brand', 'Sony')):
            self.assertEqual(watch_rows(cat['cex_targets'], config(), now=NOW), [])
            sync_mappings(cat['cex_targets'], [{'items':[item()]}], OPTIONS, now=NOW)
            self.assertEqual(cat['cex_targets']['mappings'], {})

    def test_delayed_other_section_cannot_rollback_shared_target_state(self):
        newer = catalogue()
        old_state = target_state({})
        old_state['generated_at'] = (NOW-timedelta(hours=1)).isoformat()
        fragment={'section_id':'home','generated_at':(NOW-timedelta(hours=1)).isoformat(), 'items':[], 'cex_targets':old_state}
        merged=merge_catalogue(newer,fragment)
        self.assertEqual(merged['cex_targets'], newer['cex_targets'])

    def test_old_section_cannot_erase_a_failed_quote_refresh_backoff(self):
        cat = catalogue()
        state = cat['cex_targets']
        state['mappings']['B000000001']['cex_quote_cache']['last_error_at']=(NOW+timedelta(minutes=1)).isoformat()
        row = item()
        row['last_seen_at']=(NOW+timedelta(minutes=2)).isoformat()
        sync_mappings(state, [{'items':[row]}], OPTIONS, now=NOW+timedelta(minutes=2))
        self.assertIn('last_error_at', state['mappings'][row['asin']]['cex_quote_cache'])
        attach_target_quotes([row], state)
        self.assertIn('last_error_at', row['cex_quote_cache'])

    def test_search_filters_cash_and_pages_without_changing_default_search(self):
        bodies = []
        async def go():
            def handler(request):
                import json
                bodies.append(json.loads(request.content))
                return httpx.Response(200, json={'hits': []})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await search_boxes(client, 'Sony', hits_per_page=25, min_cash_eur=150, page=2, strict=True)
                await search_boxes(client, 'Sony', strict=True)
        asyncio.run(go())
        self.assertEqual(bodies[0]['page'], 2)
        self.assertIn(['cashBuyPrice>150', 'cashPriceCalculated>150'], bodies[0]['numericFilters'])
        self.assertNotIn('page', bodies[1])
        self.assertEqual(bodies[1]['hitsPerPage'], 3)

    def test_discovery_uses_shared_budget_rejects_non_buyable_and_persists_cursor(self):
        state = target_state({})
        run = {'live_lookups': 9}
        hits = [{'boxId': 'valid', 'boxName': TITLE, 'cashBuyPrice': 300, 'boxWebBuyAllowed': True},
                {'boxId': 'disabled', 'boxName': TITLE, 'cashBuyPrice': 300, 'boxWebBuyAllowed': 0},
                {'boxId': 'cheap', 'boxName': TITLE, 'cashBuyPrice': 9.99, 'boxWebBuyAllowed': 1},
                {'boxId': 'boundary', 'boxName': TITLE, 'cashBuyPrice': 10, 'boxWebBuyAllowed': 1}]
        async def go():
            with patch('app.scraper.cex_ie.search_boxes', new_callable=AsyncMock, return_value=hits) as search:
                await discover_targets(state, OPTIONS, config(), object(), run, now=NOW)
                self.assertEqual(search.await_count, 1)
                self.assertEqual(run['live_lookups'], 10)
                self.assertEqual(list(state['items']), ['valid'])
                run.clear()
                await discover_targets(state, OPTIONS, config(), object(), run, now=NOW)
                self.assertEqual(search.await_count, 3)  # remaining two terms, not the cached first
                await discover_targets(state, OPTIONS, config(), object(), {}, now=NOW)
                self.assertEqual(search.await_count, 3)
        asyncio.run(go())

    def test_above_ten_cash_is_admitted_even_when_purchase_is_under_one_hundred(self):
        from dataclasses import replace
        small = replace(PRICE, cash_eur=10.01)
        row = item(price=5)
        row['cex_quote_cache'] = quote_entry(TITLE, small, NOW)
        state = target_state({})
        sync_mappings(state, [{'items':[row]}], OPTIONS, now=NOW)
        self.assertIn(row['asin'], state['mappings'])
        watch = watch_rows(state, config(), now=NOW)
        self.assertEqual(len(watch), 1)
        self.assertFalse(qualifies_cex_alert(watch[0]))  # payout is not net profit
        selected = choose_detail_checks({'items':[dict(row, identity_status='identifiers_missing', cex_target_box_id=small.box_id)]},
                                        [], [], config=config(), policy=AlertPolicy())
        self.assertEqual(len(selected), 1)
        # A subsequent quote falling to the cutoff keeps the invalidation
        # record, but no longer contributes an eligible watch.
        state['mappings'][row['asin']]['cex_quote_cache'] = quote_entry(TITLE, replace(small, cash_eur=10), NOW)
        prune_targets(state, OPTIONS, NOW)
        self.assertIn(row['asin'], state['mappings'])
        self.assertEqual(watch_rows(state, config(), now=NOW), [])

    def test_floor_change_requeues_cached_queries_within_budget_and_keeps_error_backoff(self):
        state = target_state({})
        state.update(cash_floor_eur=150, queries={q:{'checked_at':NOW.isoformat()} for q in OPTIONS.queries})
        state['queries']['Canon EOS']['last_error_at']=NOW.isoformat()
        async def go():
            with patch('app.scraper.cex_ie.search_boxes', new_callable=AsyncMock, return_value=[]) as search:
                run={}
                await discover_targets(state, OPTIONS, config(), object(), run, now=NOW)
                self.assertEqual(search.await_count, 2)
                self.assertEqual(run['live_lookups'], 2)
                self.assertEqual([c.args[1] for c in search.await_args_list], ['Sony WH','Nvidia RTX'])
                self.assertEqual(state['cash_floor_eur'], 10)
        asyncio.run(go())

    def test_low_cash_volume_cannot_evict_all_high_value_targets_or_mappings(self):
        from dataclasses import replace
        options=replace(OPTIONS,max_items=6,max_mappings=6)
        state=target_state({})
        for n in range(12):
            quote=replace(PRICE,box_id=str(n),cash_eur=300 if n<4 else 30)
            when=NOW-timedelta(minutes=10 if n<4 else 1)
            state['items'][str(n)]=quote_entry(TITLE,quote,when)
            row=item(str(n),when=when)
            row['cex_quote_cache']=quote_entry(TITLE,quote,when)
            state['mappings'][str(n)]=row
        prune_targets(state,options,NOW)
        for field in ('items','mappings'):
            self.assertEqual(len(state[field]),6)
            self.assertTrue({'0','1','2','3'} <= set(state[field]))

    def test_catalogue_rate_limit_stops_section_and_retries_after_backoff(self):
        state = target_state({})
        run = {}
        error = httpx.HTTPStatusError('blocked', request=httpx.Request('POST', 'https://example.com'), response=httpx.Response(429))
        async def go():
            with patch('app.scraper.cex_ie.search_boxes', new_callable=AsyncMock, side_effect=error) as search:
                await discover_targets(state, OPTIONS, config(), object(), run, now=NOW)
                self.assertEqual(search.await_count, 1)
                self.assertTrue(run['rate_limited'])
                self.assertIn('last_error_at', state['queries']['Sony WH'])
        asyncio.run(go())

    def test_index_quote_cannot_alert_when_detail_budget_is_exhausted(self):
        row = item(price=180)
        state = target_state({})
        record = quote_entry(TITLE, PRICE, NOW)
        record['source'] = 'catalogue_search'
        state['items']['cex-test'] = record
        row.pop('cex_quote_cache')
        attach_target_quotes([row], state)
        self.assertEqual(row['cex_target_box_id'], 'cex-test')
        quotes = {row['asin']: row['cex_quote_cache']}
        async def go():
            with patch('app.scraper.cex_ie.fetch_box_prices', new_callable=AsyncMock, return_value=PRICE) as detail:
                rows, flips = await enrich_with_cex([row], config(), client=object(), cache=quotes,
                                                  run_state={'live_lookups': 10}, now=NOW)
                self.assertFalse(rows[0]['cex_quote_fresh_for_alert'])
                self.assertFalse(qualifies_cex_alert(rows[0]))
                self.assertEqual(flips, [])
                detail.assert_not_awaited()
                rows, flips = await enrich_with_cex([row], config(), client=object(), cache=quotes, run_state={}, now=NOW)
                detail.assert_awaited_once()
                self.assertTrue(rows[0]['cex_quote_fresh_for_alert'])
                self.assertNotIn('source', quotes[row['asin']])
        asyncio.run(go())

    def test_non_profitable_exact_mapping_survives_publication_without_copying_shared_state(self):
        cat = catalogue()
        self.assertEqual(len(cat['cex_targets']['mappings']), 1)
        rows = watch_rows(cat['cex_targets'], config(), now=NOW)
        self.assertFalse(rows[0]['cex_cash_flip'])
        self.assertEqual(rows[0]['cex_max_buy_eur'], 212)
        fragment = {'section_id': 'pc', 'generated_at': NOW.isoformat(), 'items': [item()], **cat}
        published = merge_catalogue({}, fragment)
        self.assertEqual(len(published['cex_targets']['mappings']), 1)
        self.assertNotIn('cex_targets', published['sections']['pc'])
        next_fragment = {'section_id': 'home', 'generated_at': NOW.isoformat(), 'items': []}
        self.assertEqual(merge_catalogue(published, next_fragment)['cex_targets'], cat['cex_targets'])

    def test_title_only_index_and_wrong_model_never_become_exact_maps(self):
        state = target_state({})
        rows = [item('A', exact=False), item('B'), item('C')]
        rows[1]['model_number'] = 'wh1000xm4'
        rows[2]['cex_quote_cache']['source'] = 'catalogue_search'
        sync_mappings(state, [{'items': rows}], OPTIONS, now=NOW)
        self.assertEqual(state['mappings'], {})

    def test_model_only_does_not_prove_colour_or_network_and_barcode_conflicts_fail(self):
        black = CexPrices('b', TITLE + ' Black, B', 'Headphones', 300, 320, 350, PRICE.url, model_number='wh1000xm5')
        self.assertFalse(exact_target_match(item(), black))
        self.assertTrue(exact_target_match(dict(item(), title=TITLE + ' Black'), black))
        self.assertFalse(exact_target_match(dict(item(), title=TITLE + ' Silver'), black))
        self.assertFalse(exact_target_match(dict(item(), title=TITLE + ' replacement ear pads'), PRICE))

    def test_mapping_removed_when_amazon_title_changes_and_expired_mapping_pruned(self):
        cat = catalogue()
        changed = dict(item(), title='Sony replacement ear pads', last_seen_at=(NOW+timedelta(minutes=1)).isoformat())
        sync_mappings(cat['cex_targets'], [{'items': [changed]}], OPTIONS, now=NOW+timedelta(minutes=1))
        self.assertEqual(cat['cex_targets']['mappings'], {})
        old = item(when=NOW-timedelta(days=31))
        sync_mappings(cat['cex_targets'], [{'items': [old]}], OPTIONS, now=NOW)
        self.assertEqual(cat['cex_targets']['mappings'], {})

    def test_known_quote_refresh_spends_existing_slots_and_never_searches(self):
        cat = catalogue([item(str(i), when=NOW-timedelta(hours=7)) for i in range(4)])
        run = {}
        async def go():
            with patch('app.scraper.cex_ie.fetch_box_prices', new_callable=AsyncMock, return_value=PRICE) as detail, patch('app.scraper.cex_ie.search_boxes', new_callable=AsyncMock) as search:
                await refresh_mapped_quotes(cat['cex_targets'], OPTIONS, config(), object(), run, now=NOW)
                self.assertEqual(detail.await_count, 1)  # same box is shared by four ASINs
                self.assertEqual(run['live_lookups'], 1)
                search.assert_not_awaited()
        asyncio.run(go())

    def test_unprofitable_targets_use_half_existing_detail_budget(self):
        rows = [dict(item(str(i), exact=False), cex_target_box_id='cex-test') for i in range(10)]
        selected = choose_detail_checks({'items': rows}, [], [], config=config(), policy=AlertPolicy())
        self.assertEqual(len(selected), 6)
        # No history or positive profit is needed to collect identity evidence.
        self.assertEqual(len({r['asin'] for r in selected}), 6)


class TestCexFastChecks(unittest.TestCase):
    def run_check(self, cat=None, previous=None, snap=None, *, notify=False, now=NOW):
        async def go():
            with patch('scripts.scan_hotlist.fetch_product', new_callable=AsyncMock, return_value=snap or snapshot()) as fetch, patch('scripts.scan_hotlist.send_notification', new_callable=AsyncMock, return_value=(True, False)) as send:
                result = await recheck({}, previous or {}, catalogue=cat or catalogue(), config=config(), notify=notify, now=now)
                return result, fetch.await_count, send.await_count
        return asyncio.run(go())

    def test_catches_profitable_first_drop_without_history_and_uses_no_cex_calls(self):
        with patch('app.scraper.cex_ie.fetch_box_prices', new_callable=AsyncMock) as cex:
            out, fetched, sent = self.run_check(notify=True)
        cex.assert_not_awaited()
        self.assertEqual((fetched, sent), (1, 1))
        row = out['products'][0]
        self.assertEqual(row['status'], 'profitable')
        self.assertEqual(row['cex_cash_profit_eur'], 85)
        self.assertEqual(row['cex_max_buy_eur'], 212)
        self.assertEqual(row['found_at'], NOW.isoformat())
        self.assertEqual(row['last_cex_alert_price_eur'], 180)

    def test_wrong_model_missing_identifiers_wrong_currency_and_asin_never_alert(self):
        bad = [snapshot(model='wh1000xm4'), snapshot(model=None), snapshot(asin='B000000002')]
        foreign = snapshot(); foreign.currency='GBP'; bad.append(foreign)
        nan = snapshot(price=float('nan')); bad.append(nan)
        for snap in bad:
            with self.subTest(snap=snap):
                result, _, sent = self.run_check(snap=snap, notify=True)
                self.assertEqual(sent, 0)
                self.assertNotEqual(result['products'][0]['status'], 'profitable')

    def test_stale_or_failed_quote_does_not_consume_amazon_pages(self):
        for error in (False, True):
            cat = catalogue([item(when=NOW-timedelta(hours=13 if not error else 1))])
            if error:
                cat['cex_targets']['mappings']['B000000001']['cex_quote_cache']['last_error_at']=NOW.isoformat()
            result, fetched, sent = self.run_check(cat=cat, notify=True)
            self.assertEqual((fetched, sent), (0, 0))
            self.assertEqual(result['funnel']['stale_quotes'], 1)

    def test_alert_fingerprint_survives_failed_reads_and_unchanged_price_after_day(self):
        out, _, sent = self.run_check(notify=True)
        self.assertEqual(sent, 1)
        failed, _, sent = self.run_check(previous=out, snap=snapshot(price=None), notify=True)
        self.assertEqual(sent, 0)
        self.assertEqual(failed['products'][0]['last_cex_alert_price_eur'], 180)
        tomorrow = NOW+timedelta(days=1)
        cat = catalogue([item(when=tomorrow)], now=tomorrow)
        repeat, _, sent = self.run_check(cat=cat, previous=failed, notify=True, now=tomorrow)
        self.assertEqual(sent, 0)
        self.assertEqual(repeat['products'][0]['last_cex_alert_price_eur'], 180)
        self.assertEqual(repeat['products'][0]['found_at'], out['products'][0]['found_at'])
        deeper, _, sent = self.run_check(cat=cat, previous=repeat, snap=snapshot(price=150), notify=True, now=tomorrow)
        self.assertEqual(sent, 1)
        self.assertEqual(deeper['products'][0]['last_cex_alert_price_eur'], 150)

    def test_failed_notification_is_retried_and_old_broad_alert_is_respected(self):
        async def go():
            with patch('scripts.scan_hotlist.fetch_product', new_callable=AsyncMock, return_value=snapshot()), patch('scripts.scan_hotlist.send_notification', new_callable=AsyncMock, return_value=(False, False)):
                return await recheck({}, {}, catalogue=catalogue(), config=config(), notify=True, now=NOW)
        failed = asyncio.run(go())
        self.assertNotIn('last_cex_alert_price_eur', failed['products'][0])
        _, _, sent = self.run_check(previous=failed, notify=True)
        self.assertEqual(sent, 1)
        cat = catalogue()
        cat['cex_targets']['mappings']['B000000001']['last_cex_alert']={'price_eur':180,'sent_at':NOW.isoformat()}
        _, _, sent = self.run_check(cat=cat, notify=True)
        self.assertEqual(sent, 0)

    def test_block_stops_remaining_requests_and_cooldown_survives_next_tick(self):
        cat = catalogue([item(f'B{i:09d}') for i in range(20)])
        error = httpx.HTTPStatusError('blocked', request=httpx.Request('GET','https://www.amazon.ie'), response=httpx.Response(503))
        async def go():
            with patch('scripts.scan_hotlist.fetch_product', new_callable=AsyncMock, side_effect=error) as fetch:
                first = await recheck({}, {}, catalogue=cat, config=config(), notify=False, now=NOW)
                self.assertEqual(fetch.await_count, 1)
                self.assertEqual(first['scan_status'], 'provider_blocked')
                second = await recheck({}, first, catalogue=cat, config=config(), notify=False, now=NOW+timedelta(minutes=10))
                self.assertEqual(fetch.await_count, 1)
                self.assertEqual(second['checked_this_run'], 0)
                self.assertEqual(second['scan_status'], 'provider_cooldown')
        asyncio.run(go())

    def test_twelve_page_cap_fair_rotation_exploration_and_retained_unscheduled_state(self):
        cat = catalogue([item(f'B{i:09d}') for i in range(50)])
        mapped = watch_rows(cat['cex_targets'], config(), now=NOW)
        drops = [dict(item(f'D{i:09d}'), found_at=NOW.isoformat()) for i in range(20)]
        selected = select_fast_checks(drops, mapped, {}, limit=500)
        self.assertEqual(len(selected), 12)
        self.assertEqual(sum(r['scan_lane']=='cex_target' for r in selected), 8)
        prior = {r['asin']:{**r,'checked_at':NOW.isoformat()} for r in selected}
        again = select_fast_checks(drops, mapped, prior)
        self.assertFalse({r['asin'] for r in again} & set(prior))
        # Every target eventually reaches an exploration slot.
        seen=set()
        for n in range(30):
            batch=select_fast_checks(drops, mapped, prior)
            for r in batch:
                seen.add(r['asin'])
                prior[r['asin']]={'checked_at':(NOW+timedelta(minutes=n+1)).isoformat()}
        self.assertTrue({r['asin'] for r in mapped} <= seen)
        self.assertEqual(select_fast_checks(drops, mapped, {}, limit=0), [])

    def test_successful_unselected_fingerprints_are_not_discarded(self):
        previous={'products':[{'asin':'OTHER', 'last_cex_alert_price_eur':125,'checked_at':NOW.isoformat()}]}
        out, _, _ = self.run_check(previous=previous)
        self.assertIn('OTHER', [r['asin'] for r in out['products']])


if __name__ == '__main__':
    unittest.main()
