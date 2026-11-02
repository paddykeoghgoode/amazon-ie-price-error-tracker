from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.scraper.bestsellers import normalize_category_path
from app.services.bestsellers import CategoryRoot, CrawlConfig


@dataclass
class CrawlSection:
    id: str
    name: str
    categories: list[CategoryRoot]
    max_items: int | None = None
    max_requests: int | None = None


def _parse_categories(entries: list) -> list[CategoryRoot]:
    return [
        CategoryRoot(
            path=normalize_category_path(entry["path"] if isinstance(entry, dict) else entry),
            depth=int(entry.get("depth", 1)) if isinstance(entry, dict) else 1,
            name=entry.get("name") if isinstance(entry, dict) else None,
        )
        for entry in entries
    ]


def load_sections(path: Path) -> list[CrawlSection]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    sections_raw = raw.get("sections")
    if sections_raw:
        return [
            CrawlSection(
                id=str(entry["id"]),
                name=str(entry.get("name") or entry["id"]),
                categories=_parse_categories(entry.get("categories") or []),
                max_items=int(entry["max_items"]) if entry.get("max_items") else None,
                max_requests=int(entry["max_requests"]) if entry.get("max_requests") else None,
            )
            for entry in sections_raw
        ]
    # Legacy single list — one synthetic section.
    return [
        CrawlSection(
            id="all",
            name="All categories",
            categories=_parse_categories(raw.get("categories") or []),
        )
    ]


def rotate_section_id(sections: list[CrawlSection], run_number: int) -> str:
    if not sections:
        return "all"
    ordered = [s for s in sections if s.id != "all"]
    if not ordered:
        return sections[0].id
    return ordered[run_number % len(ordered)].id


def config_for_section(base: CrawlConfig, section: CrawlSection) -> CrawlConfig:
    """Return a copy of base config scoped to one section."""
    max_items = section.max_items if section.max_items is not None else max(2000, base.max_items // 4)
    max_requests = section.max_requests if section.max_requests is not None else max(150, base.max_requests // 4)
    return CrawlConfig(
        roots=list(section.categories),
        exclude=set(base.exclude),
        max_items=max_items,
        max_requests=max_requests,
        pages_per_category=base.pages_per_category,
        request_delay_seconds=base.request_delay_seconds,
        max_alerts_per_run=base.max_alerts_per_run,
        alert_cooldown_minutes=base.alert_cooldown_minutes,
        history_per_product=base.history_per_product,
        report_drop_percent=base.report_drop_percent,
        daily_summary_hour=base.daily_summary_hour,
        deep_drop_tiers=list(base.deep_drop_tiers),
        cex_enabled=base.cex_enabled,
        cex_max_lookups=max(15, base.cex_max_lookups // 2),
        cex_request_delay_seconds=base.cex_request_delay_seconds,
        cex_min_cash_profit_eur=base.cex_min_cash_profit_eur,
        cex_min_cash_profit_percent=base.cex_min_cash_profit_percent,
        cex_extra_cost_eur=base.cex_extra_cost_eur,
        cex_payout_haircut_percent=base.cex_payout_haircut_percent,
        cex_extra_cost_eur=base.cex_extra_cost_eur,
        cex_payout_haircut_percent=base.cex_payout_haircut_percent,
        cex_max_alerts_per_run=base.cex_max_alerts_per_run,
        cex_scan_any_price=base.cex_scan_any_price,
        cex_any_price_max_lookups=max(20, base.cex_any_price_max_lookups // 2),
        cex_any_price_min_amazon_eur=base.cex_any_price_min_amazon_eur,
        cex_any_price_max_amazon_eur=base.cex_any_price_max_amazon_eur,
        cex_any_price_require_keyword=base.cex_any_price_require_keyword,
        cex_any_price_max_alerts_per_run=base.cex_any_price_max_alerts_per_run,
        cex_priority_keywords=list(base.cex_priority_keywords),
    )


def config_for_all_sections(base: CrawlConfig, sections: list[CrawlSection]) -> CrawlConfig:
    roots: list[CategoryRoot] = []
    seen: set[str] = set()
    for section in sections:
        for root in section.categories:
            if root.path in seen:
                continue
            seen.add(root.path)
            roots.append(root)
    return CrawlConfig(
        roots=roots,
        exclude=set(base.exclude),
        max_items=base.max_items,
        max_requests=base.max_requests,
        pages_per_category=base.pages_per_category,
        request_delay_seconds=base.request_delay_seconds,
        max_alerts_per_run=base.max_alerts_per_run,
        alert_cooldown_minutes=base.alert_cooldown_minutes,
        history_per_product=base.history_per_product,
        report_drop_percent=base.report_drop_percent,
        daily_summary_hour=base.daily_summary_hour,
        deep_drop_tiers=list(base.deep_drop_tiers),
        cex_enabled=base.cex_enabled,
        cex_max_lookups=base.cex_max_lookups,
        cex_request_delay_seconds=base.cex_request_delay_seconds,
        cex_min_cash_profit_eur=base.cex_min_cash_profit_eur,
        cex_min_cash_profit_percent=base.cex_min_cash_profit_percent,
        cex_max_alerts_per_run=base.cex_max_alerts_per_run,
        cex_scan_any_price=base.cex_scan_any_price,
        cex_any_price_max_lookups=base.cex_any_price_max_lookups,
        cex_any_price_min_amazon_eur=base.cex_any_price_min_amazon_eur,
        cex_any_price_max_amazon_eur=base.cex_any_price_max_amazon_eur,
        cex_any_price_require_keyword=base.cex_any_price_require_keyword,
        cex_any_price_max_alerts_per_run=base.cex_any_price_max_alerts_per_run,
        cex_priority_keywords=list(base.cex_priority_keywords),
    )
