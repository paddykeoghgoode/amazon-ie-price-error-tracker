"""Real public response excerpts plus adversarial variants of those layouts."""
import asyncio
import json
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx

from app.scraper.amazon_ie import parse_product_html, fetch_product, AmazonBlockedError
from app.scraper.cex_ie import fetch_box_prices, titles_match_for_trade_in, search_cash_price
from app.services.cex_targets import target_state, discover_targets, TargetConfig
from app.services.bestsellers import CrawlConfig
from app.services.premium_drops import spot_check_list_prices

FIXTURES = Path('tests/fixtures')


class LiveResponseRegressionTests(unittest.TestCase):
    def test_real_amazon_table_is_read_but_recommendation_price_is_not(self):
        snap = parse_product_html((FIXTURES/'amazon-g305-hidden-offer.html').read_text(),
                                  'B07CGPZ3ZQ', 'https://www.amazon.ie/dp/B07CGPZ3ZQ')
        self.assertEqual(snap.model_number, '910005283')
        self.assertIsNone(snap.price_eur)
        self.assertEqual(snap.parse_status, 'price_hidden')

    def test_real_cex_model_attribute_is_read_without_assuming_box_id_is_ean(self):
        async def go():
            payload = json.loads((FIXTURES/'cex-g305-detail.json').read_text())
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200,json=payload))) as client:
                quote = await fetch_box_prices(client,'5099206077836B',strict=True)
                self.assertEqual(quote.model_number,'g305')
                self.assertEqual(quote.barcodes,())
                self.assertEqual(quote.cash_eur,21)
        asyncio.run(go())

    def test_real_consoles_with_zero_base_cash_enter_the_target_catalogue(self):
        hits = json.loads((FIXTURES/'cex-calculated-cash.json').read_text())['hits']
        self.assertTrue(all(h['cashBuyPrice'] == 0 for h in hits))
        self.assertTrue(all(search_cash_price(h)>10 for h in hits))
        async def go():
            state=target_state({}); budget={}
            with patch('app.scraper.cex_ie.search_boxes',new_callable=AsyncMock,return_value=hits) as search:
                await discover_targets(state,TargetConfig(enabled=True,queries=('PS5',)),
                                       CrawlConfig(roots=[],cex_enabled=True,cex_request_delay_seconds=0),object(),budget)
                self.assertEqual(len(state['items']),len(hits))
                self.assertEqual(search.await_args.kwargs['hits_per_page'],100)
                self.assertEqual(budget['live_lookups'],1)
        asyncio.run(go())

    def test_rrp_foreign_currency_other_variants_and_unavailable_prices_cannot_pass(self):
        prefix='<span id="productTitle">Sony WH-1000XM5 Black</span>'
        rrp='<span class="a-price a-text-price"><span class="a-offscreen">€400</span></span>'
        current='<span class="a-price"><span class="a-offscreen">€200</span></span>'
        html=prefix+'<div id="corePriceDisplay_desktop_feature_div">'+rrp+current+'</div>'
        self.assertEqual(parse_product_html(html,'B000000001','').price_eur,200)
        for extra, replacement in [('',current.replace('€','£')),('<input id="ASIN" value="B000000002">',current),
                                   ('<div id="availability">Currently unavailable</div>',current)]:
            snap=parse_product_html(prefix+extra+'<div id="corePriceDisplay_desktop_feature_div">'+replacement+'</div>','B000000001','')
            self.assertIsNone(snap.price_eur)

    def test_http_200_captcha_stops_requests_and_reports_reason(self):
        async def go():
            requests=[]
            def handler(request):
                requests.append(request)
                return httpx.Response(200,text='<title>Robot Check</title><form action="/errors/validateCaptcha"></form>')
            diagnostics={}
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                rows=[{'asin':f'B{i:09}', 'url':f'https://www.amazon.ie/dp/B{i:09}'} for i in range(3)]
                await spot_check_list_prices(client,rows,config=CrawlConfig(roots=[]),allow_alerts=False,
                                             delay_seconds=0,diagnostics=diagnostics)
            self.assertEqual(len(requests),1)
            self.assertEqual(list(diagnostics.values()),['provider_blocked'])
        asyncio.run(go())

    def test_production_false_matches_are_rejected(self):
        for amazon,cex in [
            ('Logitech G240 Cloth Gaming Mouse Pad','Logitech G502 X Lightspeed Wireless Gaming Mouse'),
            ('Razer BlackShark V2 X Headset Black','Razer BlackShark V2 X Headset White'),
            ('Sony WH-1000XM4 Headphones','Sony WH-1000XM5 Headphones'),
            ('Apple iPhone 15 Pro 128GB Blue','Apple iPhone 15 Pro 128GB Black'),
            ('EA Sports FC 25 PS5','EA Sports F1 25 PS5'),
            ('Nintendo Switch 2 Console 256GB Black','Nintendo Switch Console 256GB Black'),
        ]:
            with self.subTest(amazon=amazon): self.assertFalse(titles_match_for_trade_in(amazon,cex))

    def test_console_brand_aliases_keep_compatible_research_candidates(self):
        self.assertTrue(titles_match_for_trade_in('Sony PlayStation 5 Console 825GB White',
                                                'Playstation 5 Console, 825GB, White, Unboxed'))
        self.assertTrue(titles_match_for_trade_in('Nintendo Switch Console 32GB Grey',
                                                'Switch Console 32GB Grey, Boxed'))


if __name__ == '__main__': unittest.main()
