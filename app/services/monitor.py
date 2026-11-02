import asyncio
import logging
from dataclasses import dataclass

import httpx

from app.config import settings
from app import database as db
from app.detector.price_error import evaluate_price, should_notify_again
from app.notifier.telegram import format_price_alert, send_telegram_message, send_whatsapp_message
from app.scraper.amazon_ie import ProductSnapshot, extract_asin, fetch_product, normalize_product_url
from app.services.price_history import observe_price_reference

logger = logging.getLogger(__name__)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IE,en;q=0.9",
}


@dataclass
class DetectedAlert:
    alert_id: int
    asin: str
    discount_percent: float | None
    message: str


def record_and_detect(
    product_id: int,
    snap: ProductSnapshot,
    *,
    known_title: str | None = None,
    cooldown_minutes: int | None = None,
    baseline_max_age_days: int = 30,
) -> DetectedAlert | None:
    if snap.title and snap.title != known_title:
        db.update_product_title(product_id, snap.title)

    observe_price_reference(product_id, snap.price_eur, max_age_days=baseline_max_age_days)
    db.record_price_check(
        product_id,
        snap.price_eur,
        snap.list_price_eur,
        snap.currency,
    )

    return detect_price_lead(
        product_id, snap, known_title=known_title,
        cooldown_minutes=cooldown_minutes,
    )


def detect_price_lead(
    product_id: int,
    snap: ProductSnapshot,
    *,
    known_title: str | None = None,
    cooldown_minutes: int | None = None,
) -> DetectedAlert | None:
    """Flag a suspected price error without checking Amazon's marketplace seller.

    The observed price can be from a bestseller card or product page. It is
    never a confirmed checkout price; notifications explicitly say so.
    """
    verdict = evaluate_price(
        product_id=product_id,
        price_eur=snap.price_eur,
        list_price_eur=snap.list_price_eur,
    )
    if not verdict.is_error or snap.price_eur is None:
        return None
    pending = db.last_alert_for_product(product_id)
    if (
        pending
        and not pending.get("notified_telegram")
        and not pending.get("notified_whatsapp")
        and abs(float(pending["price_eur"]) - snap.price_eur) < 0.01
    ):
        # Reuse the unsent alert ID. A temporary Telegram failure should not
        # suppress all notifications until the next 24-hour cooldown.
        message = format_price_alert(
            title=snap.title or known_title, asin=snap.asin, url=snap.url,
            price_eur=snap.price_eur, list_price_eur=snap.list_price_eur,
            discount_percent=verdict.discount_percent, reason=verdict.reason,
        )
        return DetectedAlert(int(pending["id"]), snap.asin, verdict.discount_percent, message)
    if not should_notify_again(product_id, snap.price_eur, cooldown_minutes=cooldown_minutes):
        logger.info("Skipping duplicate alert for product %s", product_id)
        return None

    alert_id = db.create_alert(
        product_id=product_id,
        price_eur=snap.price_eur,
        list_price_eur=snap.list_price_eur,
        discount_percent=verdict.discount_percent,
        reason=verdict.reason,
    )
    message = format_price_alert(
        title=snap.title or known_title,
        asin=snap.asin,
        url=snap.url,
        price_eur=snap.price_eur,
        list_price_eur=snap.list_price_eur,
        discount_percent=verdict.discount_percent,
        reason=verdict.reason,
    )
    return DetectedAlert(alert_id, snap.asin, verdict.discount_percent, message)


async def send_notification(message: str) -> tuple[bool, bool]:
    telegram_ok = whatsapp_ok = False
    if settings.telegram_enabled:
        try:
            await send_telegram_message(message)
            telegram_ok = True
        except Exception as exc:
            # Telegram's API token is embedded in the request URL. An httpx
            # exception message/traceback can expose it in Actions logs.
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.error("Telegram notify failed (%s, HTTP status=%s); credentials were not logged",
                         type(exc).__name__, status)
    if settings.whatsapp_enabled:
        try:
            await send_whatsapp_message(message)
            whatsapp_ok = True
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            logger.error("WhatsApp notify failed (%s, HTTP status=%s)",
                         type(exc).__name__, status)
    if not settings.telegram_enabled and not settings.whatsapp_enabled:
        logger.warning(
            "Price error detected but no notifier configured — set Telegram or Twilio WhatsApp env vars"
        )
    return telegram_ok, whatsapp_ok


async def notify_alert(alert: DetectedAlert) -> None:
    telegram_ok, whatsapp_ok = await send_notification(alert.message)
    if telegram_ok or whatsapp_ok:
        db.mark_alert_notified(alert.alert_id, telegram=telegram_ok, whatsapp=whatsapp_ok)
        logger.info("Alert sent for %s", alert.asin)


async def check_product(client: httpx.AsyncClient, product: dict) -> None:
    product_id = int(product["id"])
    url = product["url"]
    try:
        snap = await fetch_product(client, url)
    except Exception as exc:
        logger.warning("Scrape failed for %s: %s", url, exc)
        db.record_price_check(product_id, None, None, None, raw_note=str(exc))
        return

    alert = record_and_detect(product_id, snap, known_title=product.get("title"))
    if alert:
        await notify_alert(alert)


async def run_once() -> None:
    async with httpx.AsyncClient(headers=BROWSER_HEADERS, timeout=30.0) as client:
        products = db.list_products(enabled_only=True)
        for product in products:
            await check_product(client, product)
            await asyncio.sleep(settings.request_delay_seconds)


async def run_monitor_loop(stop_event: asyncio.Event | None = None) -> None:
    while True:
        await run_once()
        if stop_event and stop_event.is_set():
            break
        await asyncio.sleep(settings.poll_interval_seconds)


def add_watch_url(url_or_asin: str) -> dict:
    asin = extract_asin(url_or_asin)
    if not asin:
        raise ValueError("Invalid Amazon URL or ASIN")
    url = normalize_product_url(asin)
    pid = db.add_product(asin, url)
    products = db.list_products()
    return next(p for p in products if p["id"] == pid)
