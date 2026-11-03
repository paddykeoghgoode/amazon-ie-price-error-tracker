import unittest

from app.deals import merge_section_deals
from app.services.resale_relevance import ResalePolicy, relevance, annotate_payload


class TestResaleRelevance(unittest.TestCase):
    def setUp(self):
        self.policy = ResalePolicy(True, ('Sony', 'Anker', 'UGREEN', 'Sage'), ('ExcludedBrand',))

    def test_known_brands_include_respected_brands_regardless_of_origin(self):
        for title in ('Sony WH-1000XM5', 'Anker Prime charger', 'UGREEN NASync NAS', 'Sage Barista Pro'):
            self.assertTrue(relevance({'title': title}, self.policy)[0])

    def test_unfamiliar_spelling_and_compatibility_do_not_establish_value(self):
        for title in ('ZXQWER wireless earbuds', 'Generic compatible with Sony WH-1000XM5', 'Sonyish headphones'):
            self.assertFalse(relevance({'title': title}, self.policy)[0])

    def test_exact_fresh_cex_evidence_can_admit_unfamiliar_brand(self):
        row = {'title': 'NicheBrand player', 'match_confidence': 'model_verified',
               'cex_quote_fresh_for_alert': True, 'cex_cash_eur': 200}
        self.assertEqual(relevance(row, self.policy)[1], 'exact_cex_resale_evidence')
        self.assertFalse(relevance({**row, 'match_confidence': 'title_only'}, self.policy)[0])
        self.assertFalse(relevance({**row, 'cex_quote_fresh_for_alert': False}, self.policy)[0])
        self.assertFalse(relevance({**row, 'title': 'ExcludedBrand player'}, self.policy)[0])

    def test_filter_can_be_disabled_and_never_deletes_research_rows(self):
        self.assertTrue(relevance({'title': 'Any brand'}, ResalePolicy())[0])
        payload = {'drops': [{'title': 'Unknown product'}, {'title': 'Sony headset'}]}
        annotate_payload(payload, self.policy)
        self.assertEqual(len(payload['drops']), 2)
        self.assertFalse(payload['drops'][0]['resale_eligible'])
        self.assertTrue(payload['drops'][1]['resale_eligible'])


class TestFoundTimestamp(unittest.TestCase):
    def test_same_price_preserves_first_find_across_sections_and_new_price_gets_new_time(self):
        first = '2026-11-03T10:00:00+00:00'
        later = '2026-11-03T12:00:00+00:00'
        original = {'section_id': 'videogames', 'generated_at': first,
                    'drops': [{'asin': 'A', 'title': 'Sony console', 'price_eur': 150, 'found_at': first}]}
        previous = merge_section_deals(None, original)
        incoming = {'section_id': 'cex_priority', 'generated_at': later,
                    'drops': [{'asin': 'A', 'title': 'Sony console', 'price_eur': 150, 'found_at': later}]}
        merged = merge_section_deals(previous, incoming)
        self.assertEqual(merged['sections']['cex_priority']['drops'][0]['found_at'], first)
        incoming['drops'][0].update(price_eur=120, found_at=later)
        changed = merge_section_deals(merged, incoming)
        self.assertEqual(changed['sections']['cex_priority']['drops'][0]['found_at'], later)
