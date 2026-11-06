import unittest

from app.services.retail_compare import parse_retail_html
from app.scraper import cex_ie
from tests.test_retail_comparison import product_html


class CexTitleRetailMatchTests(unittest.TestCase):
    def test_title_fallback_accepts_fc27_without_mpn(self):
        html = product_html(
            model="IGNORED",
            barcode="0000000000000",
            price="69.99",
            name="EA SPORTS FC 27 Standard Edition PlayStation 5 Physical Disc",
        )
        row = parse_retail_html(
            html,
            expected_model=None,
            expected_barcode=None,
            title_fallback="EA Sports FC 27",
            cex_category_name="Playstation5 Software",
        )
        self.assertEqual(row["status"], "matched")
        self.assertEqual(row["identity"], "title_confirmed")
        self.assertEqual(row["price_eur"], 69.99)


if __name__ == "__main__":
    unittest.main()
