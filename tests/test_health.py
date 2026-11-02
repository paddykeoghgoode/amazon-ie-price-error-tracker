import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from app import database as db
from app.config import settings
from app.scraper.bestsellers import BestsellerItem
from app.services import health
from app.services.bestsellers import CategoryRoot, CrawlConfig, CrawlResult, ProcessResult

# 10:00 GMT on a winter day is 10:00 in Dublin, after the default 09:00 summary hour.
MORNING = datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc)


def healthy_result(count: int = 500) -> CrawlResult:
    result = CrawlResult(categories=["Coffee Machines"], requests=20)
    for n in range(count):
        asin = f"B{n:09d}"
        result.items[asin] = (BestsellerItem(asin=asin, title=f"Item {n}", price_eur=30.0, rank=n + 1, url=f"https://www.amazon.ie/dp/{asin}"), None)
    return result


class TestHealthMessages(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = settings.database_path
        self._old_token, self._old_chat = settings.telegram_bot_token, settings.telegram_chat_id
        settings.database_path = Path(self._tmp.name) / "health.db"
        settings.telegram_bot_token, settings.telegram_chat_id = "token", "chat"
        db.init_db()
        self.config = CrawlConfig(roots=[CategoryRoot("/gp/bestsellers")], daily_summary_hour=9)
        patcher = mock.patch.object(health, "send_notification", new=mock.AsyncMock(return_value=(True, False)))
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        settings.database_path = self._old_db
        settings.telegram_bot_token, settings.telegram_chat_id = self._old_token, self._old_chat
        self._tmp.cleanup()

    def run_health(self, result: CrawlResult, now: datetime, config: CrawlConfig | None = None) -> list[str]:
        return asyncio.run(
            health.send_health_messages(
                result, ProcessResult(), config or self.config, dashboard_url="https://example.test/", now=now
            )
        )

    def test_welcome_and_summary_sent_once(self):
        first = self.run_health(healthy_result(), MORNING)
        self.assertEqual(len(first), 2)
        self.assertIn("connected", first[0])
        self.assertIn("Daily tracker summary", first[1])

        self.assertEqual(self.run_health(healthy_result(), MORNING + timedelta(minutes=30)), [])

        next_day = self.run_health(healthy_result(), MORNING + timedelta(days=1))
        self.assertEqual(len(next_day), 1)
        self.assertIn("Daily tracker summary", next_day[0])

    def test_summary_waits_for_hour_in_dublin(self):
        # 08:30 UTC in summer is 09:30 IST, so the summary is due.
        summer = datetime(2026, 7, 1, 8, 30, tzinfo=timezone.utc)
        sent = self.run_health(healthy_result(), summer)
        self.assertTrue(any("Daily tracker summary" in m for m in sent))

        db.set_meta(health.DIGEST_KEY, "")
        # 07:30 UTC in summer is 08:30 IST, before the summary hour.
        early = datetime(2026, 7, 2, 7, 30, tzinfo=timezone.utc)
        self.assertFalse(any("Daily tracker summary" in m for m in self.run_health(healthy_result(), early)))

    def test_summary_can_be_disabled(self):
        config = CrawlConfig(roots=self.config.roots, daily_summary_hour=None)
        sent = self.run_health(healthy_result(), MORNING, config)
        self.assertEqual(len(sent), 1)
        self.assertIn("connected", sent[0])

    def test_problem_is_rate_limited_then_recovers(self):
        db.set_meta(health.WELCOME_KEY, MORNING.isoformat())
        db.set_meta(health.DIGEST_KEY, MORNING.date().isoformat())
        blocked = healthy_result(50)
        blocked.blocked = True

        first = self.run_health(blocked, MORNING)
        self.assertEqual(len(first), 1)
        self.assertIn("blocked", first[0])
        self.assertEqual(self.run_health(blocked, MORNING + timedelta(hours=1)), [])

        later = self.run_health(blocked, MORNING + timedelta(hours=13))
        self.assertEqual(len(later), 1)

        recovered = self.run_health(healthy_result(), MORNING + timedelta(hours=14))
        self.assertEqual(len(recovered), 1)
        self.assertIn("working again", recovered[0])
        self.assertEqual(self.run_health(healthy_result(), MORNING + timedelta(hours=15)), [])

    def test_too_few_items_is_a_problem(self):
        db.set_meta(health.WELCOME_KEY, MORNING.isoformat())
        db.set_meta(health.DIGEST_KEY, MORNING.date().isoformat())
        sent = self.run_health(healthy_result(10), MORNING)
        self.assertEqual(len(sent), 1)
        self.assertIn("only found 10 items", sent[0])

    def test_failed_send_is_retried_next_run(self):
        self.send.return_value = (False, False)
        self.assertEqual(self.run_health(healthy_result(), MORNING), [])
        self.assertIsNone(db.get_meta(health.WELCOME_KEY))

        self.send.return_value = (True, False)
        self.assertTrue(any("connected" in m for m in self.run_health(healthy_result(), MORNING)))

    def test_nothing_sent_without_notifier(self):
        settings.telegram_bot_token = ""
        self.assertEqual(self.run_health(healthy_result(), MORNING), [])
        self.send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
