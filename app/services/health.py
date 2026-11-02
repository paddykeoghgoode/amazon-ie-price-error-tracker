from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app import database as db
from app.config import settings
from app.services.bestsellers import CrawlConfig, CrawlResult, ProcessResult
from app.services.monitor import send_notification

DUBLIN_TZ = ZoneInfo("Europe/Dublin")

WELCOME_KEY = "telegram_welcome_sent"
PROBLEM_ALERT_KEY = "problem_alert_at"
PROBLEM_ACTIVE_KEY = "problem_active"
DIGEST_KEY = "daily_summary_date"

PROBLEM_REPEAT_HOURS = 12
# A full crawl finds ~10,000 items; far fewer without a captcha means the page layout changed.
MIN_HEALTHY_ITEMS = 200


def _problem(result: CrawlResult) -> str | None:
    if result.blocked:
        return (
            f"⚠️ Amazon blocked the bestseller crawl after {len(result.items):,} items "
            f"({result.requests} pages, {result.errors} failed). "
            "It will retry on the next run; you'll hear from me when it recovers."
        )
    if len(result.items) < MIN_HEALTHY_ITEMS:
        return (
            f"⚠️ The bestseller crawl only found {len(result.items):,} items "
            f"from {result.requests} pages. Amazon may have changed its page layout, "
            "so price errors could be missed until the tracker is fixed."
        )
    return None


async def _send(message: str) -> bool:
    telegram_ok, whatsapp_ok = await send_notification(message)
    return telegram_ok or whatsapp_ok


async def send_health_messages(
    result: CrawlResult,
    processed: ProcessResult,
    config: CrawlConfig,
    *,
    dashboard_url: str,
    now: datetime | None = None,
) -> list[str]:
    """Send setup, problem and daily-summary messages. Returns what was sent."""
    if not (settings.telegram_enabled or settings.whatsapp_enabled):
        return []
    now = now or datetime.now(timezone.utc)
    sent: list[str] = []

    if db.get_meta(WELCOME_KEY) is None:
        message = (
            "✅ Amazon.ie price tracker is connected.\n\n"
            f"This crawl checked {len(result.items):,} bestsellers across {len(result.categories)} lists. "
            "Each item needs 3 crawls of price history before it can be flagged, so alerts can start "
            "about an hour from now. You'll get a message here as soon as a price looks like an error.\n\n"
            f"{dashboard_url}"
        )
        if await _send(message):
            db.set_meta(WELCOME_KEY, now.isoformat())
            sent.append(message)

    problem = _problem(result)
    if problem:
        last = db.get_meta(PROBLEM_ALERT_KEY)
        due = last is None or now - datetime.fromisoformat(last) >= timedelta(hours=PROBLEM_REPEAT_HOURS)
        if due and await _send(problem):
            db.set_meta(PROBLEM_ALERT_KEY, now.isoformat())
            sent.append(problem)
        db.set_meta(PROBLEM_ACTIVE_KEY, "1")
    elif db.get_meta(PROBLEM_ACTIVE_KEY) == "1":
        message = f"✅ The bestseller crawl is working again: {len(result.items):,} items checked."
        if await _send(message):
            sent.append(message)
        db.set_meta(PROBLEM_ACTIVE_KEY, "0")

    local = now.astimezone(DUBLIN_TZ)
    hour = config.daily_summary_hour
    if hour is not None and local.hour >= hour and db.get_meta(DIGEST_KEY) != local.date().isoformat():
        alerts_24h = db.alerts_since((now - timedelta(hours=24)).isoformat())
        status = "working normally" if not problem else "having problems (see above)"
        message = (
            "📊 Daily tracker summary\n\n"
            f"Price-error alerts in the last 24 hours: {alerts_24h}\n"
            f"Latest crawl: {len(result.items):,} bestsellers, "
            f"{processed.with_history:,} with enough history to judge\n"
            f"Crawler is {status}.\n\n"
            f"{dashboard_url}"
        )
        if await _send(message):
            db.set_meta(DIGEST_KEY, local.date().isoformat())
            sent.append(message)
    return sent
