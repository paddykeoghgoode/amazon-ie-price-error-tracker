from dataclasses import dataclass

from app.config import settings
from app import database as db


@dataclass
class PriceErrorVerdict:
    is_error: bool
    reason: str
    discount_percent: float | None = None


def _discount(current: float, reference: float) -> float:
    if reference <= 0:
        return 0.0
    return round((1 - current / reference) * 100, 1)


def evaluate_price(
    *,
    product_id: int,
    price_eur: float | None,
    list_price_eur: float | None,
) -> PriceErrorVerdict:
    if price_eur is None:
        return PriceErrorVerdict(False, "No price on page")

    if price_eur > settings.max_alert_price_eur:
        return PriceErrorVerdict(False, "Price above max alert threshold")

    reasons: list[str] = []

    if list_price_eur and list_price_eur >= settings.min_list_price_eur:
        disc = _discount(price_eur, list_price_eur)
        if disc >= settings.min_discount_percent:
            reasons.append(f"{disc}% off list price (€{list_price_eur:.2f} → €{price_eur:.2f})")

    median = db.median_historical_price(product_id)
    if median and median >= settings.min_list_price_eur:
        drop = _discount(price_eur, median)
        if drop >= settings.min_discount_percent:
            reasons.append(f"{drop}% below recent median (€{median:.2f})")

    if not reasons:
        return PriceErrorVerdict(False, "No suspicious discount")

    reference = list_price_eur or median
    return PriceErrorVerdict(
        is_error=True,
        reason="; ".join(reasons),
        discount_percent=_discount(price_eur, reference) if reference else None,
    )


def should_notify_again(product_id: int, price_eur: float) -> bool:
    last = db.last_alert_for_product(product_id)
    if not last:
        return True
    from datetime import datetime, timedelta, timezone

    created = datetime.fromisoformat(last["created_at"])
    if datetime.now(timezone.utc) - created > timedelta(minutes=settings.cooldown_minutes):
        return True
    last_price = float(last["price_eur"])
    return abs(last_price - price_eur) >= 0.01
