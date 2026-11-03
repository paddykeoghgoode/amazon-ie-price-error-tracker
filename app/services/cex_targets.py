"""Incremental CeX-first catalogue and persistent exact ASIN mappings.

Discovery and quote maintenance spend the SAME live lookup allowance as broad
research. Search-index cash values are provisional: only box detail quotes can
support an alert. This state travels inside the existing published catalogue.
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.scraper import cex_ie
from app.scraper.product_identity import catalog_barcodes, normalise_model
from app.services.alert_policy import load_alert_policy
from app.services.cex_arbitrage import _compatible_quote, _match_level, build_cex_row, enrich_with_cex
from app.services.cex_cache import age_hours, cache_price, quote_entry, retry_allowed
from app.services.resale_relevance import relevance


@dataclass(frozen=True)
class TargetConfig:
    enabled: bool = False
    max_items: int = 300
    max_mappings: int = 100
    min_cash_eur: float = 150
    searches_per_run: int = 2
    refreshes_per_run: int = 2
    query_refresh_hours: float = 24
    queries: tuple[str, ...] = ()


def load_target_config(path: str | Path = "bestsellers.json") -> TargetConfig:
    raw = json.loads(Path(path).read_text(encoding="utf-8")).get("cex_first") or {}
    return TargetConfig(
        enabled=raw.get("enabled") is True,
        max_items=max(1, min(500, int(raw.get("max_items", 300)))),
        max_mappings=max(1, min(100, int(raw.get("max_mappings", 100)))),
        min_cash_eur=max(1, float(raw.get("min_cash_eur", 150))),
        searches_per_run=max(0, min(2, int(raw.get("searches_per_run", 2)))),
        refreshes_per_run=max(0, min(2, int(raw.get("refreshes_per_run", 2)))),
        query_refresh_hours=max(24, float(raw.get("query_refresh_hours", 24))),
        queries=tuple(dict.fromkeys(q.strip() for q in raw.get("queries", []) if isinstance(q, str) and q.strip())),
    )


def target_state(published: dict) -> dict:
    state = copy.deepcopy(published.get("cex_targets") or {})
    for name in ("items", "queries", "mappings"):
        state.setdefault(name, {})
    return state


def exact_target_match(item, prices):
    """A model alone must not hide a quoted colour or network variant."""
    if not _compatible_quote(item, prices) or _match_level(item, prices) == "title_only":
        return False
    accessory = re.compile(r"\b(?:replacement|compatible with|case for|cover for|ear\s*pads?|ear\s*cushions?)\b", re.I)
    if accessory.search(item.get("title") or "") and not accessory.search(prices.box_name):
        return False
    def variants(title):
        tokens = set(re.findall(r"[a-z0-9]+", (title or "").lower().replace("gray", "grey")))
        return tokens & {"black", "white", "grey", "silver", "gold", "blue", "red", "green", "pink", "purple"}, tokens & {"unlocked", "vodafone", "three", "o2", "ee", "eir"}
    amazon_colour, amazon_network = variants(item.get("title"))
    cex_colour, cex_network = variants(prices.box_name)
    if (amazon_colour and cex_colour and amazon_colour != cex_colour) or (amazon_network and cex_network and amazon_network != cex_network):
        return False
    if _match_level(item, prices) == "model_verified":
        return (not cex_colour or amazon_colour == cex_colour) and (not cex_network or amazon_network == cex_network)
    return True


def _recent(stamp, now, hours=720):
    age = age_hours({"checked_at": stamp}, now)
    return age is not None and age <= hours


def _cache_version(record):
    # A failed refresh updates last_error_at without changing the old quote's
    # checked_at. Never erase that backoff by importing an older section copy.
    events = [datetime.min.replace(tzinfo=timezone.utc)]
    for key in ("checked_at", "last_error_at"):
        try:
            stamp = datetime.fromisoformat((record or {}).get(key) or "")
            if stamp.tzinfo is not None:
                events.append(stamp.astimezone(timezone.utc))
        except (TypeError, ValueError):
            pass
    return max(events)


def prune_targets(state, options, now):
    for field, stamp in (("items", "checked_at"), ("mappings", "last_seen_at")):
        rows = [r for r in state[field].values() if _recent(r.get(stamp), now)]
        rows.sort(key=lambda r: r.get(stamp) or "", reverse=True)
        key = (lambda r: r["quote"]["box_id"]) if field == "items" else (lambda r: r["asin"])
        limit = options.max_items if field == "items" else options.max_mappings
        state[field] = {key(r): r for r in rows[:limit]}
    state["generated_at"] = now.isoformat()


async def discover_targets(state, options, config, client, run_state, *, now=None):
    """At most two cached, paged category searches, never a whole-site scrape."""
    now = now or datetime.now(timezone.utc)
    if not options.enabled or not config.cex_enabled:
        return
    queries = sorted(options.queries, key=lambda q: state["queries"].get(q, {}).get("checked_at") or "")
    used = 0
    for query in queries:
        prior = state["queries"].get(query) or {}
        if _recent(prior.get("checked_at"), now, options.query_refresh_hours):
            continue
        if _recent(prior.get("last_error_at"), now, config.cex_error_retry_hours):
            continue
        if (used >= options.searches_per_run or run_state.get("rate_limited") or
                run_state.get("live_lookups", 0) >= config.cex_live_lookups_per_run):
            break
        used += 1
        run_state["live_lookups"] = run_state.get("live_lookups", 0) + 1
        run_state["target_searches"] = run_state.get("target_searches", 0) + 1
        try:
            await asyncio.sleep(config.cex_request_delay_seconds)
            page = int(prior.get("next_page", 0))
            hits = await cex_ie.search_boxes(client, query, hits_per_page=25, page=page,
                                            min_cash_eur=options.min_cash_eur, strict=True)
            for hit in hits:
                if not isinstance(hit, dict) or str(hit.get("boxWebBuyAllowed", "")).lower() not in ("1", "true"):
                    continue
                box_id, title = str(hit.get("boxId") or ""), str(hit.get("boxName") or "")
                try:
                    cash = float(hit.get("cashBuyPrice") or 0)
                    retail = float(hit.get("sellPrice") or 0)
                except (ValueError, TypeError, OverflowError):
                    continue
                if not box_id or not title or not options.min_cash_eur <= cash < float("inf"):
                    continue
                prices = cex_ie.CexPrices(box_id, title, hit.get("categoryName"), cash, 0,
                                         retail, cex_ie.product_url(box_id), catalog_barcodes(hit),
                                         normalise_model(hit.get("modelNumber")))
                state["items"][box_id] = {**quote_entry(title, prices, now),
                                          "source": "catalogue_search", "query": query}
            state["queries"][query] = {"checked_at": now.isoformat(),
                                        "next_page": (page + 1) % 3 if len(hits) >= 25 else 0}
        except (httpx.HTTPError, ValueError, TypeError, OverflowError) as exc:
            state["queries"][query] = {**prior, "last_error_at": now.isoformat()}
            run_state["errors"] = run_state.get("errors", 0) + 1
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in (403, 429, 503):
                run_state["rate_limited"] = True
    prune_targets(state, options, now)


def attach_target_quotes(items, state):
    """Use the target index to select a box, without repeating fuzzy searches.

    A compatible title only earns a detail-research slot, never an exact map.
    When several grades match, use the lowest cash quote, not the best grade.
    """
    targets = [(record, cache_price(record)) for record in state["items"].values()]
    for row in items:
        row.pop("cex_target_box_id", None)
        if (not row.get("active", True) or not row.get("price_eur") or
                relevance(row)[1] == "explicitly_excluded_brand"):
            continue
        mapping = state["mappings"].get(row["asin"])
        if mapping and mapping.get("title") == row.get("title"):
            for key in ("barcode", "model_number", "identity_checked_at", "identity_status", "last_cex_alert"):
                if key in mapping and key not in row:
                    row[key] = mapping[key]
            cached = mapping.get("cex_quote_cache") or {}
            if _cache_version(cached) > _cache_version(row.get("cex_quote_cache")):
                row["cex_quote_cache"] = copy.deepcopy(cached)
        brand = cex_ie.leading_brand(row.get("title"))
        matches = [(record, prices) for record, prices in targets
                   if prices and (not brand or len(brand) < 4 or brand in prices.box_name.lower())
                   and _compatible_quote(row, prices)]
        if not matches:
            continue
        matches.sort(key=lambda pair: (_match_level(row, pair[1]) == "title_only", pair[1].cash_eur, pair[1].box_id))
        record, prices = matches[0]
        row["cex_target_box_id"] = prices.box_id
        # Do not replace a detail quote or its retry backoff with an index hit.
        existing = row.get("cex_quote_cache") or {}
        existing_price = cache_price(existing)
        if existing.get("status") == "error" or existing.get("last_error_at"):
            continue
        if existing_price and (existing_price.box_id == prices.box_id or existing_price.cash_eur <= prices.cash_eur):
            continue
        row["cex_quote_cache"] = {**copy.deepcopy(record), "title": row.get("title")}


def sync_mappings(state, sections, options, *, now=None):
    """Save explicit exact identity evidence, including non-profitable products."""
    now = now or datetime.now(timezone.utc)
    latest = {}
    for section in sections:
        for row in section.get("items") or []:
            asin = row.get("asin")
            if asin and (row.get("last_seen_at") or "") >= (latest.get(asin, {}).get("last_seen_at") or ""):
                latest[asin] = row
    for asin, row in latest.items():
        if relevance(row)[1] == "explicitly_excluded_brand":
            state["mappings"].pop(asin, None)
            continue
        prior = state["mappings"].get(asin) or {}
        if prior and row.get("last_seen_at", "") < prior.get("last_seen_at", ""):
            continue
        if prior and (row.get("title") != prior.get("title") or row.get("identity_status") == "identity_mismatch"):
            state["mappings"].pop(asin, None)
            prior = {}
        cache = row.get("cex_quote_cache") or {}
        # Quote maintenance can be newer than this section's Amazon observation.
        if _cache_version(prior.get("cex_quote_cache")) > _cache_version(cache):
            cache = prior["cex_quote_cache"]
        prices = cache_price(cache)
        if (not prices or cache.get("source") == "catalogue_search" or
                prices.cash_eur < options.min_cash_eur or not exact_target_match(row, prices) or
                not row.get("identity_checked_at")):
            continue
        state["mappings"][asin] = {
            **copy.deepcopy(row), "cex_quote_cache": copy.deepcopy(cache),
            "cex_box_id": prices.box_id, "match_confidence": _match_level(row, prices),
            "mapped_at": prior.get("mapped_at") or now.isoformat(),
        }
    prune_targets(state, options, now)


async def refresh_mapped_quotes(state, options, config, client, run_state, *, now=None):
    """Refresh known boxes before their 12h alert deadline; no Amazon requests."""
    now = now or datetime.now(timezone.utc)
    if not options.enabled or not config.cex_enabled:
        return
    rows = sorted(state["mappings"].values(),
                  key=lambda r: (r.get("cex_quote_cache") or {}).get("checked_at") or "")
    used = 0
    refreshed_boxes = {}
    for row in rows:
        cache = row.get("cex_quote_cache") or {}
        box_id = row.get("cex_box_id")
        if box_id in refreshed_boxes:
            row["cex_quote_cache"] = {**copy.deepcopy(refreshed_boxes[box_id]), "title": row.get("title")}
            continue
        age = age_hours(cache, now)
        if (age is not None and age < config.cex_alert_quote_max_age_hours / 2) or not retry_allowed(cache, now, config.cex_error_retry_hours):
            continue
        if (used >= options.refreshes_per_run or run_state.get("rate_limited") or
                run_state.get("live_lookups", 0) >= config.cex_live_lookups_per_run):
            continue
        used += 1
        quotes = {row["asin"]: cache}
        # Force refresh through the existing error/backoff/budget implementation.
        await enrich_with_cex([row], config, client=client, max_lookups=1,
                              cache=quotes, run_state=run_state, now=now, force_refresh=True)
        row["cex_quote_cache"] = quotes[row["asin"]]
        refreshed_boxes[box_id] = quotes[row["asin"]]
        run_state["mapped_quote_refreshes"] = run_state.get("mapped_quote_refreshes", 0) + 1


def watch_rows(state, config, *, now=None):
    now = now or datetime.now(timezone.utc)
    policy = load_alert_policy()
    rows = []
    for mapping in state.get("mappings", {}).values():
        if relevance(mapping)[1] == "explicitly_excluded_brand":
            continue
        cache = mapping.get("cex_quote_cache") or {}
        prices = cache_price(cache)
        if (not prices or cache.get("source") == "catalogue_search" or
                not _recent(mapping.get("last_seen_at"), now) or not exact_target_match(mapping, prices)):
            continue
        row = build_cex_row(mapping, prices,
                            min_profit_eur=max(config.cex_min_cash_profit_eur, policy.min_cex_cash_profit_eur),
                            min_profit_percent=config.cex_min_cash_profit_percent,
                            min_roi_percent=max(config.cex_min_roi_percent, policy.min_cex_roi_percent),
                            delivery_eur=config.cex_delivery_eur, extra_cost_eur=config.cex_extra_cost_eur,
                            payout_haircut_percent=config.cex_payout_haircut_percent)
        age = age_hours(cache, now)
        row.update(match_confidence=_match_level(mapping, prices),
                   cex_quote_checked_at=cache.get("checked_at"),
                   cex_quote_fresh_for_alert=bool(age is not None and age <= config.cex_alert_quote_max_age_hours
                                                and retry_allowed(cache, now, config.cex_error_retry_hours)),
                   scan_lane="cex_target")
        rows.append(row)
    return rows


def target_funnel(state, config, *, now=None):
    rows = watch_rows(state, config, now=now)
    return {"catalogue_skus": len(state.get("items", {})), "exact_mappings": len(rows),
            "fresh_quotes": sum(r["cex_quote_fresh_for_alert"] for r in rows),
            "stale_quotes": sum(not r["cex_quote_fresh_for_alert"] for r in rows)}
