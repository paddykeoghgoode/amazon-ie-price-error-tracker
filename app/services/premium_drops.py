"""High-ticket and scan-to-scan drop detection beyond the default 75% rule."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from app import database as db
from app.config import settings
from app.scraper.amazon_ie import ProductSnapshot
from app.detector.price_error import should_notify_again
from app.services.monitor import DetectedAlert
from app.services.alert_policy import (
    load_alert_policy, materially_cheaper_than_last, qualifies_observed_drop,
)


@dataclass(frozen=True)
class DropObservation:
    asin: str
    title: str | None
    url: str
    category: str | None
    rank: int | None
    price_eur: float
    reference_eur: float
    drop_percent: float
    drop_kind: str  # rapid_scan | premium_history | list_price
    previous_eur: float | None = None


def _discount(current: float, reference: float) -> float:
    if reference <= 0:
        return 0.0
    return round((1 - current / reference) * 100, 1)


def observe_rapid_scan_drop(
    *,
    asin: str,
    title: str | None,
    url: str,
    category: str | None,
    rank: int | None,
    price_eur: float,
    previous_eur: float | None,
    config: Any,
) -> DropObservation | None:
    if previous_eur is None or previous_eur <= 0:
        return None
    if price_eur >= previous_eur - 0.01:
        return None
    if price_eur < config.rapid_drop_min_price_eur:
        return None
    saving = previous_eur - price_eur
    drop = _discount(price_eur, previous_eur)
    if drop < config.rapid_drop_percent or saving < config.rapid_drop_min_saving_eur:
        return None
    return DropObservation(
        asin=asin,
        title=title,
        url=url,
        category=category,
        rank=rank,
        price_eur=price_eur,
        reference_eur=round(previous_eur, 2),
        drop_percent=drop,
        drop_kind="rapid_scan",
        previous_eur=round(previous_eur, 2),
    )


def observe_premium_history_drop(
    *,
    asin: str,
    title: str | None,
    url: str,
    category: str | None,
    rank: int | None,
    price_eur: float,
    reference_eur: float,
    config: Any,
) -> DropObservation | None:
    if price_eur < config.premium_min_price_eur:
        return None
    if reference_eur < config.premium_min_price_eur * 0.5:
        return None
    drop = _discount(price_eur, reference_eur)
    if drop < config.premium_history_drop_percent:
        return None
    if drop >= settings.min_discount_percent:
        return None  # covered by the standard price-error path
    return DropObservation(
        asin=asin,
        title=title,
        url=url,
        category=category,
        rank=rank,
        price_eur=price_eur,
        reference_eur=round(reference_eur, 2),
        drop_percent=drop,
        drop_kind="premium_history",
    )


def observation_to_row(obs: DropObservation) -> dict:
    return {
        "asin": obs.asin,
        "title": obs.title,
        "url": obs.url,
        "category": obs.category,
        "rank": obs.rank,
        "price_eur": obs.price_eur,
        "reference_eur": obs.reference_eur,
        "median_eur": obs.reference_eur,
        "previous_eur": obs.previous_eur,
        "drop_percent": obs.drop_percent,
        "drop_kind": obs.drop_kind,
    }


def lead_reason(obs: DropObservation) -> str:
    if obs.drop_kind == "rapid_scan":
        return (
            f"{obs.drop_percent:g}% since last scan "
            f"(€{obs.reference_eur:.2f} → €{obs.price_eur:.2f}, high-ticket item)"
        )
    if obs.drop_kind == "list_price":
        return (
            f"{obs.drop_percent:g}% below struck-through list price "
            f"(€{obs.reference_eur:.2f} → €{obs.price_eur:.2f})"
        )
    return (
        f"{obs.drop_percent:g}% below recent reference "
        f"(€{obs.reference_eur:.2f} → €{obs.price_eur:.2f}, premium item)"
    )


def build_lead_alert(
    product_id: int,
    obs: DropObservation,
    *,
    list_price_eur: float | None = None,
    cooldown_minutes: int | None = None,
) -> DetectedAlert | None:
    previous = db.last_alert_for_product(product_id)
    if previous and not materially_cheaper_than_last(
        previous.get("price_eur"), obs.price_eur, load_alert_policy()
    ):
        return None
    if not should_notify_again(product_id, obs.price_eur, cooldown_minutes=cooldown_minutes):
        return None
    reason = lead_reason(obs)
    alert_id = db.create_alert(
        product_id=product_id,
        price_eur=obs.price_eur,
        list_price_eur=list_price_eur,
        discount_percent=obs.drop_percent,
        reason=reason,
    )
    headline = "💎 High-ticket Amazon drop — research lead"
    if obs.drop_kind == "rapid_scan":
        headline = "⚡ Rapid price cut on expensive item — research lead"
    message = (
        f"{headline}\n"
        "⚠️ Seller, condition and checkout price NOT verified. Confirm the exact offer before buying.\n\n"
        f"{obs.title or obs.asin}\n"
        f"Now: €{obs.price_eur:.2f}\n"
        f"Reference: €{obs.reference_eur:.2f}\n"
        f"Move: ~{obs.drop_percent:.0f}%\n\n"
        f"{reason}\n\n"
        f"{obs.url}"
    )
    return DetectedAlert(alert_id, obs.asin, obs.drop_percent, message)


async def spot_check_list_prices(
    client,
    rows: list[dict],
    *,
    config: CrawlConfig,
    allow_alerts: bool,
    delay_seconds: float,
) -> list[DetectedAlert]:
    """Fetch a handful of product pages to read struck-through list prices on expensive items."""
    from app.scraper.amazon_ie import fetch_product

    alerts: list[DetectedAlert] = []
    policy = load_alert_policy()
    budget = min(config.premium_alerts_per_run, policy.max_alerts_per_run)
    seen: set[str] = set()
    for row in rows:
        asin = row.get("asin")
        if not asin or asin in seen:
            continue
        seen.add(asin)
        if len(seen) > config.premium_spot_check_limit:
            break
        try:
            snap = await fetch_product(client, row["url"])
        except Exception:
            if delay_seconds:
                await asyncio.sleep(delay_seconds)
            continue
        obs = list_price_observation(
            snap,
            category=row.get("category"),
            rank=row.get("rank"),
            config=config,
        )
        if (obs and allow_alerts and budget > 0
            and qualifies_observed_drop(observation_to_row(obs), policy)):
            product_id = db.add_product(asin, snap.url, snap.title)
            lead = build_lead_alert(
                product_id, obs, list_price_eur=snap.list_price_eur, cooldown_minutes=config.alert_cooldown_minutes
            )
            if lead:
                alerts.append(lead)
                budget -= 1
        if delay_seconds:
            await asyncio.sleep(delay_seconds)
    return alerts


def list_price_observation(
    snap: ProductSnapshot,
    *,
    category: str | None,
    rank: int | None,
    config: Any,
) -> DropObservation | None:
    if snap.price_eur is None or snap.list_price_eur is None:
        return None
    if snap.price_eur < config.premium_min_price_eur:
        return None
    if snap.list_price_eur < settings.min_list_price_eur:
        return None
    drop = _discount(snap.price_eur, snap.list_price_eur)
    threshold = min(config.premium_history_drop_percent, settings.min_discount_percent)
    if drop < threshold:
        return None
    return DropObservation(
        asin=snap.asin,
        title=snap.title,
        url=snap.url,
        category=category,
        rank=rank,
        price_eur=float(snap.price_eur),
        reference_eur=round(float(snap.list_price_eur), 2),
        drop_percent=drop,
        drop_kind="list_price",
    )
