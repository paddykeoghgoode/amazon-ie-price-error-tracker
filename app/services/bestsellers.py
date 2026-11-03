import asyncio
import json
import logging
from collections import deque
from datetime import datetime, timezone
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app import database as db
from app.scraper.amazon_ie import AMAZON_IE_BASE, ProductSnapshot, is_eligible_amazon_offer, eligible_offer_type, fetch_product
from app.scraper.bestsellers import BestsellerItem, normalize_category_path, parse_bestseller_page
from app.services.alert_policy import (
    load_alert_policy, qualifies_cex_alert, qualifies_observed_drop,
)
from app.services.premium_drops import (
    build_lead_alert,
    observe_premium_history_drop,
    observe_rapid_scan_drop,
    observation_to_row,
)
from app.services.drop_tiers import drop_tier
from app.services.monitor import DetectedAlert, notify_alert, record_and_detect, send_notification

logger = logging.getLogger(__name__)

Fetcher = Callable[[str], Awaitable[tuple[int, str]]]

# Several failed pages in a row almost always means Amazon is throttling us.
MAX_CONSECUTIVE_ERRORS = 8


@dataclass
class CategoryRoot:
    path: str
    depth: int = 1
    name: str | None = None
    pages: int | None = None


@dataclass
class CrawlConfig:
    roots: list[CategoryRoot]
    exclude: set[str] = field(default_factory=set)
    max_items: int = 10000
    max_requests: int = 650
    pages_per_category: int = 2
    request_delay_seconds: float = 1.5
    max_alerts_per_run: int = 15
    alert_cooldown_minutes: int = 1440
    history_per_product: int = 20
    baseline_max_age_days: int = 30
    report_drop_percent: float = 30.0
    general_drop_percent: float = 10.0
    premium_min_price_eur: float = 100.0
    premium_history_drop_percent: float = 45.0
    rapid_drop_min_price_eur: float = 80.0
    rapid_drop_percent: float = 20.0
    rapid_drop_min_saving_eur: float = 30.0
    premium_spot_check_limit: int = 6
    premium_alerts_per_run: int = 8
    discovery_feeds: list[CategoryRoot] = field(default_factory=list)
    daily_summary_hour: int | None = 9
    deep_drop_tiers: list[float] = field(default_factory=lambda: [40.0, 50.0, 66.0])
    verify_max_candidates: int = 35  # legacy configuration; progressive queue supersedes this
    catalogue_checks_per_run: int = 120
    catalogue_time_budget_seconds: int = 480
    cex_enabled: bool = True
    cex_max_lookups: int = 40
    cex_request_delay_seconds: float = 0.75
    cex_live_lookups_per_run: int = 40
    cex_quote_ttl_hours: int = 72
    cex_no_match_ttl_hours: int = 168
    cex_error_retry_hours: int = 6
    cex_alert_quote_max_age_hours: int = 12
    cex_min_cash_profit_eur: float = 5.0
    cex_min_cash_profit_percent: float = 10.0
    cex_extra_cost_eur: float = 0.0
    cex_delivery_eur: float = 0.0
    cex_min_roi_percent: float = 10.0
    cex_payout_haircut_percent: float = 0.0
    cex_max_alerts_per_run: int = 5
    cex_scan_any_price: bool = True
    cex_any_price_max_lookups: int = 50
    cex_any_price_min_amazon_eur: float = 20.0
    cex_any_price_max_amazon_eur: float = 800.0
    cex_any_price_require_keyword: bool = True
    cex_any_price_max_alerts_per_run: int = 5
    cex_priority_keywords: list[str] = field(
        default_factory=lambda: [
            "game",
            "gaming",
            "playstation",
            "ps5",
            "ps4",
            "xbox",
            "nintendo",
            "switch",
            "console",
            "iphone",
            "ipad",
            "samsung",
            "pixel",
            "tablet",
            "laptop",
            "macbook",
            "airpods",
            "headphone",
            "headset",
            "camera",
            "canon",
            "nikon",
            "kindle",
            "echo",
            "graphics",
            "gpu",
            "oculus",
            "meta quest",
            "fitbit",
            "watch",
        ]
    )


@dataclass
class CrawlResult:
    items: dict[str, tuple[BestsellerItem, str | None]] = field(default_factory=dict)
    categories: list[str] = field(default_factory=list)
    requests: int = 0
    errors: int = 0
    blocked: bool = False


