import asyncio
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch
import httpx

from app.services.amazon_target_discovery import discover_amazon_targets, parse_search_results
from app.services.cex_targets import target_state
from app.services.cex_cache import quote_entry
from app.services.bestsellers import CrawlConfig
from app.scraper.cex_ie import CexPrices

NOW=datetime(2026,11,4,tzinfo=timezone.utc)
QUOTE=CexPrices('sony','Sony WH-1000XM5 Headphones Black, B','Headphones',250,300,350,'https://ie.webuy.com',model_number='wh1000xm5')

def html(n=4):
    return ''.join(f'<div data-component-type="s-search-result" data-asin="B{i:09d}"><h2>Sony WH-1000XM5 Headphones Black</h2><span class="a-price"><span class="a-offscreen">€300</span></span></div>' for i in range(n))

def state():
    s=target_state({});s['items']['sony']={**quote_entry(QUOTE.box_name,QUOTE,NOW),'source':'catalogue_search'};return s

def config(): return CrawlConfig(roots=[],cex_enabled=True,cex_live_lookups_per_run=10,cex_request_delay_seconds=0,request_delay_seconds=0)

class AmazonTargetDiscoveryTests(unittest.TestCase):
    def test_one_search_two_candidates_shared_cex_budget_and_daily_cursor(self):
        async def go():
            s=state();budget={'live_lookups':9};requests=[]
            def handle(r): requests.append(r);return httpx.Response(200,text=html())
            async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
                with patch('app.scraper.cex_ie.fetch_box_prices',new_callable=AsyncMock,return_value=QUOTE):
                    rows,stats=await discover_amazon_targets(s,config(),client,budget,now=NOW)
                    self.assertEqual(len(rows),2);self.assertEqual(stats['search_requests'],1)
                    self.assertEqual(budget['live_lookups'],10)
                    self.assertNotIn('model_number',rows[0]) # search does not assert Amazon identity
                    self.assertEqual(s['mappings'],{})
                    again,stats=await discover_amazon_targets(s,config(),client,budget,now=NOW)
                    self.assertEqual(again,[]);self.assertEqual(stats['search_requests'],0)
            self.assertEqual(len(requests),1)
        asyncio.run(go())

    def test_cex_budget_exhaustion_makes_no_amazon_request(self):
        async def go():
            client=AsyncMock(); rows,stats=await discover_amazon_targets(state(),config(),client,{'live_lookups':10},now=NOW)
            self.assertEqual(rows,[]);client.get.assert_not_awaited();self.assertEqual(stats['search_requests'],0)
        asyncio.run(go())

    def test_blocked_search_persists_cooldown_without_retry(self):
        async def go():
            s=state();s['items']['sony']['source']='box_detail'
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(200,text='<title>Robot Check</title>'))) as client:
                rows,stats=await discover_amazon_targets(s,config(),client,{},now=NOW)
                self.assertEqual(stats['status'],'provider_blocked');self.assertEqual(stats['search_requests'],1)
                rows,stats=await discover_amazon_targets(s,config(),client,{},now=NOW+timedelta(minutes=5))
                self.assertEqual(stats['status'],'provider_cooldown');self.assertEqual(stats['search_requests'],0)
        asyncio.run(go())

    def test_wrong_models_and_recommendation_widgets_are_not_candidates(self):
        self.assertEqual(parse_search_results(html().replace('WH-1000XM5','WH-1000XM4'),QUOTE),[])
        with self.assertRaises(ValueError):parse_search_results('<html>unexpected document</html>',QUOTE)

if __name__=='__main__':unittest.main()
