"""Only high-value, credible resale research can wake the user."""
import unittest
from pathlib import Path

from app.services.alert_policy import (
    AlertPolicy, load_alert_policy, qualifies_cex_alert,
    qualifies_observed_drop, qualifies_price_event,
    hotlist_further_drop_qualifies,
)


class TestQuietTelegram(unittest.TestCase):
    def test_cheap_games_suppressed_even_with_90pct_discounts(self):
        self.assertFalse(qualifies_price_event(5, historical_reference_eur=120))
        self.assertFalse(qualifies_price_event(85, historical_reference_eur=500))
        self.assertTrue(qualifies_price_event(150, historical_reference_eur=400))

    def test_historical_vs_advertised_was_prices_are_different(self):
        self.assertFalse(qualifies_price_event(120, list_reference_eur=400))
        self.assertTrue(qualifies_price_event(120, list_reference_eur=600))
        self.assertTrue(qualifies_observed_drop({"price_eur": 150, "reference_eur": 400}))
        self.assertFalse(qualifies_observed_drop({
            "price_eur": 150, "reference_eur": 400, "drop_kind": "list_price"
        }))

    def test_cex_requires_exact_match_and_good_net_profit(self):
        good = {
            "price_eur": 130, "cex_cash_eur": 230,
            "cex_cash_profit_eur": 60, "cex_net_roi_percent": 46,
            "match_confidence": "model_verified",
            "cex_quote_fresh_for_alert": True, "cex_cash_flip": True,
        }
        self.assertTrue(qualifies_cex_alert(good))
        for changed in (
            {"match_confidence": "title_only"},
            {"cex_cash_profit_eur": 10},
            {"cex_net_roi_percent": 12},
            {"cex_quote_fresh_for_alert": False},
            {"price_eur": 15},
        ):
            self.assertFalse(qualifies_cex_alert({**good, **changed}))

    def test_hotlist_requires_further_material_cut(self):
        row = {"price_eur": 200, "reference_eur": 700}
        self.assertFalse(hotlist_further_drop_qualifies(row, previous_eur=200, current_eur=195))
        self.assertTrue(hotlist_further_drop_qualifies(row, previous_eur=200, current_eur=170))

    def test_no_routine_messages_on_automated_runs(self):
        crawl = Path("scripts/crawl_bestsellers.py").read_text(encoding="utf-8")
        watch = Path("scripts/scan.py").read_text(encoding="utf-8")
        self.assertNotIn("await send_section_summary(", crawl)
        self.assertNotIn("await send_health_messages(", crawl)
        self.assertNotIn("asyncio.run(send_watchlist_summary(", watch)
        self.assertIn("send_alerts(", crawl)

    def test_policy_is_configured_at_strict_but_useful_thresholds(self):
        policy = load_alert_policy()
        self.assertGreaterEqual(policy.min_amazon_price_eur, 100)
        self.assertGreaterEqual(policy.min_cex_cash_profit_eur, 40)
        self.assertLessEqual(policy.max_alerts_per_run, 3)
        self.assertEqual(AlertPolicy().max_alerts_per_run, 3)




class TestWatchlistHistory(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from app import database as db
        from app.config import settings
        self.temp = tempfile.TemporaryDirectory()
        self.old_database = settings.database_path
        settings.database_path = Path(self.temp.name) / "data.db"
        db.init_db()
        self.pid = db.add_product("B000000001", "https://www.amazon.ie/dp/B000000001", "Electronics")

    def tearDown(self):
        from app.config import settings
        settings.database_path = self.old_database
        self.temp.cleanup()

    def observe(self, price):
        from app.services.monitor import record_and_detect
        from app.scraper.amazon_ie import ProductSnapshot
        snap = ProductSnapshot(
            asin="B000000001", url="https://www.amazon.ie/dp/B000000001",
            title="Electronics", price_eur=price, currency="EUR",
        )
        return record_and_detect(
            self.pid, snap, alert_policy=AlertPolicy(),
            allow_high_value_history_lead=True,
        )

    def test_low_price_does_not_create_alert_but_history_survives(self):
        from app import database as db
        for _ in range(3):
            self.assertIsNone(self.observe(100))
        self.assertIsNone(self.observe(5))
        self.assertEqual(db.price_history(self.pid, limit=3)[0]["price_eur"], 5)
        self.assertIsNone(db.last_alert_for_product(self.pid))

    def test_large_watchlist_cut_notifies_once_even_below_75pct(self):
        from app import database as db
        for _ in range(3):
            self.assertIsNone(self.observe(500))
        lead = self.observe(200)
        self.assertIsNotNone(lead)
        self.assertIn("Large Amazon price reduction", lead.message)
        self.assertIn("RESEARCH ONLY", lead.message)
        db.mark_alert_notified(lead.alert_id, telegram=True, whatsapp=False)
        self.assertIsNone(self.observe(200), "unchanged lead must not repeat")

if __name__ == "__main__":
    unittest.main()
