import unittest
from datetime import datetime,timedelta,timezone
from app.services.public_price_history import record_price
from app.services.catalogue import make_section_catalogue
from app.services.bestsellers import CrawlResult
from app.scraper.bestsellers import BestsellerItem

class PublicHistoryTests(unittest.TestCase):
    def test_changes_ordering_invalid_prices_and_duplicate_timestamps(self):
        h=[]
        for day,price in [(1,100),(3,80),(2,150),(3,80),(4,None),(5,float('nan'))]:
            h=record_price(h,price,f'2026-11-{day:02}T12:00:00+00:00','product_page')
        self.assertEqual([p['price_eur'] for p in h],[100,150,80])
    def test_unchanged_prices_keep_first_and_latest_and_changes_remain_bounded(self):
        start=datetime(2026,11,1,tzinfo=timezone.utc);h=[]
        for i in range(400):h=record_price(h,100,(start+timedelta(minutes=i)).isoformat(),'product_page')
        self.assertEqual(len(h),2)
        for i in range(400,600):h=record_price(h,i,(start+timedelta(minutes=i)).isoformat(),'product_page')
        self.assertEqual(len(h),120)
    def test_catalogue_imports_actual_prior_price_and_resets_changed_identity(self):
        item=BestsellerItem('B000000001','Sony headphones',80,1,'https://www.amazon.ie/dp/B000000001')
        previous={'items':[{'asin':item.asin,'title':item.title,'price_eur':100,'last_seen_at':'2026-11-01T12:00:00+00:00'}]}
        result=CrawlResult(items={item.asin:(item,'Audio')})
        row=make_section_catalogue(result,previous,section_id='pc',section_name='PC',observed_at='2026-11-02T12:00:00+00:00')['items'][0]
        self.assertEqual([p['price_eur'] for p in row['price_history']],[100,80])
        previous['items'][0]['title']='Different product'
        row=make_section_catalogue(result,previous,section_id='pc',section_name='PC',observed_at='2026-11-02T12:00:00+00:00')['items'][0]
        self.assertEqual(len(row['price_history']),1)
