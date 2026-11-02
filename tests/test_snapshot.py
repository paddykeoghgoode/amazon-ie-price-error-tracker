import json
import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app import database as db
from app.snapshot import (
    export_snapshot,
    import_snapshot,
    read_snapshot,
    render_tracker_markdown,
    write_snapshot,
)
from scripts.scan import extract_asins_from_text, load_watchlist


class TestWatchlist(unittest.TestCase):
    def test_load_watchlist_urls_and_asins(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watchlist.json"
            path.write_text(
                json.dumps(
                    {
                        "products": [
                            "https://www.amazon.ie/dp/B0AAA11111",
                            {"asin": "B0BBB22222"},
                            {"url": "https://www.amazon.ie/gp/product/B0CCC33333"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            urls = load_watchlist(path)
            self.assertEqual(len(urls), 3)

    def test_extract_asins_from_issue_body(self):
        body = "### Amazon.ie URL or ASIN\n\nhttps://www.amazon.ie/dp/B0CX1234AB/ref=sr_1"
        self.assertEqual(extract_asins_from_text("Watch: B0CX1234AB", body), ["B0CX1234AB"])


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = settings.database_path
        settings.database_path = Path(self._tmp.name) / "tracker.db"
        db.init_db()

    def tearDown(self):
        settings.database_path = self._old_db
        self._tmp.cleanup()

    def test_roundtrip_preserves_alert_time_and_price(self):
        pid = db.add_product("B0SNAP0001", "https://www.amazon.ie/dp/B0SNAP0001", "Snap Item")
        db.record_price_check(
            pid, 19.99, 199.99, "EUR", checked_at="2026-11-01T12:00:00+00:00"
        )
        db.create_alert(
            pid,
            19.99,
            199.99,
            90.0,
            "90% off list price (€199.99 → €19.99)",
            created_at="2026-11-01T12:01:00+00:00",
        )
        payload = export_snapshot(github_repo="paddykeoghgoode/amazon-ie-price-error-tracker")
        self.assertEqual(payload["github_repo"], "paddykeoghgoode/amazon-ie-price-error-tracker")
        self.assertTrue(payload["pages_url"].endswith("/amazon-ie-price-error-tracker/"))
        self.assertEqual(payload["products"][0]["status"], "price_error")

        out = Path(self._tmp.name) / "data.json"
        write_snapshot(out, payload)

        settings.database_path = Path(self._tmp.name) / "restored.db"
        db.init_db()
        import_snapshot(read_snapshot(out))
        alerts = db.recent_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["created_at"], "2026-11-01T12:01:00+00:00")
        self.assertEqual(alerts[0]["price_eur"], 19.99)

        markdown = render_tracker_markdown(payload)
        self.assertIn("price error", markdown)
        self.assertIn("Snap Item", markdown)
        self.assertIn("€19.99", markdown)


if __name__ == "__main__":
    unittest.main()
