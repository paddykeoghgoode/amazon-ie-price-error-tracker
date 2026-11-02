import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from app import database as db
from app.scraper.amazon_ie import AMAZON_IE_BASE, ProductSnapshot, is_eligible_amazon_offer, eligible_offer_type, fetch_product
from app.scraper.bestsellers import BestsellerItem, normalize_category_path, parse_bestseller_page
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
    daily_summary_hour: int | None = 9
    deep_drop_tiers: list[float] = field(default_factory=lambda: [40.0, 50.0, 66.0])
    verify_max_candidates: int = 35
    cex_enabled: bool = True
    cex_max_lookups: int = 40
    cex_request_delay_seconds: float = 0.35
    cex_min_cash_profit_eur: float = 5.0
    cex_min_cash_profit_percent: float = 10.0
    cex_extra_cost_eur: float = 0.0
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
    drops: list[dict] = field(default_factory=list)
    cex_lookups: list[dict] = field(default_factory=list)
    cex_flips: list[dict] = field(default_factory=list)
    cex_arbitrage: list[dict] = field(default_factory=list)
    cex_stats: dict = field(default_factory=dict)
    verified_asins: set[str] = field(default_factory=set)
    verification_stats: dict = field(default_factory=dict)


def load_config(path: Path) -> CrawlConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    roots = [
        CategoryRoot(
            path=normalize_category_path(entry["path"] if isinstance(entry, dict) else entry),
            depth=int(entry.get("depth", 1)) if isinstance(entry, dict) else 1,
            name=entry.get("name") if isinstance(entry, dict) else None,
        )
        for entry in raw.get("categories") or []
    ]
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
        daily_summary_hour=raw.get("daily_summary_hour", defaults.daily_summary_hour),
        deep_drop_tiers=[float(x) for x in raw.get("deep_drop_tiers", defaults.deep_drop_tiers)],
        verify_max_candidates=max(0, int(raw.get('verify_max_candidates', defaults.verify_max_candidates))),
        cex_enabled=bool(raw.get("cex_enabled", defaults.cex_enabled)),
        cex_max_lookups=int(raw.get("cex_max_lookups", defaults.cex_max_lookups)),
        cex_request_delay_seconds=float(
            raw.get("cex_request_delay_seconds", defaults.cex_request_delay_seconds)
        ),
        cex_min_cash_profit_eur=float(
            raw.get("cex_min_cash_profit_eur", defaults.cex_min_cash_profit_eur)
        ),
        cex_min_cash_profit_percent=float(
            raw.get("cex_min_cash_profit_percent", defaults.cex_min_cash_profit_percent)
        ),
        cex_max_alerts_per_run=int(raw.get("cex_max_alerts_per_run", defaults.cex_max_alerts_per_run)),
        cex_extra_cost_eur=max(0.0, float(raw.get("cex_extra_cost_eur", defaults.cex_extra_cost_eur))),
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
            for page_no in range(1, config.pages_per_category + 1):
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
    for asin, (item, category) in result.items.items():
        if item.price_eur is None:
            continue
        processed.priced += 1
        product_id = db.add_product(asin, item.url, item.title)
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
        )
        if alert and allow_alerts:
            processed.alerts.append(alert)

        anchor = db.get_price_baseline(product_id)
        if not anchor:
            continue
        reference_eur = float(anchor["reference_eur"])
        processed.with_history += 1
        drop = round((1 - item.price_eur / reference_eur) * 100, 1) if reference_eur > 0 else 0.0
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
    processed.drops.sort(key=lambda d: d["drop_percent"], reverse=True)
    db.prune_price_checks(config.history_per_product)
    return processed


async def send_cex_alerts(
    flips: list[dict],
    config: CrawlConfig,
    *,
    dashboard_url: str,
    deep_drop: bool = True,
    max_alerts: int | None = None,
) -> int:
    cap = max_alerts if max_alerts is not None else config.cex_max_alerts_per_run
    sent = 0
    for row in flips[:cap]:
        title = row.get("title") or row.get("asin") or "Item"
        amazon = float(row.get("price_eur") or 0)
        cash = float(row.get("cex_cash_eur") or 0)
        profit = float(row.get("cex_cash_profit_eur") or 0)
        retail = float(row.get("cex_retail_eur") or 0)
        margin = float(row.get("cex_min_profit_eur") or 0)
        drop = row.get("drop_percent")
        if deep_drop and drop is not None:
            headline = "💰 Possible CeX cash flip (Amazon on sale)\n\n"
            price_line = (
                f"Amazon now: €{amazon:.2f} ({drop:g}% below its usual Amazon price)\n"
            )
        else:
            headline = "💰 CeX cash profit (no Amazon sale needed)\n\n"
            price_line = f"Amazon now: €{amazon:.2f} (regular price — no big drop required)\n"
        message = (
            f"{headline}{title}\n"
            f"{price_line}"
            f"CeX quoted cash: €{cash:.2f} · estimated net profit: €{profit:.2f}\n"
            f"(after configured costs/haircut; required profit ≥€{margin:.2f})\n"
            f"CeX also sells similar for €{retail:.2f}\n\n"
            f"{row.get('url')}\n"
            f"{row.get('cex_url')}\n\n"
            f"{dashboard_url}"
        )
        telegram_ok, whatsapp_ok = await send_notification(message)
        if telegram_ok or whatsapp_ok:
            sent += 1
    return sent


async def send_alerts(
    alerts: list[DetectedAlert], config: CrawlConfig, *, details_url: str | None = None
) -> int:
    ranked = sorted(alerts, key=lambda a: a.discount_percent or 0, reverse=True)
    for alert in ranked[: config.max_alerts_per_run]:
        await notify_alert(alert)
    extra = len(ranked) - config.max_alerts_per_run
    if extra > 0:
        lines = [f"…and {extra} more possible Amazon.ie price errors in this bestseller scan."]
        if details_url:
            lines.append(details_url)
        await send_notification("\n".join(lines))
    return min(len(ranked), config.max_alerts_per_run)


async def verify_candidates(client: httpx.AsyncClient, candidates: list[dict], *, limit: int) -> tuple[list[dict], dict]:
    """Confirm exact ASIN's Amazon-owned offer before publishing or contacting CeX."""
    passed = []
    stats = {"attempted": 0, "amazon_verified": 0, "rejected_or_unknown": 0}
    seen = set()
    for candidate in candidates:
        asin = candidate.get("asin")
        if not asin or asin in seen or stats["attempted"] >= limit:
            continue
        seen.add(asin)
        stats["attempted"] += 1
        try:
            snap = await fetch_product(client, asin)
            expected = float(candidate.get("price_eur") or 0)
            if not is_eligible_amazon_offer(snap) or not expected or abs(float(snap.price_eur) - expected) > 0.02:
                stats["rejected_or_unknown"] += 1
                continue
            passed.append({**candidate, "seller_name": snap.seller_name, "seller_type": eligible_offer_type(snap), "fulfilment_status": snap.fulfilment_status, "seller_verified": True, "verified_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()})
            stats["amazon_verified"] += 1
        except (httpx.HTTPError, ValueError, TypeError):
            stats["rejected_or_unknown"] += 1
    return passed, stats
