from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.scraper import cex_ie
from app.scraper.product_identity import normalise_barcode, normalise_model
from app.scraper.bestsellers import BestsellerItem
from app.services.bestsellers import CrawlConfig, CrawlResult

logger = logging.getLogger(__name__)


def required_cash_profit(amazon_price: float, *, min_eur: float, min_percent: float) -> float:
    return max(min_eur, amazon_price * min_percent / 100)


def item_dict_from_bestseller(
    asin: str, item: BestsellerItem, category: str | None, *, drop_percent: float | None = None
) -> dict[str, Any]:
    return {
        "asin": asin,
        "title": item.title,
        "url": item.url,
        "category": category,
        "rank": item.rank,
        "price_eur": item.price_eur,
        "drop_percent": drop_percent,
        "drop_tier": None,
    }


def select_any_price_candidates(
    result: CrawlResult, config: CrawlConfig, *, skip_asins: set[str]
) -> list[dict[str, Any]]:
    """Pick likely CeX-friendly bestsellers to compare even when Amazon is not on sale."""
    if not config.cex_scan_any_price:
        return []
    keywords = [k.lower() for k in config.cex_priority_keywords]
    scored: list[tuple[int, float, dict[str, Any]]] = []
    for asin, (item, category) in result.items.items():
        if asin in skip_asins or item.price_eur is None:
            continue
        price = float(item.price_eur)
        if price < config.cex_any_price_min_amazon_eur or price > config.cex_any_price_max_amazon_eur:
            continue
        hay = f"{item.title or ''} {category or ''}".lower()
        score = sum(1 for kw in keywords if kw in hay)
        if config.cex_any_price_require_keyword and score == 0:
            continue
        scored.append((score, price, item_dict_from_bestseller(asin, item, category)))
    scored.sort(key=lambda row: (-row[0], -row[1]))
    return [row[2] for row in scored[: config.cex_any_price_max_lookups]]


def build_cex_row(
    item: dict[str, Any],
    prices: cex_ie.CexPrices,
    *,
    min_profit_eur: float,
    min_profit_percent: float,
    delivery_eur: float = 0.0,
    min_roi_percent: float = 0.0,
    extra_cost_eur: float = 0.0,
    payout_haircut_percent: float = 0.0,
) -> dict[str, Any]:
    amazon = float(item["price_eur"])
    if extra_cost_eur < 0 or not 0 <= payout_haircut_percent <= 100:
        raise ValueError("Costs must be non-negative and payout haircut must be 0-100%")
    expected_cash = round(prices.cash_eur * (1 - payout_haircut_percent / 100), 2)
    total_buy = amazon + delivery_eur
    net_cash = round(expected_cash - total_buy - extra_cost_eur, 2)
    net_voucher = round(prices.voucher_eur - total_buy - extra_cost_eur, 2)
    need = required_cash_profit(amazon, min_eur=min_profit_eur, min_percent=min_profit_percent)
    vs_retail = (
        round((1 - amazon / prices.retail_eur) * 100, 1) if prices.retail_eur > 0 else None
    )
    return {
        **item,
        "cex_box_id": prices.box_id,
        "cex_title": prices.box_name,
        "cex_category": prices.category_name,
        "cex_url": prices.url,
        "cex_cash_eur": prices.cash_eur,
        "amazon_total_eur": round(total_buy, 2),
        "delivery_eur": round(delivery_eur, 2),
        "cex_break_even_buy_eur": round(max(0, expected_cash - delivery_eur - extra_cost_eur), 2),
        "cex_max_buy_eur": round(max(0, min(expected_cash - delivery_eur - extra_cost_eur - min_profit_eur, (expected_cash - extra_cost_eur)/(1 + max(min_profit_percent, min_roi_percent)/100) - delivery_eur)), 2),
        "cex_net_roi_percent": round(net_cash / total_buy * 100, 1) if total_buy > 0 else None,
        "cex_expected_cash_eur": expected_cash,
        "cex_extra_cost_eur": round(extra_cost_eur, 2),
        "cex_payout_haircut_percent": payout_haircut_percent,
        "cex_voucher_eur": prices.voucher_eur,
        "cex_retail_eur": prices.retail_eur,
        "cex_cash_profit_eur": net_cash,
        "cex_voucher_profit_eur": net_voucher,
        "cex_cash_profit_percent": round(net_cash / amazon * 100, 1) if amazon > 0 else None,
        "cex_min_profit_eur": round(need, 2),
        "cex_cash_flip": net_cash >= need and (net_cash / total_buy * 100 >= min_roi_percent if total_buy > 0 else False),
        "vs_cex_retail_percent": vs_retail,
    }


async def enrich_with_cex(
    candidates: list[dict[str, Any]],
    config: CrawlConfig,
    *,
    client: httpx.AsyncClient | None = None,
    max_lookups: int | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Look up CeX trade-in prices. Returns (all lookups, rows that pass the cash margin)."""
    if not config.cex_enabled or not candidates:
        return [], []

    cap = max_lookups if max_lookups is not None else config.cex_max_lookups
    to_check = candidates[:cap]
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(headers=cex_ie.CEX_HEADERS, timeout=20.0)

    assert client is not None
    lookups: list[dict[str, Any]] = []
    flips: list[dict[str, Any]] = []

    try:
        for item in to_check:
            # Pace every CeX request, including mismatched and early-exit hits.
            # This is intentionally not a burst scraper.
            if config.cex_request_delay_seconds:
                await asyncio.sleep(config.cex_request_delay_seconds)
            query = cex_ie.search_query_from_title(item.get("title"))
            hits = await cex_ie.search_boxes(client, query, hits_per_page=5)
            hit = cex_ie.pick_best_box_hit(item.get("title"), hits)
            if not hit:
                await asyncio.sleep(config.cex_request_delay_seconds)
                continue
            box_id = str(hit.get("boxId") or "")
            prices = await cex_ie.fetch_box_prices(client, box_id)
            if not prices or not cex_ie.titles_match_for_trade_in(item.get("title"), prices.box_name):
                await asyncio.sleep(config.cex_request_delay_seconds)
                continue
            amazon_barcode = normalise_barcode(item.get('barcode'))
            amazon_model = normalise_model(item.get('model_number'))
            if amazon_barcode and prices.barcodes and amazon_barcode not in prices.barcodes:
                continue
            if amazon_model and prices.model_number and amazon_model != prices.model_number:
                continue
            identity = ('barcode_verified' if amazon_barcode and prices.barcodes else
                        'model_verified' if amazon_model and prices.model_number else 'title_only')
            row = build_cex_row(
                item,
                prices,
                min_profit_eur=config.cex_min_cash_profit_eur,
                min_profit_percent=config.cex_min_cash_profit_percent,
                delivery_eur=config.cex_delivery_eur,
                min_roi_percent=config.cex_min_roi_percent,
                extra_cost_eur=config.cex_extra_cost_eur,
                payout_haircut_percent=config.cex_payout_haircut_percent,
            )
            row['match_confidence'] = identity
            row['amazon_barcode'] = amazon_barcode
            row['cex_barcodes'] = list(prices.barcodes)
            lookups.append(row)
            if row["cex_cash_flip"]:
                flips.append(row)
            await asyncio.sleep(config.cex_request_delay_seconds)
    finally:
        if own_client:
            await client.aclose()

    flips.sort(key=lambda r: r.get("cex_cash_profit_eur") or 0, reverse=True)
    lookups.sort(key=lambda r: r.get("cex_cash_profit_eur") or 0, reverse=True)
    return lookups, flips
