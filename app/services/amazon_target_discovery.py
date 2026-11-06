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
    """Strict trade-in-compatible hits (legacy alert path)."""
    return [row for row in parse_search_scored(html, prices, limit=limit)
            if row.get("match_percent", 0) >= 72 and cex_ie.titles_match_for_trade_in(
                row.get("title"), prices.box_name, cex_category_name=prices.category_name)][:limit]


def parse_search_scored(html: str, prices, *, limit=5) -> list[dict]:
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
        if relevance({"title": title})[1] == "explicitly_excluded_brand":
            continue
        score = cex_ie.title_match_score(title, prices.box_name, cex_category_name=prices.category_name)
        if score < 25:
            continue
        price = None
        for el in card.select('.a-price:not(.a-text-price) .a-offscreen'):
            text = el.get_text(" ", strip=True)
            if "€" in text and not re.search(r"[$£]", text):
                price = _parse_price(text)
                if price and price > 0:
                    break
        seen.add(asin)
        rows.append({"asin": asin, "title": title, "price_eur": price,
                     "url": normalize_product_url(asin), "active": True,
                     "match_percent": score,
                     "discovery_source": "cex_target_search", "cex_target_box_id": prices.box_id})
    rows.sort(key=lambda r: (-int(r.get("match_percent") or 0), r["asin"]))
    return rows[:limit]


def product_key(title):
    """Group condition grades only; preserve storage, colour, edition and bundles."""
    title = re.sub(r",\s*(?:[ABC]|boxed|unboxed|discounted|mint)\s*$", "", title or "", flags=re.I)
    return re.sub(r"\s+", " ", title).strip().casefold()


def candidate_matches(row, prices):
    if (not re.fullmatch(r"[A-Z0-9]{10}", row.get('asin') or '')
            or row.get('active') is False or row.get('identity_status') == 'identity_mismatch'
            or relevance(row)[1] == 'explicitly_excluded_brand'):
        return False
    return cex_ie.titles_match_for_trade_in(
        row.get('title') or '', prices.box_name, cex_category_name=prices.category_name)


def search_query(prices, record) -> str:
    query = (prices.barcodes[0].lstrip("0") if prices.barcodes else
             f"{cex_ie.leading_brand(prices.box_name) or ''} {prices.model_number}".strip()
             if prices.model_number else product_key(prices.box_name))
    hint = cex_ie.platform_hint_from_category(prices.category_name)
    if hint and hint.casefold() not in query.casefold():
        query = f"{query} {hint}".strip()
    return query


async def _hydrate_cex_detail(state, box_id, record, prices, client, config, run_state, group, prior, now, remember):
    if record.get("source") != "catalogue_search":
        return prices, record, None
    if run_state.get("live_lookups", 0) >= config.cex_live_lookups_per_run:
        return None, record, "cex_budget_exhausted"

    run_state["live_lookups"] = run_state.get("live_lookups", 0) + 1
    try:
        await asyncio.sleep(config.cex_request_delay_seconds)
        prices = await cex_ie.fetch_box_prices(client, box_id, strict=True)
        if not prices or prices.cash_eur <= float(state.get('cash_floor_eur', 10)):
            remember({"checked_at": now.isoformat(), "status": "cex_unavailable"})
            return None, record, "cex_unavailable"
        record = {**quote_entry(prices.box_name, prices, now), "source": "box_detail"}
        state["items"][box_id] = record
        return prices, record, None
    except (httpx.HTTPError, ValueError, TypeError, OverflowError) as exc:
        remember({**prior, "last_error_at": now.isoformat(), "status": "cex_read_failed"})
        run_state["errors"] = run_state.get("errors", 0) + 1
        if failure_reason(exc) == "provider_blocked":
            run_state["rate_limited"] = True
        return None, record, "cex_read_failed"


