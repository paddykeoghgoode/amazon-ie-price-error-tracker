"""Bounded CeX-to-Amazon discovery, charged to the existing six-page allowance."""
from __future__ import annotations

import asyncio
import copy
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from bs4 import BeautifulSoup
import httpx

from app.scraper.amazon_ie import AMAZON_IE_BASE, _parse_price, blocked_html, failure_reason, normalize_product_url
from app.scraper import cex_ie
from app.services.cex_cache import age_hours, cache_price, quote_entry, retry_allowed
from app.services.resale_relevance import relevance
from app.services.public_price_history import record_price


def parse_search_results(html: str, prices, *, limit=2) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    if blocked_html(soup):
        from app.scraper.amazon_ie import AmazonBlockedError
        raise AmazonBlockedError("Amazon search returned a challenge page")
    if not soup.select_one('[data-component-type="s-search-result"]') and not re.search(
            r'no results for|did not match any products', soup.get_text(' ', strip=True), re.I):
        raise ValueError('Amazon search layout not recognised')
    rows, seen = [], set()
    for card in soup.select('[data-component-type="s-search-result"][data-asin]'):
        asin = card.get("data-asin", "")
        title = card.select_one("h2")
        if not re.fullmatch(r"[A-Z0-9]{10}", asin) or not title or asin in seen:
            continue
        title = title.get_text(" ", strip=True)
        if not cex_ie.titles_match_for_trade_in(title, prices.box_name):
            continue
        if relevance({"title": title})[1] == "explicitly_excluded_brand":
            continue
        price = None
        for el in card.select('.a-price:not(.a-text-price) .a-offscreen'):
            text = el.get_text(" ", strip=True)
            if "€" in text and not re.search(r"[$£]", text):
                price = _parse_price(text)
                if price and price > 0:
                    break
        if not price or price <= 0:
            continue
        seen.add(asin)
        rows.append({"asin": asin, "title": title, "price_eur": price,
                     "url": normalize_product_url(asin), "active": True,
                     "discovery_source": "cex_target_search", "cex_target_box_id": prices.box_id})
        if len(rows) >= limit:
            break
    return rows


async def discover_amazon_targets(state, config, client, run_state, *, existing_asins=(), now=None):
    """One search, at most two candidates; no extra workflow or request allowance.

    A search hit is only a research candidate. Alert eligibility still needs a
    current detail-page price, compatible explicit identity and fresh CeX cash.
    """
    now = now or datetime.now(timezone.utc)
    stats = {"search_requests": 0, "candidates": 0, "status": "no_due_target"}
    history = state.setdefault("amazon_discovery", {})
    # Bound persistence alongside the catalogue; expired SKUs cannot grow state.
    for key in set(history) - set(state.get("items", {})):
        history.pop(key, None)
    try:
        until = datetime.fromisoformat(state.get("amazon_blocked_until") or "")
        if until.tzinfo and now < until:
            return [], {**stats, "status": "provider_cooldown"}
    except ValueError:
        pass
    mapped = {r.get("cex_box_id") for r in state.get("mappings", {}).values()}
    due = []
    for box_id, record in state.get("items", {}).items():
        prices = cache_price(record)
        prior = history.get(box_id) or {}
        age = age_hours(prior, now)
        if (not prices or box_id in mapped or (age is not None and age < 24)
                or not retry_allowed(prior, now, config.cex_error_retry_hours)):
            continue
        due.append((prior.get("checked_at") or "", -prices.cash_eur, box_id, record))
    if not due or config.premium_spot_check_limit < 3 or run_state.get("rate_limited"):
        return [], stats
    _, _, box_id, record = sorted(due)[0]
    prior = history.get(box_id) or {}
    prices = cache_price(record)
    # Index hits lack CeX's attributeInfo. Hydrate one SKU from the same live
    # lookup budget, giving discovery a chance to obtain a real model number.
    if record.get("source") == "catalogue_search":
        if run_state.get("live_lookups", 0) >= config.cex_live_lookups_per_run:
            return [], {**stats, "status": "cex_budget_exhausted"}
        run_state["live_lookups"] = run_state.get("live_lookups", 0) + 1
        try:
            await asyncio.sleep(config.cex_request_delay_seconds)
            prices = await cex_ie.fetch_box_prices(client, box_id, strict=True)
            if not prices or prices.cash_eur <= float(state.get('cash_floor_eur', 10)):
                history[box_id] = {"checked_at": now.isoformat(), "status": "cex_unavailable"}
                return [], {**stats, "status": "cex_unavailable"}
            record = {**quote_entry(prices.box_name, prices, now), "source": "box_detail"}
            state["items"][box_id] = record
        except (httpx.HTTPError, ValueError, TypeError, OverflowError) as exc:
            history[box_id] = {**prior, "last_error_at": now.isoformat(), "status": "cex_read_failed"}
            run_state["errors"] = run_state.get("errors", 0) + 1
            if failure_reason(exc) == "provider_blocked":
                run_state["rate_limited"] = True
            return [], {**stats, "status": "cex_read_failed"}
    # Never infer an EAN from a box ID. Explicit identifiers are preferred;
    # product-name queries remain provisional and cannot create exact mappings.
    query = (prices.barcodes[0].lstrip("0") if prices.barcodes else
             f"{cex_ie.leading_brand(prices.box_name) or ''} {prices.model_number}" if prices.model_number else
             re.sub(r",\s*(?:[ABC]|boxed|unboxed|discounted)\s*$", "", prices.box_name, flags=re.I))
    stats["search_requests"] = 1
    try:
        await asyncio.sleep(config.request_delay_seconds)
        response = await client.get(f"{AMAZON_IE_BASE}/s?{urlencode({'k': query})}", follow_redirects=True)
        response.raise_for_status()
        rows = parse_search_results(response.text, prices)
        rows = [r for r in rows if r["asin"] not in set(existing_asins)]
        for row in rows:
            row.update(cex_quote_cache={**copy.deepcopy(record), "title": row["title"]},
                       first_seen_at=now.isoformat(), last_seen_at=now.isoformat(),
                       category="CeX target search", verification_status="disabled",
                       identity_status="not_checked", last_checked_at=None)
            row['price_history'] = record_price([], row['price_eur'], now.isoformat(), 'search_card')
        history[box_id] = {"checked_at": now.isoformat(), "status": "candidates_found" if rows else "no_new_candidates",
                           "query": query, "candidates": len(rows)}
        stats.update(status=history[box_id]["status"], candidates=len(rows))
        return rows, stats
    except (httpx.HTTPError, ValueError, TypeError, OverflowError) as exc:
        reason = failure_reason(exc)
        history[box_id] = {**prior, "last_error_at": now.isoformat(), "status": reason}
        if reason == "provider_blocked":
            state["amazon_blocked_until"] = (now + timedelta(minutes=30)).isoformat()
        return [], {**stats, "status": reason}