@dataclass
class ProcessResult:
    priced: int = 0
    with_history: int = 0
    alerts: list[DetectedAlert] = field(default_factory=list)
    premium_drops: list[dict] = field(default_factory=list)
    rapid_drops: list[dict] = field(default_factory=list)
    drops: list[dict] = field(default_factory=list)
    general_drops: list[dict] = field(default_factory=list)
    cex_lookups: list[dict] = field(default_factory=list)
    cex_flips: list[dict] = field(default_factory=list)
    cex_arbitrage: list[dict] = field(default_factory=list)
    cex_stats: dict = field(default_factory=dict)
    verified_asins: set[str] = field(default_factory=set)
    verification_stats: dict = field(default_factory=dict)
    catalogue_stats: dict = field(default_factory=dict)


def _parse_roots(entries: list) -> list[CategoryRoot]:
    roots: list[CategoryRoot] = []
    for entry in entries or []:
        if isinstance(entry, dict):
            roots.append(
                CategoryRoot(
                    path=normalize_category_path(entry["path"]),
                    depth=int(entry.get("depth", 1)),
                    name=entry.get("name"),
                    pages=int(entry["pages"]) if entry.get("pages") is not None else None,
                )
            )
        else:
            roots.append(CategoryRoot(path=normalize_category_path(str(entry))))
    return roots


