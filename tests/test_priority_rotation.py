import argparse
import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.bestseller_sections import load_sections, next_priority_section
from app.services.bestsellers import CategoryRoot, CrawlConfig, CrawlResult, crawl
from app.services.catalogue import reuse_cex_metadata
from scripts.crawl_bestsellers import crawl_section, resolve_section_id
from test_bestsellers import FakeAmazon, item_html, page_html


class TestPriorityRotation(unittest.TestCase):
    def test_overlapping_sections_reuse_fresh_quotes_and_delivered_alerts(self):
        quote = {'checked_at': '2026-11-03T12:00:00Z', 'title': 'RTX 5090'}
        alert = {'sent_at': '2026-11-03T12:01:00Z', 'price_eur': 500}
        published = {'sections': {'pc_hardware': {'items': [
            {'asin': 'A', 'title': 'RTX 5090', 'cex_quote_cache': quote, 'last_cex_alert': alert},
            {'asin': 'B', 'title': 'Different model', 'cex_quote_cache': quote},
            {'asin': 'C', 'title': 'GPU', 'cex_quote_cache': {'checked_at': 'invalid'}},
        ]}}}
        section = {'items': [{'asin': 'A', 'title': 'RTX 5090'}, {'asin': 'B', 'title': 'GPU'}, {'asin': 'C', 'title': 'GPU'}]}
        reuse_cex_metadata(section, published)
        self.assertEqual(section['items'][0]['cex_quote_cache'], quote)
        self.assertEqual(section['items'][0]['last_cex_alert'], alert)
        self.assertNotIn('cex_quote_cache', section['items'][1])
        self.assertNotIn('cex_quote_cache', section['items'][2])
        section['items'][0]['cex_quote_cache']['checked_at'] = '2026-11-03T13:00:00Z'
        reuse_cex_metadata(section, published)
        self.assertEqual(section['items'][0]['cex_quote_cache']['checked_at'], '2026-11-03T13:00:00Z')
        self.assertEqual(quote['checked_at'], '2026-11-03T12:00:00Z')

    def test_complete_rotation_and_restart_use_published_checkpoints(self):
        sections = load_sections(Path('bestsellers.json'))
        snapshot = {'sections': {}}
        expected = []
        for secondary in ('coffee', 'home', 'lifestyle', 'general'):
            expected.extend(('videogames', 'pc_hardware', 'cex_priority', secondary))
        now = datetime(2026, 11, 3, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'deals.json'
            args = argparse.Namespace(section='rotate', merge_previous=path, run_number=999)
            for i, section_id in enumerate(expected):
                path.write_text(json.dumps(snapshot))
                self.assertEqual(resolve_section_id(args, sections), section_id)
                # A failed unpublished attempt must select exactly the same turn.
                self.assertEqual(resolve_section_id(args, load_sections(Path('bestsellers.json'))), section_id)
                snapshot['sections'][section_id] = {'generated_at': (now + timedelta(minutes=i)).isoformat()}
                snapshot['last_section_id'] = section_id
            self.assertEqual(next_priority_section(sections, snapshot), 'videogames')

    def test_migration_and_invalid_timestamps_start_missing_priorities(self):
        sections = load_sections(Path('bestsellers.json'))
        old = {'last_section_id': 'gaming', 'sections': {
            'gaming': {'generated_at': '2026-11-03T12:00:00Z'},
            'home': {'generated_at': '2026-11-03T12:10:00Z'},
            'videogames': {'generated_at': 'invalid'},
        }}
        self.assertEqual(next_priority_section(sections, old), 'videogames')

    def test_budget_is_reallocated_without_larger_per_round_ceilings(self):
        sections = load_sections(Path('bestsellers.json'))
        primary = [s for s in sections if s.priority_rotation]
        secondary = [s for s in sections if not s.priority_rotation]
        for s in secondary:
            round_sections = primary + [s]
            self.assertLessEqual(sum(x.max_requests for x in round_sections), 820)
            self.assertLessEqual(sum(x.cex_live_lookups_per_run for x in round_sections), 40)
            self.assertTrue(all(x.catalogue_time_budget_seconds <= 360 for x in round_sections))

    def test_all_roots_precede_deep_branches_with_fixed_request_cap(self):
        fake = FakeAmazon({
            '/gp/bestsellers/videogames': page_html('Games', [item_html('B000000001', 1, 'Game', '€100')], children=['/gp/bestsellers/videogames/1']),
            '/gp/bestsellers/electronics': page_html('Tech', [item_html('B000000002', 1, 'Phone', '€500')]),
        })
        cfg = CrawlConfig(roots=[CategoryRoot('/gp/bestsellers/videogames', depth=2), CategoryRoot('/gp/bestsellers/electronics', depth=2)], pages_per_category=1, max_requests=2, request_delay_seconds=0)
        result = asyncio.run(crawl(fake, cfg))
        self.assertEqual(fake.requested, ['/gp/bestsellers/videogames', '/gp/bestsellers/electronics'])
        self.assertEqual(result.requests, 2)
        self.assertEqual(len(result.items), 2)

    def test_blocked_scan_cannot_publish_or_research_cex(self):
        async def check():
            with patch('scripts.crawl_bestsellers.crawl', new_callable=AsyncMock, return_value=CrawlResult(blocked=True)), patch('scripts.crawl_bestsellers.process_crawl') as process:
                with self.assertRaisesRegex(RuntimeError, 'preserve published checkpoint'):
                    await crawl_section(CrawlConfig(roots=[]), section_id='videogames', section_name='Games', args=argparse.Namespace(), notify=False)
                process.assert_not_called()
        asyncio.run(check())
