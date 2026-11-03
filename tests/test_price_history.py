import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import database as db
from app.config import settings
from app.services.price_history import observe_price_reference


class TestDurablePriceReference(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.original_path = settings.database_path
        settings.database_path = Path(self.tmp.name) / "prices.db"
        db.init_db()
        self.pid = db.add_product("B0TEST1234", "https://www.amazon.ie/dp/B0TEST1234")

    def tearDown(self):
        settings.database_path = self.original_path
        self.tmp.cleanup()

    def tick(self, price):
        ref = observe_price_reference(self.pid, price)
        db.record_price_check(self.pid, price, None, "EUR")
        return ref

    def test_requires_three_prior_stable_observations(self):
        self.tick(200)
        self.tick(198)
        self.tick(201)
        self.assertIsNone(db.get_price_baseline(self.pid))
        self.assertEqual(self.tick(20), 200.0)
        self.assertEqual(db.get_price_baseline(self.pid)["reference_eur"], 200.0)

    def test_maintains_pre_drop_reference_across_many_low_scans(self):
        for _ in range(4):
            self.tick(200)
        for _ in range(25):
            self.tick(20)
        self.assertAlmostEqual(db.get_price_baseline(self.pid)["reference_eur"], 200, places=1)
        self.assertEqual(len(db.price_history(self.pid, 100)), 29)

    def test_general_ten_percent_discount_does_not_erase_its_own_reference(self):
        for _ in range(4):
            self.tick(100.00)
        for _ in range(16):
            self.tick(85.00)
        self.assertEqual(db.get_price_baseline(self.pid)["reference_eur"], 100.00)

    def test_unstable_history_does_not_create_reference(self):
        self.tick(200)
        self.tick(50)
        self.tick(300)
        self.assertIsNone(self.tick(10))

    def test_checkpoint_restore_when_cache_missing(self):
        for price in (200, 200, 200, 20):
            self.tick(price)
        snapshot = db.export_price_baselines({"B0TEST1234"})
        self.assertIn("B0TEST1234", snapshot)
        settings.database_path = Path(self.tmp.name) / "restored.db"
        db.init_db()
        self.assertEqual(db.import_price_baselines(snapshot), 1)
        found = db.list_products()[0]
        self.assertEqual(db.get_price_baseline(found["id"])["reference_eur"], 200.0)
        self.assertEqual(db.import_price_baselines({"INVALID": ["oops", "none", "bad"]}), 0)

    def test_expired_reference_does_not_trigger_old_drop(self):
        stale = (datetime.now(timezone.utc) - timedelta(days=35)).isoformat()
        for _ in range(4):
            db.record_price_check(self.pid, 200, None, "EUR", checked_at=stale)
        db.save_price_baseline(self.pid, 200, confirmed_at=stale, last_seen_at=stale)
        self.assertIsNone(self.tick(20))
        self.assertIsNone(db.get_price_baseline(self.pid))


if __name__ == "__main__":
    unittest.main()