def load_config(path: Path) -> CrawlConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    roots = _parse_roots(raw.get("categories") or [])
    defaults = CrawlConfig(roots=[])
    return CrawlConfig(
        roots=roots,
        exclude={normalize_category_path(p) for p in raw.get("exclude") or []},
        max_items=int(raw.get("max_items", defaults.max_items)),
        max_requests=int(raw.get("max_requests", defaults.max_requests)),
        pages_per_category=int(raw.get("pages_per_category", defaults.pages_per_category)),
        request_delay_seconds=float(raw.get("request_delay_seconds", defaults.request_delay_seconds)),
        max_alerts_per_run=int(raw.get("max_alerts_per_run", defaults.max_alerts_per_run)),
        alert_cooldown_minutes=int(raw.get("alert_cooldown_minutes", defaults.alert_cooldown_minutes)),
        history_per_product=int(raw.get("history_per_product", defaults.history_per_product)),
        baseline_max_age_days=max(1, int(raw.get("baseline_max_age_days", defaults.baseline_max_age_days))),
        report_drop_percent=float(raw.get("report_drop_percent", defaults.report_drop_percent)),
        general_drop_percent=max(0.0, min(100.0, float(
            raw.get("general_drop_percent", defaults.general_drop_percent)
        ))),
        premium_min_price_eur=float(raw.get("premium_min_price_eur", defaults.premium_min_price_eur)),
        premium_history_drop_percent=float(
            raw.get("premium_history_drop_percent", defaults.premium_history_drop_percent)
        ),
        rapid_drop_min_price_eur=float(raw.get("rapid_drop_min_price_eur", defaults.rapid_drop_min_price_eur)),
        rapid_drop_percent=float(raw.get("rapid_drop_percent", defaults.rapid_drop_percent)),
        rapid_drop_min_saving_eur=float(raw.get("rapid_drop_min_saving_eur", defaults.rapid_drop_min_saving_eur)),
        premium_spot_check_limit=max(0, int(raw.get("premium_spot_check_limit", defaults.premium_spot_check_limit))),
        premium_alerts_per_run=max(0, int(raw.get("premium_alerts_per_run", defaults.premium_alerts_per_run))),
        discovery_feeds=_parse_roots(raw.get("discovery_feeds") or []),
        daily_summary_hour=raw.get("daily_summary_hour", defaults.daily_summary_hour),
        deep_drop_tiers=[float(x) for x in raw.get("deep_drop_tiers", defaults.deep_drop_tiers)],
        verify_max_candidates=max(0, int(raw.get('verify_max_candidates', defaults.verify_max_candidates))),
        catalogue_checks_per_run=max(1, int(raw.get('catalogue_checks_per_run', defaults.catalogue_checks_per_run))),
        catalogue_time_budget_seconds=max(60, int(raw.get('catalogue_time_budget_seconds', defaults.catalogue_time_budget_seconds))),
        cex_enabled=bool(raw.get("cex_enabled", defaults.cex_enabled)),
        cex_max_lookups=int(raw.get("cex_max_lookups", defaults.cex_max_lookups)),
        cex_request_delay_seconds=max(0.25, float(
            raw.get("cex_request_delay_seconds", defaults.cex_request_delay_seconds)
        )),
        cex_live_lookups_per_run=max(1, int(raw.get("cex_live_lookups_per_run", defaults.cex_live_lookups_per_run))),
        cex_quote_ttl_hours=max(1, int(raw.get("cex_quote_ttl_hours", defaults.cex_quote_ttl_hours))),
        cex_no_match_ttl_hours=max(1, int(raw.get("cex_no_match_ttl_hours", defaults.cex_no_match_ttl_hours))),
        cex_error_retry_hours=max(1, int(raw.get("cex_error_retry_hours", defaults.cex_error_retry_hours))),
        cex_alert_quote_max_age_hours=max(1, int(raw.get("cex_alert_quote_max_age_hours", defaults.cex_alert_quote_max_age_hours))),
        cex_min_cash_profit_eur=float(
            raw.get("cex_min_cash_profit_eur", defaults.cex_min_cash_profit_eur)
        ),
        cex_min_cash_profit_percent=float(
            raw.get("cex_min_cash_profit_percent", defaults.cex_min_cash_profit_percent)
        ),
        cex_max_alerts_per_run=int(raw.get("cex_max_alerts_per_run", defaults.cex_max_alerts_per_run)),
        cex_extra_cost_eur=max(0.0, float(raw.get("cex_extra_cost_eur", defaults.cex_extra_cost_eur))),
        cex_delivery_eur=max(0.0, float(raw.get("cex_delivery_eur", defaults.cex_delivery_eur))),
        cex_min_roi_percent=max(0.0, float(raw.get("cex_min_roi_percent", defaults.cex_min_roi_percent))),
        cex_payout_haircut_percent=min(100.0, max(0.0, float(raw.get("cex_payout_haircut_percent", defaults.cex_payout_haircut_percent)))),
        cex_scan_any_price=bool(raw.get("cex_scan_any_price", defaults.cex_scan_any_price)),
        cex_any_price_max_lookups=int(
            raw.get("cex_any_price_max_lookups", defaults.cex_any_price_max_lookups)
        ),
        cex_any_price_min_amazon_eur=float(
            raw.get("cex_any_price_min_amazon_eur", defaults.cex_any_price_min_amazon_eur)
        ),
        cex_any_price_max_amazon_eur=float(
            raw.get("cex_any_price_max_amazon_eur", defaults.cex_any_price_max_amazon_eur)
        ),
        cex_any_price_require_keyword=bool(
            raw.get("cex_any_price_require_keyword", defaults.cex_any_price_require_keyword)
        ),
        cex_any_price_max_alerts_per_run=int(
            raw.get("cex_any_price_max_alerts_per_run", defaults.cex_any_price_max_alerts_per_run)
        ),
        cex_priority_keywords=list(
            raw.get("cex_priority_keywords", defaults.cex_priority_keywords)
        ),
    )


def httpx_fetcher(client: httpx.AsyncClient) -> Fetcher:
    async def fetch(url: str) -> tuple[int, str]:
        resp = await client.get(url, follow_redirects=True)
        return resp.status_code, resp.text

    return fetch


