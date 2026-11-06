import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.config import settings
from app import database as db
from app.services import amazon_access
from app.services.monitor import run_once
from app.snapshot import export_snapshot


class TestWatchlistAmazonAccess(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = settings.database_path
        settings.database_path = Path(self._tmp.name) / "tracker.db"
        db.init_db()

    def tearDown(self):
        settings.database_path = self._old_db
        self._tmp.cleanup()

    def test_export_keeps_last_price_after_challenge(self):
        pid = db.add_product("B07K29F7J8", "https://www.amazon.ie/dp/B07K29F7J8", "Coffee machine")
        db.record_price_check(pid, 549.0, 699.0, "EUR", checked_at="2026-11-05T20:00:00+00:00")
        db.record_price_check(
            pid,
            None,
            None,
            None,
            raw_note="Amazon returned a challenge page",
            checked_at="2026-11-06T00:39:00+00:00",
        )
        payload = export_snapshot()
        product = payload["products"][0]
        self.assertEqual(product["price_eur"], 549.0)
        self.assertTrue(product["price_stale"])
        self.assertEqual(product["status"], "fetch_error")

    def test_cooling_down_skips_fetch_and_marks_status(self):
        pid = db.add_product("B07K29F7J8", "https://www.amazon.ie/dp/B07K29F7J8", "Coffee machine")
        db.record_price_check(pid, 549.0, None, "EUR", checked_at="2026-11-05T20:00:00+00:00")
        db.record_price_check(
            pid,
            None,
            None,
            None,
            raw_note="Amazon returned a challenge page",
            checked_at="2026-11-06T00:39:00+00:00",
        )
        until = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
        db.set_meta(amazon_access.WATCHLIST_AMAZON_BLOCKED_META, until)

        with patch("app.services.monitor.fetch_product", new=AsyncMock()) as fetch:
            metadata = asyncio.run(run_once())
            fetch.assert_not_called()
        self.assertEqual(metadata, {})

        payload = export_snapshot()
        self.assertEqual(payload["scan_status"], "provider_cooldown")
        self.assertEqual(payload["products"][0]["status"], "provider_cooldown")


class TestAmazonCooldownHelpers(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = settings.database_path
        settings.database_path = Path(self._tmp.name) / "tracker.db"
        db.init_db()

    def tearDown(self):
        settings.database_path = self._old_db
        self._tmp.cleanup()

    def test_start_and_clear_cooldown(self):
        iso = amazon_access.start_watchlist_amazon_cooldown(
            now=datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc)
        )
        self.assertTrue(amazon_access.watchlist_amazon_cooling_down(now=datetime(2026, 11, 6, 0, 15, tzinfo=timezone.utc)))
        amazon_access.clear_watchlist_amazon_cooldown()
        self.assertFalse(amazon_access.watchlist_amazon_cooling_down(now=datetime(2026, 11, 6, 0, 15, tzinfo=timezone.utc)))
        self.assertIn("2026-11-06T00:30:00", iso)


if __name__ == "__main__":
    unittest.main()
