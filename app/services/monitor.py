import asyncio
import logging

import httpx

from app.config import settings
from app import database as db
from app.detector.price_error import evaluate_price, should_notify_again
from app.notifier.telegram import format_price_alert, send_telegram_message, send_whatsapp_message
from app.scraper.amazon_ie import extract_asin, fetch_product, normalize_product_url

logger = logging.getLogger(__name__)


async def check_product(client: httpx.AsyncClient, product: dict) -> None:
    product_id = int(product["id"])
    url = product["url"]
    try:
        snap = await fetch_product(client, url)
    except Exception as exc:
        logger.warning("Scrape failed for %s: %s", url, exc)
        db.record_price_check(product_id, None, None, None, raw_note=str(exc))
        return

    if snap.title and snap.title != product.get("title"):
        db.update_product_title(product_id, snap.title)

    db.record_price_check(
        product_id,
        snap.price_eur,
        snap.list_price_eur,
        snap.currency,
    )

    verdict = evaluate_price(
        product_id=product_id,
        price_eur=snap.price_eur,
        list_price_eur=snap.list_price_eur,
    )
    if not verdict.is_error or snap.price_eur is None:
        return
    if not should_notify_again(product_id, snap.price_eur):
        logger.info("Skipping duplicate alert for product %s", product_id)
        return

    alert_id = db.create_alert(
        product_id=product_id,
        price_eur=snap.price_eur,
        list_price_eur=snap.list_price_eur,
        discount_percent=verdict.discount_percent,
        reason=verdict.reason,
    )

    message = format_price_alert(
        title=snap.title or product.get("title"),
        asin=snap.asin,
        url=snap.url,
        price_eur=snap.price_eur,
        list_price_eur=snap.list_price_eur,
        discount_percent=verdict.discount_percent,
        reason=verdict.reason,
    )

    if settings.telegram_enabled:
        try:
            await send_telegram_message(message)
            db.mark_alert_notified(alert_id, telegram=True)
            logger.info("Telegram alert sent for %s", snap.asin)
        except Exception as exc:
            logger.exception("Telegram notify failed: %s", exc)

    if settings.whatsapp_enabled:
        try:
            await send_whatsapp_message(message)
            db.mark_alert_notified(alert_id, whatsapp=True)
        except Exception as exc:
            logger.exception("WhatsApp notify failed: %s", exc)

    if not settings.telegram_enabled and not settings.whatsapp_enabled:
        logger.warning(
            "Price error detected for %s but no notifier configured — set Telegram or Twilio WhatsApp env vars",
            snap.asin,
        )


async def run_monitor_loop(stop_event: asyncio.Event | None = None) -> None:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-IE,en;q=0.9",
    }
    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        while True:
            products = db.list_products(enabled_only=True)
            for product in products:
                await check_product(client, product)
                await asyncio.sleep(settings.request_delay_seconds)
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
