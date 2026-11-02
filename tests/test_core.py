import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app import database as db
from app.detector.price_error import evaluate_price
from app.scraper.amazon_ie import extract_asin, parse_product_html


class TempDbTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = settings.database_path
        settings.database_path = Path(self._tmp.name) / "tracker.db"
        db.init_db()

    def tearDown(self):
        settings.database_path = self._old_db
        self._tmp.cleanup()


class TestAsin(unittest.TestCase):
    def test_extract_from_dp_url(self):
        url = "https://www.amazon.ie/dp/B0CX1234AB/ref=abc"
        self.assertEqual(extract_asin(url), "B0CX1234AB")

    def test_extract_raw_asin(self):
        self.assertEqual(extract_asin("b0cx1234ab"), "B0CX1234AB")


class TestParseHtml(unittest.TestCase):
    def test_parses_core_price(self):
        html = """
        <html><body>
          <span id="productTitle">Test Widget</span>
          <span class="a-price" data-a-color="price">
            <span class="a-offscreen">€12.99</span>
          </span>
          <span class="a-price a-text-price">
            <span class="a-offscreen">€129.99</span>
          </span>
        </body></html>
        """
        snap = parse_product_html(html, "B0TEST1234", "https://www.amazon.ie/dp/B0TEST1234")
        self.assertEqual(snap.title, "Test Widget")
        self.assertEqual(snap.price_eur, 12.99)
        self.assertEqual(snap.list_price_eur, 129.99)


class TestDetector(TempDbTestCase):
    def test_flags_large_list_discount(self):
        pid = db.add_product("B0TEST1234", "https://www.amazon.ie/dp/B0TEST1234")
        verdict = evaluate_price(product_id=pid, price_eur=19.99, list_price_eur=199.99)
        self.assertTrue(verdict.is_error)
        self.assertIn("off list price", verdict.reason)


if __name__ == "__main__":
    unittest.main()
