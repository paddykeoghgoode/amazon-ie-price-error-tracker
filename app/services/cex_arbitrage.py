from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

import httpx

from app.scraper import cex_ie

if TYPE_CHECKING:
    from app.services.bestsellers import CrawlConfig

logger = logging.getLogger(__name__)


def required_cash_profit(amazon_price: float, *, min_eur: float, min_percent: float) -> float:
    return max(min_eur, amazon_price * min_percent / 100)


def build_cex_row(
    item: dict[str, Any],
    prices: cex_ie.CexPrices,
    *,
    min_profit_eur: float,
    min_profit_percent: float,
) -> dict[str, Any]:
    amazon = float(item["price_eur"])
    net_cash = round(prices.cash_eur - amazon, 2)
    net_voucher = round(prices.voucher_eur - amazon, 2)
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
        "cex_voucher_eur": prices.voucher_eur,
        "cex_retail_eur": prices.retail_eur,
        "cex_cash_profit_eur": net_cash,
        "cex_voucher_profit_eur": net_voucher,
        "cex_cash_profit_percent": round(net_cash / amazon * 100, 1) if amazon > 0 else None,
        "cex_min_profit_eur": round(need, 2),
        "cex_cash_flip": net_cash >= need,
        "vs_cex_retail_percent": vs_retail,
    }


async def enrich_with_cex(
    candidates: list[dict[str, Any]],
    config: CrawlConfig,
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Look up CeX trade-in prices for Amazon drops. Returns (all lookups, cash flips)."""
    if not config.cex_enabled or not candidates:
        return [], []

    cap = config.cex_max_lookups
    to_check = candidates[:cap]
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(headers=cex_ie.CEX_HEADERS, timeout=20.0)

    assert client is not None
    lookups: list[dict[str, Any]] = []
    flips: list[dict[str, Any]] = []

    try:
        for item in to_check:
            query = cex_ie.search_query_from_title(item.get("title"))
            hits = await cex_ie.search_boxes(client, query, hits_per_page=3)
            if not hits:
                await asyncio.sleep(config.cex_request_delay_seconds)
                continue
            box_id = str(hits[0].get("boxId") or "")
            prices = await cex_ie.fetch_box_prices(client, box_id)
            if not prices:
                await asyncio.sleep(config.cex_request_delay_seconds)
                continue
            row = build_cex_row(
                item,
                prices,
                min_profit_eur=config.cex_min_cash_profit_eur,
                min_profit_percent=config.cex_min_cash_profit_percent,
            )
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