async def _propose_one_target(
    state, config, client, run_state, *, existing_rows=(), now, box_id, record,
    proposal_retry_hours: float, min_catalogue_percent: int, min_display_percent: int,
):
    stats = {"search_requests": 0, "candidates": 0, "status": "no_due_target"}
    history = state.setdefault("amazon_discovery", {})
    mapped = {r.get("cex_box_id") for r in state.get("mappings", {}).values()}
    user_linked = set((state.get("user_links") or {}).keys())
    if box_id in mapped or box_id in user_linked:
        return [], {**stats, "status": "already_linked"}
    groups = {}
    for key, item in state.get('items', {}).items():
        groups.setdefault(product_key((item.get('quote') or {}).get('box_name')), []).append(key)
    group = groups[product_key((record.get('quote') or {}).get('box_name'))]
    prior = history.get(box_id) or {}

    def remember(value):
        for key in group:
            history[key] = copy.deepcopy(value)

    prices = cache_price(record)
    if not prices:
        return [], stats
    prices, record, hydrate_status = await _hydrate_cex_detail(
        state, box_id, record, prices, client, config, run_state, group, prior, now, remember)
    if hydrate_status:
        return [], {**stats, "status": hydrate_status}
    query = search_query(prices, record)
    inventory = {}
    for row in existing_rows:
        old = inventory.get(row.get('asin'))
        if old is None or (row.get('last_seen_at') or '') >= (old.get('last_seen_at') or ''):
            inventory[row.get('asin')] = row
    rows = [copy.deepcopy(r) for r in inventory.values() if candidate_matches(r, prices)][:2]
    scored: list[dict] = []
    try:
        if not rows:
            stats['search_requests'] = 1
            await asyncio.sleep(config.request_delay_seconds)
            response = await client.get(f"{AMAZON_IE_BASE}/s?{urlencode({'k': query})}", follow_redirects=True)
            response.raise_for_status()
            scored = parse_search_scored(response.text, prices, limit=5)
            rows = [r for r in scored if r.get("match_percent", 0) >= min_catalogue_percent][:2]
        best = None
        if scored:
            candidate = max(scored, key=lambda r: int(r.get("match_percent") or 0))
            score = int(candidate.get("match_percent") or 0)
            if (score >= min_display_percent
                    and cex_ie.titles_match_for_trade_in(
                        candidate.get("title"), prices.box_name, cex_category_name=prices.category_name)):
                best = candidate
        if not best and rows:
            scored_row = {**rows[0], "match_percent": cex_ie.title_match_score(
                rows[0].get("title"), prices.box_name, cex_category_name=prices.category_name)}
            if (scored_row["match_percent"] >= min_display_percent
                    and cex_ie.titles_match_for_trade_in(
                        scored_row.get("title"), prices.box_name, cex_category_name=prices.category_name)):
                best = scored_row
        proposals = state.setdefault("proposed_links", {})
        if best:
            proposals[box_id] = {
                "asin": best["asin"],
                "amazon_title": best.get("title"),
                "match_percent": int(best.get("match_percent") or 0),
                "query": query,
                "proposed_at": now.isoformat(),
                "status": "proposed",
            }
        elif box_id in proposals:
            proposals.pop(box_id, None)
        candidate_payload = scored[:3] if scored else [
            {k: r[k] for k in ('asin', 'title', 'url', 'price_eur', 'price_history', 'last_seen_at') if k in r}
            | {"match_percent": cex_ie.title_match_score(
                r.get("title"), prices.box_name, cex_category_name=prices.category_name)} for r in rows[:3]
        ]
        for row in rows:
            reused = row['asin'] in inventory
            row.update(cex_target_box_id=box_id,
                       cex_quote_cache={**copy.deepcopy(record), "title": row["title"]})
            if not reused:
                row.update(first_seen_at=now.isoformat(), last_seen_at=now.isoformat(),
                           category="CeX target search", verification_status="disabled",
                           identity_status="not_checked", last_checked_at=None)
                row['price_history'] = record_price([], row.get('price_eur'), now.isoformat(), 'search_card') if row.get('price_eur') else []
        remember({"checked_at": now.isoformat(), "status": "candidates_found" if rows else "no_candidates",
                  "query": query, "candidates": len(rows),
                  "best_match_percent": int(best.get("match_percent") or 0) if best else 0,
                  "candidate_products": candidate_payload})
        stats.update(status=history[box_id]["status"], candidates=len(rows))
        return rows, stats
    except (httpx.HTTPError, ValueError, TypeError, OverflowError) as exc:
        reason = failure_reason(exc)
        remember({**prior, "last_error_at": now.isoformat(), "status": reason})
        if reason == "provider_blocked":
            state["amazon_blocked_until"] = (now + timedelta(minutes=30)).isoformat()
        return [], {**stats, "status": reason}


