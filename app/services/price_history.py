"""Conservative pre-drop Amazon price reference, independent of today's low quote.

The last 20 raw checks remain in SQLite. This separate anchor is checkpointed to
GitHub Pages and avoids chasing an extended sale lower scan after scan.
"""
from datetime import datetime, timedelta, timezone
from math import isfinite
from statistics import median

from app import database as db

STABILITY_BAND = 0.20
NORMAL_BAND = 0.25
INITIAL_LOOKBACK_DAYS = 14


def _recent_stable_reference(product_id: int, now: datetime) -> float | None:
    prices: list[float] = []
    for check in db.price_history(product_id, limit=12):
        raw_price = check.get("price_eur")
        if raw_price is None:
            continue
        try:
            price = float(raw_price)
            checked_at = datetime.fromisoformat(check["checked_at"])
        except (TypeError, ValueError):
            continue
        if not isfinite(price) or price <= 0 or checked_at.tzinfo is None:
            continue
        age = now - checked_at
        if timedelta(0) <= age <= timedelta(days=INITIAL_LOOKBACK_DAYS):
            prices.append(price)
        if len(prices) >= 3:
            break

    if len(prices) < 3:
        return None
    usual = float(median(prices))
    # A noisy/variant-changing bestseller cannot establish a trustworthy baseline.
    if usual < 20 or any(abs(price / usual - 1) > STABILITY_BAND for price in prices):
        return None
    return round(usual, 2)


def observe_price_reference(
    product_id: int, price_eur: float | None, *, max_age_days: int = 30
) -> float | None:
    """Update price anchor BEFORE saving the new price check.

    Need 3 *previous* stable observations before trusting an Amazon-only drop.
    Prices >25% away never pull the normal price toward a suspected error.
    After 30 days without a normal-price confirmation, a stable recent level can
    become the new reference (or the stale reference is removed).
    """
    if price_eur is None or not isfinite(price_eur) or price_eur <= 0:
        return None

    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    existing = db.get_price_baseline(product_id)
    if existing:
        reference = float(existing["reference_eur"])
        try:
            confirmed_at = datetime.fromisoformat(existing["confirmed_at"])
            if confirmed_at.tzinfo is None:
                raise ValueError("Naive timestamp")
        except (ValueError, TypeError):
            confirmed_at = datetime.min.replace(tzinfo=timezone.utc)

        if now - confirmed_at > timedelta(days=max_age_days):
            fresh = _recent_stable_reference(product_id, now)
            if fresh is None:
                db.delete_price_baseline(product_id)
                return None
            db.save_price_baseline(
                product_id, fresh, confirmed_at=now_iso, last_seen_at=now_iso
            )
            return fresh

        if reference * (1 - NORMAL_BAND) <= price_eur <= reference * (1 + NORMAL_BAND):
            # Mild normal-price movement adapts slowly, not to isolated spikes/dips.
            reference = round(reference * 0.9 + price_eur * 0.1, 2)
            confirmed_at_iso = now_iso
        else:
            confirmed_at_iso = existing["confirmed_at"]
        db.save_price_baseline(
            product_id,
            reference,
            confirmed_at=confirmed_at_iso,
            last_seen_at=now_iso,
        )
        return reference

    reference = _recent_stable_reference(product_id, now)
    if reference is not None:
        db.save_price_baseline(
            product_id, reference, confirmed_at=now_iso, last_seen_at=now_iso
        )
    return reference