async def crawl(fetch: Fetcher, config: CrawlConfig) -> CrawlResult:
    result = CrawlResult()
    visited: set[str] = set(config.exclude)
    consecutive_errors = 0

    def budget_left() -> bool:
        return result.requests < config.max_requests and len(result.items) < config.max_items

    # Roots are crawled in order, so put the categories you care most about first.
    for root in config.roots:
        queue: deque[tuple[str, int]] = deque([(root.path, root.depth)])
        while queue and budget_left():
            path, depth = queue.popleft()
            if path in visited:
                continue
            visited.add(path)
            category_name = None
            for page_no in range(1, (root.pages or config.pages_per_category) + 1):
                if not budget_left():
                    break
                url = f"{AMAZON_IE_BASE}{path}" + (f"?pg={page_no}" if page_no > 1 else "")
                result.requests += 1
                try:
                    status, html = await fetch(url)
                except Exception as exc:
                    logger.warning("Fetch failed for %s: %s", url, exc)
                    status, html = 0, ""
                if config.request_delay_seconds:
                    await asyncio.sleep(config.request_delay_seconds)

                if status != 200:
                    result.errors += 1
                    consecutive_errors += 1
                    logger.warning("HTTP %s for %s", status, url)
                    if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        logger.error("Too many failed pages in a row; stopping crawl")
                        result.blocked = True
                        return result
                    break
                consecutive_errors = 0

                page = parse_bestseller_page(html)
                if page.blocked:
                    logger.error("Amazon returned a captcha page at %s; stopping crawl", url)
                    result.blocked = True
                    return result

                category_name = category_name or page.category
                for item in page.items:
                    if len(result.items) >= config.max_items:
                        break
                    result.items.setdefault(item.asin, (item, category_name))
                if page_no == 1 and depth > 0:
                    queue.extend((child, depth - 1) for child in page.child_paths)
            result.categories.append(category_name or path)
    return result


def process_crawl(result: CrawlResult, config: CrawlConfig, *, allow_alerts: bool = False) -> ProcessResult:
    processed = ProcessResult()
    policy = load_alert_policy()
    premium_alert_budget = min(config.premium_alerts_per_run, policy.max_alerts_per_run)
    for asin, (item, category) in result.items.items():
        if item.price_eur is None:
            continue
        processed.priced += 1
        product_id = db.add_product(asin, item.url, item.title)
        previous = db.latest_price_check(product_id)
        previous_eur = float(previous["price_eur"]) if previous and previous.get("price_eur") is not None else None
        snap = ProductSnapshot(
            asin=asin,
            url=item.url,
            title=item.title,
            price_eur=item.price_eur,
            list_price_eur=None,
            currency="EUR",
            availability=None,
        )
        alert = record_and_detect(
            product_id,
            snap,
            known_title=item.title,
            cooldown_minutes=config.alert_cooldown_minutes,
            baseline_max_age_days=config.baseline_max_age_days,
            alert_policy=policy,
            emit_alert=allow_alerts,
        )
        if alert and allow_alerts:
            processed.alerts.append(alert)

        rapid = observe_rapid_scan_drop(
            asin=asin,
            title=item.title,
            url=item.url,
            category=category,
            rank=item.rank,
            price_eur=float(item.price_eur),
            previous_eur=previous_eur,
            config=config,
        )
        if rapid:
            processed.rapid_drops.append(observation_to_row(rapid))
            if (allow_alerts and premium_alert_budget > 0
                    and qualifies_observed_drop(observation_to_row(rapid), policy)):
                lead = build_lead_alert(
                    product_id, rapid, cooldown_minutes=config.alert_cooldown_minutes
                )
                if lead:
                    processed.alerts.append(lead)
                    premium_alert_budget -= 1

        anchor = db.get_price_baseline(product_id)
        if not anchor:
            continue
        reference_eur = float(anchor["reference_eur"])
        processed.with_history += 1
        drop = round((1 - item.price_eur / reference_eur) * 100, 1) if reference_eur > 0 else 0.0
        # General Amazon reductions are independent of CeX matching and the
        # much stricter price-error/Telegram alert threshold.
        saving = round(reference_eur - item.price_eur, 2)
        if saving > 0 and drop >= config.general_drop_percent:
            processed.general_drops.append({
                "asin": asin,
                "title": item.title,
                "url": item.url,
                "category": category,
                "rank": item.rank,
                "price_eur": round(item.price_eur, 2),
                "reference_eur": round(reference_eur, 2),
                "savings_eur": saving,
                "drop_percent": drop,
                "reference_confirmed_at": anchor["confirmed_at"],
            })
        if drop >= config.report_drop_percent:
            processed.drops.append(
                {
                    "asin": asin,
                    "title": item.title,
                    "url": item.url,
                    "category": category,
                    "rank": item.rank,
                    "price_eur": item.price_eur,
                    "median_eur": round(reference_eur, 2),  # legacy dashboard key
                    "reference_eur": round(reference_eur, 2),
                    "reference_confirmed_at": anchor["confirmed_at"],
                    "recent_prices": [
                        [check["checked_at"], check["price_eur"]]
                        for check in reversed(db.price_history(product_id, limit=12))
                        if check["price_eur"] is not None
                    ],
                    "drop_percent": drop,
                    "drop_tier": drop_tier(drop, config.deep_drop_tiers),
                }
            )
        premium = observe_premium_history_drop(
            asin=asin,
            title=item.title,
            url=item.url,
            category=category,
            rank=item.rank,
            price_eur=float(item.price_eur),
            reference_eur=reference_eur,
            config=config,
        )
        if premium:
            processed.premium_drops.append(observation_to_row(premium))
            if (allow_alerts and premium_alert_budget > 0
                    and qualifies_observed_drop(observation_to_row(premium), policy)):
                lead = build_lead_alert(
                    product_id, premium, cooldown_minutes=config.alert_cooldown_minutes
                )
                if lead:
                    processed.alerts.append(lead)
                    premium_alert_budget -= 1
    processed.general_drops.sort(key=lambda d: (d["drop_percent"], d["savings_eur"]), reverse=True)
    processed.drops.sort(key=lambda d: d["drop_percent"], reverse=True)
    processed.premium_drops.sort(key=lambda d: d["drop_percent"], reverse=True)
    processed.rapid_drops.sort(key=lambda d: d["drop_percent"], reverse=True)
    db.prune_price_checks(config.history_per_product)
    return processed