async def propose_amazon_links(
    state, config, client, run_state, *, existing_rows=(), now=None,
    max_searches: int = 1, proposal_retry_hours: float = 12.0, min_catalogue_percent: int = 72,
    min_display_percent: int = 40,
):
    """Search Amazon.ie from CeX titles; store scored proposals and optional catalogue rows."""
    now = now or datetime.now(timezone.utc)
    stats = {"search_requests": 0, "candidates": 0, "proposals": 0, "status": "no_due_target"}
    try:
        until = datetime.fromisoformat(state.get("amazon_blocked_until") or "")
        if until.tzinfo and now < until:
            return [], {**stats, "status": "provider_cooldown"}
    except ValueError:
        pass
    if config.premium_spot_check_limit < 3 or run_state.get("rate_limited"):
        return [], stats
    mapped = {r.get("cex_box_id") for r in state.get("mappings", {}).values()}
    user_linked = set((state.get("user_links") or {}).keys())
    groups = {}
    for key, record in state.get('items', {}).items():
        groups.setdefault(product_key((record.get('quote') or {}).get('box_name')), []).append(key)
    due = []
    history = state.setdefault("amazon_discovery", {})
    for box_id, record in state.get("items", {}).items():
        prices = cache_price(record)
        group = groups[product_key((record.get('quote') or {}).get('box_name'))]
        prior = max((history.get(key) or {} for key in group),
                    key=lambda r: max(r.get('checked_at') or '', r.get('last_error_at') or ''))
        age = age_hours(prior, now)
        if (not prices or box_id in mapped or box_id in user_linked
                or (age is not None and age < proposal_retry_hours)
                or not retry_allowed(prior, now, config.cex_error_retry_hours)):
            continue
        due.append((prior.get("checked_at") or "", -prices.cash_eur, box_id, record))
    if not due:
        return [], stats
    discovered: list[dict] = []
    attempts = 0
    for _, _, box_id, record in sorted(due):
        if attempts >= max(1, int(max_searches)):
            break
        if run_state.get("rate_limited"):
            break
        rows, one = await _propose_one_target(
            state, config, client, run_state, existing_rows=existing_rows, now=now,
            box_id=box_id, record=record, proposal_retry_hours=proposal_retry_hours,
            min_catalogue_percent=min_catalogue_percent, min_display_percent=min_display_percent,
        )
        attempts += 1
        stats["search_requests"] += one.get("search_requests", 0)
        stats["candidates"] += one.get("candidates", 0)
        stats["status"] = one.get("status") or stats["status"]
        if one.get("status") in ("provider_blocked", "cex_budget_exhausted", "provider_cooldown"):
            break
        if state.get("proposed_links", {}).get(box_id):
            stats["proposals"] += 1
        discovered.extend(rows)
    return discovered, stats


async def discover_amazon_targets(state, config, client, run_state, *, existing_asins=(), existing_rows=(), now=None):
    """One CeX box per call (tests and legacy callers)."""
    return await propose_amazon_links(
        state, config, client, run_state, existing_rows=existing_rows, now=now, max_searches=1,
    )
