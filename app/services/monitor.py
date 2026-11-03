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
from app.services.alert_policy import (
    AlertPolicy, load_alert_policy, qualifies_price_event,
)

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
    alert_policy: AlertPolicy | None = None,
    emit_alert: bool = True,
    allow_high_value_history_lead: bool = False,
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

    if not emit_alert:
        return None
    if alert_policy is not None:
        anchor = db.get_price_baseline(product_id) or {}
        if not qualifies_price_event(
            snap.price_eur,
            historical_reference_eur=anchor.get("reference_eur"),
            list_reference_eur=snap.list_price_eur,
            policy=alert_policy,
        ):
            # Still store the observed price/history; just don't create a
            # low-value alert record or wake the user via Telegram.
            return None
    alert = detect_price_lead(
        product_id, snap, known_title=known_title,
        cooldown_minutes=cooldown_minutes,
    )
    if alert or not allow_high_value_history_lead or alert_policy is None:
        return alert
    # Watchlist product pages also alert on substantial, stable-history
    # 50–74% reductions; the standard price-error detector requires 75%.
    anchor = db.get_price_baseline(product_id) or {}
    reference = float(anchor.get("reference_eur") or 0)
    price = float(snap.price_eur or 0)
    if (
        reference <= 0 or price <= 0
        or not qualifies_price_event(
            price, historical_reference_eur=reference, policy=alert_policy
        ) or not should_notify_again(product_id, price, cooldown_minutes=cooldown_minutes)
    ):
        return None
    pct = 100 * (reference - price) / reference
    reason = (
        f"High-ticket Amazon historical reduction: {pct:.1f}% "
        f"(€{reference:.2f} → €{price:.2f}, saving €{reference-price:.2f})"
    )
    alert_id = db.create_alert(
        product_id=product_id, price_eur=price,
        list_price_eur=snap.list_price_eur,
        discount_percent=round(pct, 1), reason=reason,
    )
    return DetectedAlert(
        alert_id, snap.asin, round(pct, 1),
        "💎 Large Amazon price reduction — RESEARCH ONLY\n"
        f"{snap.title or known_title or snap.asin}\n"
        f"Now €{price:.2f} · historical €{reference:.2f} "
        f"· saving €{reference-price:.2f} ({pct:.0f}%)\n"
        "⚠️ Seller, exact variant, condition and checkout NOT verified.\n"
        f"{snap.url}",
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


async def check_product(client: httpx.AsyncClient, product: dict) -> ProductSnapshot | None:
    product_id = int(product["id"])
    url = product["url"]
    try:
        snap = await fetch_product(client, url)
    except Exception as exc:
        logger.warning("Scrape failed for %s: %s", url, exc)
        db.record_price_check(product_id, None, None, None, raw_note=str(exc))
        return

    alert = record_and_detect(
        product_id, snap, known_title=product.get("title"),
        alert_policy=load_alert_policy(),
        allow_high_value_history_lead=True,
    )
    if alert:
        await notify_alert(alert)
    return snap


async def run_once() -> dict[str, dict]:
    async with httpx.AsyncClient(headers=BROWSER_HEADERS, timeout=30.0) as client:
        products = db.list_products(enabled_only=True)
        metadata: dict[str, dict] = {}
        for product in products:
            snap = await check_product(client, product)
            if snap is not None:
                metadata[product["asin"]] = {
                    "model_number": snap.model_number,
                    "barcode": snap.barcode,
                }
            await asyncio.sleep(settings.request_delay_seconds)
        return metadata


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