async def send_cex_alerts(
    flips: list[dict],
    config: CrawlConfig,
    *,
    dashboard_url: str,
    deep_drop: bool = True,
    max_alerts: int | None = None,
    should_send: Callable[[dict], bool] | None = None,
    on_sent: Callable[[dict], None] | None = None,
) -> int:
    policy = load_alert_policy()
    cap = min(
        max_alerts if max_alerts is not None else config.cex_max_alerts_per_run,
        policy.max_alerts_per_run,
    )
    sent = 0
    eligible = [
        row for row in flips
        if qualifies_cex_alert(row, policy)
        and (should_send is None or should_send(row))
    ]
    eligible.sort(key=lambda row: float(row.get("cex_cash_profit_eur") or 0), reverse=True)
    for row in eligible[:cap]:
        title = row.get("title") or row.get("asin") or "Item"
        amazon = float(row.get("price_eur") or 0)
        cash = float(row.get("cex_cash_eur") or 0)
        profit = float(row.get("cex_cash_profit_eur") or 0)
        retail = float(row.get("cex_retail_eur") or 0)
        margin = float(row.get("cex_min_profit_eur") or 0)
        drop = row.get("drop_percent")
        if deep_drop and drop is not None:
            headline = "💰 Possible CeX research lead (observed Amazon drop)\n\n"
            price_line = (
                f"Amazon now: €{amazon:.2f} ({drop:g}% below its usual Amazon price)\n"
            )
        else:
            headline = "💰 Possible CeX research lead (no big drop)\n\n"
            price_line = f"Amazon now: €{amazon:.2f} (regular price — no big drop required)\n"
        match = row.get("match_confidence") or "title_only"
        high = match in ("barcode_verified", "model_verified") and profit >= 25
        if high:
            headline = "🚨 HIGH POTENTIAL CeX research lead (NOT checkout verified)\n\n"
        message = (
            f"{headline}{title}\n"
            f"{price_line}"
            f"CeX quoted cash: €{cash:.2f} · estimated net profit: €{profit:.2f}\n"
            f"(after configured costs/haircut; required profit ≥€{margin:.2f})\n"
            f"Max buy: €{float(row.get('cex_max_buy_eur') or 0):.2f} · break-even: €{float(row.get('cex_break_even_buy_eur') or 0):.2f}\n"
            f"ROI: {row.get('cex_net_roi_percent')}% · matching: {match}\n"
            f"Amazon price observed: {row.get('observed_at') or row.get('found_at') or 'latest section scan'}\n"
            f"CeX quote checked: {row.get('cex_quote_checked_at') or 'unknown'}\n"
            f"⚠️ RESEARCH ONLY: Amazon seller, condition, exact variant and checkout total are not checked. Confirm CeX grade/cash quote too.\n"
            f"CeX also sells similar for €{retail:.2f}\n\n"
            f"{row.get('url')}\n"
            f"{row.get('cex_url')}\n\n"
            f"{dashboard_url}"
        )
        telegram_ok, whatsapp_ok = await send_notification(message)
        if telegram_ok or whatsapp_ok:
            sent += 1
            if on_sent is not None:
                on_sent(row)
    return sent


