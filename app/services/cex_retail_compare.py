"""Refresh Irish retailer product pages linked to CeX targets and compute buy→CeX margins."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone

from app.services.alert_policy import load_alert_policy
from app.services.cex_arbitrage import build_cex_row
from app.services.cex_cache import cache_price
from app.services.retail_compare import (
    REQUEST_DELAY_SECONDS,
    fetch_retail_offer,
    parse_retail_spec,
    retailer_from_url,
    trusted_retailer_url,
    _cache_fresh,
    _utc_now,
    clean_barcode,
    clean_model,
)


def retail_spec_for_box(state: dict, box_id: str) -> dict:
    """Merge committed and runtime user link payloads for one CeX box."""
    link = (state.get("user_links") or {}).get(box_id) or {}
    raw = {
        "retailer_urls": link.get("retailer_urls") or [],
        "model": link.get("model"),
        "barcode": link.get("barcode"),
    }
    record = (state.get("items") or {}).get(box_id) or {}
    quote = record.get("quote") or {}
    prices = cache_price(record)
    if prices:
        if prices.barcodes and not raw.get("barcode"):
            raw["barcode"] = prices.barcodes[0]
        if prices.model_number and not raw.get("model"):
            raw["model"] = prices.model_number
    spec = parse_retail_spec(raw)
    spec["cex_title"] = (prices.box_name if prices else quote.get("box_name")) or ""
    spec["cex_category"] = (prices.category_name if prices else quote.get("category_name")) or ""
    return spec


def _flip_row(offer: dict, prices, config) -> dict:
    if offer.get("status") != "matched" or offer.get("price_eur") is None:
        return offer
    policy = load_alert_policy()
    built = build_cex_row(
        {"title": offer.get("title") or "", "price_eur": float(offer["price_eur"]), "url": offer.get("url")},
        prices,
        min_profit_eur=max(config.cex_min_cash_profit_eur, policy.min_cex_cash_profit_eur),
        min_profit_percent=config.cex_min_cash_profit_percent,
        min_roi_percent=max(config.cex_min_roi_percent, policy.min_cex_roi_percent),
        delivery_eur=config.cex_delivery_eur,
        extra_cost_eur=config.cex_extra_cost_eur,
        payout_haircut_percent=config.cex_payout_haircut_percent,
    )
    gross = round(float(prices.cash_eur) - float(offer["price_eur"]), 2)
    offer = {
        **offer,
        "cex_cash_eur": prices.cash_eur,
        "cex_gross_delta_eur": gross,
        "cex_net_profit_eur": built.get("cex_cash_profit_eur"),
        "cex_meets_margin": bool(built.get("cex_cash_flip")),
    }
    return offer


def _summarise(offers: list[dict], *, checked_at: str) -> dict:
    good = [o for o in offers if o.get("status") == "matched" and o.get("price_eur") is not None]
    best = min(good, key=lambda o: (o["price_eur"], o.get("retailer") or "")) if good else None
    best_net = max(good, key=lambda o: (o.get("cex_net_profit_eur") or -1e9)) if good else None
    return {
        "checked_at": checked_at,
        "offers": offers,
        "matched_count": len(good),
        "best_price_retailer": best.get("retailer") if best else None,
        "best_price_eur": best.get("price_eur") if best else None,
        "best_price_url": best.get("url") if best else None,
        "best_price_gross_delta_eur": best.get("cex_gross_delta_eur") if best else None,
        "best_price_net_profit_eur": best.get("cex_net_profit_eur") if best else None,
        "best_net_retailer": best_net.get("retailer") if best_net else None,
        "best_net_profit_eur": best_net.get("cex_net_profit_eur") if best_net else None,
        "basis": "Buy at shop → CeX cash; item price only; grade and stock not verified",
    }


async def refresh_cex_retail_compares(state, config, client, *, max_requests: int = 12, now=None) -> dict:
    """Fetch linked Irish retailer pages and attach CeX flip maths."""
    now = now or datetime.now(timezone.utc)
    remaining = max(0, int(max_requests))
    stats = {"attempted": 0, "matched": 0, "boxes": 0}
    prior = state.setdefault("retail_compare", {})
    due: list[tuple[str, dict]] = []
    for box_id in sorted(set((state.get("user_links") or {}).keys())):
        spec = retail_spec_for_box(state, box_id)
        if not spec.get("retailer_urls"):
            continue
        record = (state.get("items") or {}).get(box_id)
        prices = cache_price(record) if record else None
        if not prices or prices.cash_eur <= 0:
            continue
        due.append((box_id, spec))
    due.sort(key=lambda row: (-cache_price((state.get("items") or {}).get(row[0])).cash_eur, row[0]))
    for box_id, spec in due:
        if remaining <= 0 and not any(
            _cache_fresh(o, now) for o in (prior.get(box_id) or {}).get("offers") or [] if isinstance(o, dict)
        ):
            break
        record = state["items"][box_id]
        prices = cache_price(record)
        key = hashlib.sha256(json.dumps({
            "urls": spec["retailer_urls"],
            "model": spec.get("model"),
            "barcode": spec.get("barcode"),
            "title": spec.get("cex_title"),
        }, sort_keys=True).encode()).hexdigest()
        previous = prior.get(box_id) or {}
        cache_by_url = {
            o.get("url"): o for o in (previous.get("offers") or [])
            if isinstance(o, dict) and previous.get("cache_key") == key
        }
        offers = []
        for url in spec["retailer_urls"]:
            if not trusted_retailer_url(url):
                continue
            cached = cache_by_url.get(url)
            if cached and _cache_fresh(cached, now):
                offers.append(_flip_row(cached, prices, config))
                continue
            if remaining <= 0:
                offers.append({
                    "url": url,
                    "retailer": retailer_from_url(url),
                    "status": "pending_refresh",
                    "checked_at": None,
                })
                continue
            stats["attempted"] += 1
            offer = await fetch_retail_offer(
                client, url,
                expected_model=clean_model(spec.get("model")),
                expected_barcode=clean_barcode(spec.get("barcode")),
                title_fallback=spec.get("cex_title") or None,
                cex_category_name=spec.get("cex_category") or None,
            )
            offer = _flip_row(offer, prices, config)
            offers.append(offer)
            if offer.get("status") != "identity_required":
                remaining -= 1
            if offer.get("status") == "matched":
                stats["matched"] += 1
            await asyncio.sleep(REQUEST_DELAY_SECONDS)
        summary = _summarise(offers, checked_at=_utc_now())
        summary["cache_key"] = key
        prior[box_id] = summary
        stats["boxes"] += 1
    state["retail_compare"] = prior
    return stats
