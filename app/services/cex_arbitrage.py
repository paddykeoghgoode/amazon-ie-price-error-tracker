from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.scraper import cex_ie
from app.scraper.product_identity import normalise_barcode, normalise_model
from app.services.cex_cache import (
    age_hours, cache_price, cache_valid, mark_error, quote_entry,
    retry_allowed, status_entry,
)
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


def _compatible_quote(item: dict[str, Any], prices: cex_ie.CexPrices) -> bool:
    if not cex_ie.titles_match_for_trade_in(item.get("title"), prices.box_name):
        return False
    barcode = normalise_barcode(item.get("barcode"))
    model = normalise_model(item.get("model_number"))
    if barcode and prices.barcodes and barcode not in prices.barcodes:
        return False
    if model and prices.model_number and model != prices.model_number:
        return False
    return True


def _match_level(item: dict[str, Any], prices: cex_ie.CexPrices) -> str:
    if normalise_barcode(item.get("barcode")) and prices.barcodes:
        return "barcode_verified"
    if normalise_model(item.get("model_number")) and prices.model_number:
        return "model_verified"
    return "title_only"


async def enrich_with_cex(
    candidates: list[dict[str, Any]],
    config: CrawlConfig,
    *,
    client: httpx.AsyncClient | None = None,
    max_lookups: int | None = None,
    cache: dict[str, dict] | None = None,
    run_state: dict | None = None,
    now: datetime | None = None,
    force_refresh: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Price all candidate ASINs, reusing a timestamped CeX quote when suitable.

    An Amazon price change recalculates profit from the cached CeX cash quote;
    it does NOT force a new CeX search. Positive quotes expire after 72h by
    default; no-match research after 7 days. High-margin research leads are
    refreshed directly via their CeX box ID if the quote is >12h old.
    Never alert on a quote older than that. On CeX 429, stop new CeX requests
    for the rest of the section scan rather than retrying around the limit.

    cache=None retains the old uncached behaviour for standalone clients.
    cache is modified in place and must be saved with the public catalogue.
    """
    if not config.cex_enabled or not candidates:
        return [], []
    cap = max_lookups if max_lookups is not None else config.cex_max_lookups
    to_check = candidates[:cap]
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(headers=cex_ie.CEX_HEADERS, timeout=20.0)
    assert client is not None

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    state = run_state if run_state is not None else {}
    state.setdefault("live_lookups", 0)
    state.setdefault("cache_hits", 0)
    state.setdefault("deferred", 0)
    state.setdefault("errors", 0)
    state.setdefault("rate_limited", False)
    budget = max(1, int(getattr(config, "cex_live_lookups_per_run", 40)))
    quote_ttl = float(getattr(config, "cex_quote_ttl_hours", 72))
    negative_ttl = float(getattr(config, "cex_no_match_ttl_hours", 168))
    error_ttl = float(getattr(config, "cex_error_retry_hours", 6))
    alert_ttl = float(getattr(config, "cex_alert_quote_max_age_hours", 12))
    lookups: list[dict[str, Any]] = []
    flips: list[dict[str, Any]] = []

    async def pace() -> None:
        if config.cex_request_delay_seconds > 0:
            await asyncio.sleep(config.cex_request_delay_seconds)

    async def request_quote(
        item: dict[str, Any], *, box_id: str | None = None
    ) -> cex_ie.CexPrices | None:
        # The box ID is stable across price changes; reusing it needs one call
        # instead of repeated fuzzy title searches.
        if box_id:
            await pace()
            return await cex_ie.fetch_box_prices(client, box_id, strict=cache is not None)
        query = cex_ie.search_query_from_title(item.get("title"))
        await pace()
        hits = await cex_ie.search_boxes(client, query, hits_per_page=5, strict=cache is not None)
        hit = cex_ie.pick_best_box_hit(item.get("title"), hits)
        if not hit:
            return None
        await pace()
        return await cex_ie.fetch_box_prices(
            client, str(hit.get("boxId") or ""), strict=cache is not None
        )

    try:
        for item in to_check:
            asin = str(item.get("asin") or "")
            title = item.get("title")
            prior = cache.get(asin) if cache is not None else None
            usable = cache is not None and cache_valid(
                prior, title=title, now=now, quote_ttl_hours=quote_ttl,
                no_match_ttl_hours=negative_ttl, error_retry_hours=error_ttl,
            )
            prices = cache_price(prior) if prior and prior.get("title") == title else None
            quote_source = "cache"
            quote_fresh = False

            # A negative CeX match also gets a TTL. A transient error should
            # never be permanently stored as "no matching CeX product".
            if usable and prior.get("status") in ("no_match", "error"):
                state["cache_hits"] += 1
                continue

            if usable and prices and not _compatible_quote(item, prices):
                usable = False
                prices = None

            # Cached quote can be used to recompute today's profit, but an
            # apparently profitable and eligible item requires a recent quote.
            high_interest = False
            if prices and _compatible_quote(item, prices):
                estimate = build_cex_row(
                    item, prices, min_profit_eur=config.cex_min_cash_profit_eur,
                    min_profit_percent=config.cex_min_cash_profit_percent,
                    delivery_eur=config.cex_delivery_eur,
                    min_roi_percent=config.cex_min_roi_percent,
                    extra_cost_eur=config.cex_extra_cost_eur,
                    payout_haircut_percent=config.cex_payout_haircut_percent,
                )
                # Seller checks are disabled. Refresh attractive *research*
                # leads so a stale CeX cash quote cannot trigger an alert.
                high_interest = bool(estimate["cex_cash_flip"])

            age = age_hours(prior, now) if prices else None
            quote_fresh = age is not None and age <= alert_ttl
            needs_refresh = (
                force_refresh or not usable or prices is None or
                (prior or {}).get("source") == "catalogue_search" or
                (high_interest and not quote_fresh)
            )
            if needs_refresh:
                may_request = (
                    not state["rate_limited"] and
                    (cache is None or state["live_lookups"] < budget) and
                    retry_allowed(prior, now, error_ttl)
                )
                if may_request:
                    state["live_lookups"] += 1
                    try:
                        # On a known matching SKU only fetch its detail.
                        refreshed = await request_quote(
                            item, box_id=prices.box_id if prices and _compatible_quote(item, prices) else None,
                        )
                        if refreshed and _compatible_quote(item, refreshed) and refreshed.cash_eur > 0:
                            prices = refreshed
                            if cache is not None:
                                prior = quote_entry(title, prices, now)
                                cache[asin] = prior
                            quote_source = "live"
                            quote_fresh = True
                        else:
                            # If the old SKU no longer matches the CeX detail,
                            # do not reuse its old cash payout.
                            prices = None
                            if cache is not None:
                                prior = status_entry(title, "no_match", now)
                                cache[asin] = prior
                    except httpx.HTTPStatusError as exc:
                        state["errors"] += 1
                        if exc.response.status_code in (403, 429, 503):
                            state["rate_limited"] = True
                            logger.warning(
                                "CeX returned HTTP %s; pausing further live CeX requests for this scan",
                                exc.response.status_code,
                            )
                        if cache is not None:
                            if prices and isinstance(prior, dict):
                                mark_error(prior, now)
                            else:
                                prior = status_entry(title, "error", now)
                                cache[asin] = prior
                        quote_fresh = False
                    except (httpx.HTTPError, ValueError, TypeError, OverflowError) as exc:
                        state["errors"] += 1
                        logger.warning("CeX lookup unavailable for ASIN %s: %s", asin, type(exc).__name__)
                        if cache is not None:
                            if prices and isinstance(prior, dict):
                                mark_error(prior, now)
                            else:
                                prior = status_entry(title, "error", now)
                                cache[asin] = prior
                        quote_fresh = False
                else:
                    state["deferred"] += 1

            if not prices or not _compatible_quote(item, prices):
                continue
            age = age_hours(prior, now) if cache is not None else 0
            quote_fresh = bool(age is not None and age <= alert_ttl and
                               retry_allowed(prior, now, error_ttl) and
                               (prior or {}).get("source") != "catalogue_search")
            row = build_cex_row(
                item, prices,
                min_profit_eur=config.cex_min_cash_profit_eur,
                min_profit_percent=config.cex_min_cash_profit_percent,
                delivery_eur=config.cex_delivery_eur,
                min_roi_percent=config.cex_min_roi_percent,
                extra_cost_eur=config.cex_extra_cost_eur,
                payout_haircut_percent=config.cex_payout_haircut_percent,
            )
            row["match_confidence"] = _match_level(item, prices)
            row["amazon_barcode"] = normalise_barcode(item.get("barcode"))
            row["cex_barcodes"] = list(prices.barcodes)
            row["cex_quote_checked_at"] = (prior or {}).get("checked_at") if cache is not None else now.isoformat()
            row["cex_quote_source"] = quote_source
            row["cex_quote_fresh_for_alert"] = quote_fresh
            # Stale quotes are research estimates, never actionable flips.
            if not quote_fresh:
                row["cex_cash_flip"] = False
            lookups.append(row)
            if row["cex_cash_flip"]:
                flips.append(row)
            if usable and quote_source == "cache":
                state["cache_hits"] += 1
    finally:
        if own_client:
            await client.aclose()

    flips.sort(key=lambda r: r.get("cex_cash_profit_eur") or 0, reverse=True)
    lookups.sort(key=lambda r: r.get("cex_cash_profit_eur") or 0, reverse=True)
    return lookups, flips