async def send_alerts(
    alerts: list[DetectedAlert], config: CrawlConfig, *, details_url: str | None = None
) -> int:
    # Never send a second "and X more" message for a scan. The dashboard
    # retains all candidates; Telegram is reserved for the strongest leads.
    cap = min(config.max_alerts_per_run, load_alert_policy().max_alerts_per_run)
    ranked = sorted(alerts, key=lambda a: a.discount_percent or 0, reverse=True)
    for alert in ranked[:cap]:
        await notify_alert(alert)
    return min(len(ranked), cap)


def verification_rejection_reason(snap: ProductSnapshot, expected: float) -> str | None:
    """Report *why* a deal cannot be trusted, without weakening seller checks."""
    if expected <= 0:
        return "invalid_bestseller_price"
    if snap.price_eur is None or snap.price_eur <= 0:
        return "product_page_price_missing"
    if abs(snap.price_eur - expected) > 0.02:
        # The scanned list and selected buy box disagree, so neither is safe to buy.
        return "price_changed_or_variant"
    if snap.condition == "used" or re.search(
        r"\b(?:used|renewed|refurbished|pre-owned)\b", snap.title or "", re.I
    ):
        return "used_or_refurbished"
    if snap.availability and re.search(
        r"\b(?:out of stock|currently unavailable|temporarily unavailable)\b",
        snap.availability, re.I,
    ):
        return "unavailable"
    if snap.seller_status == "unknown":
        return "seller_not_detected"
    if snap.seller_status == "third_party" and snap.fulfilment_status != "amazon":
        return "third_party_fulfilment_not_confirmed"
    if not is_eligible_amazon_offer(snap):
        return "seller_not_eligible"
    return None


async def verify_candidates(
    client: httpx.AsyncClient, candidates: list[dict], *, limit: int,
    on_result: Callable[[str, str], None] | None = None,
    delay_seconds: float = 0.0,
) -> tuple[list[dict], dict]:
    """Verify shortlisted offers and expose rejection reasons to Actions and Pages.

    A bestsellers price alone cannot establish who sells or fulfils the item.
    Unknown prices, sellers or delivery arrangements always fail closed.
    """
    passed: list[dict] = []
    stats: dict = {
        "attempted": 0,
        "amazon_verified": 0,
        "rejected_or_unknown": 0,
        "rejection_reasons": {},
        "sample_failures": [],
    }
    seen: set[str] = set()

    def reject(asin: str, reason: str) -> None:
        stats["rejected_or_unknown"] += 1
        reasons = stats["rejection_reasons"]
        reasons[reason] = reasons.get(reason, 0) + 1
        if len(stats["sample_failures"]) < 5:
            stats["sample_failures"].append({"asin": asin, "reason": reason})
        if on_result:
            on_result(asin, reason)

    for candidate in candidates:
        asin = candidate.get("asin")
        if not asin or asin in seen or stats["attempted"] >= limit:
            continue
        seen.add(asin)
        stats["attempted"] += 1
        try:
            snap = await fetch_product(client, asin)
            expected = float(candidate.get("price_eur") or 0)
            reason = verification_rejection_reason(snap, expected)
            if reason:
                reject(asin, reason)
                continue
            passed.append({
                **candidate,
                "verified_title": snap.title,
                "seller_name": snap.seller_name,
                "seller_type": eligible_offer_type(snap),
                "fulfilment_status": snap.fulfilment_status,
                "barcode": snap.barcode,
                "model_number": snap.model_number,
                "seller_verified": True,
                "verified_at": datetime.now(timezone.utc).isoformat(),
            })
            stats["amazon_verified"] += 1
            if on_result:
                on_result(asin, "verified")
        except httpx.HTTPStatusError as exc:
            reject(asin, f"amazon_http_{exc.response.status_code}")
        except httpx.TimeoutException:
            reject(asin, "amazon_timeout")
        except httpx.HTTPError:
            reject(asin, "amazon_network_error")
        except (ValueError, TypeError):
            reject(asin, "product_page_parse_error")
        finally:
            if delay_seconds > 0:
                await asyncio.sleep(delay_seconds)
    return passed, stats
