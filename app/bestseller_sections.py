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
    include_global_discovery_feeds: bool = True
    discovery_feeds: list[CategoryRoot] = field(default_factory=list)
    signal_min_price_eur: float | None = None
    signal_min_review_count: int | None = None
    cex_live_lookups_per_run: int | None = None
    catalogue_checks_per_run: int | None = None
    catalogue_time_budget_seconds: int | None = None
    cex_any_price_max_lookups: int | None = None
    cex_priority_keywords: list[str] | None = None
    cex_any_price_require_keyword: bool | None = None


def _parse_categories(entries: list) -> list[CategoryRoot]:
    return [
        CategoryRoot(
            path=normalize_category_path(entry["path"] if isinstance(entry, dict) else entry),
            depth=int(entry.get("depth", 1)) if isinstance(entry, dict) else 1,
            name=entry.get("name") if isinstance(entry, dict) else None,
            pages=int(entry["pages"]) if isinstance(entry, dict) and entry.get("pages") is not None else None,
        )
        for entry in entries
    ]


def load_sections(path: Path) -> list[CrawlSection]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    sections_raw = raw.get("sections")
    if sections_raw:
        out: list[CrawlSection] = []
        for entry in sections_raw:
            keywords = entry.get("cex_priority_keywords")
            out.append(
                CrawlSection(
                    id=str(entry["id"]),
                    name=str(entry.get("name") or entry["id"]),
                    categories=_parse_categories(entry.get("categories") or []),
                    max_items=int(entry["max_items"]) if entry.get("max_items") else None,
                    max_requests=int(entry["max_requests"]) if entry.get("max_requests") else None,
                    include_global_discovery_feeds=bool(
                        entry.get("include_global_discovery_feeds", True)
                    ),
                    discovery_feeds=_parse_categories(entry.get("discovery_feeds") or []),
                    signal_min_price_eur=(
                        float(entry["signal_min_price_eur"])
                        if entry.get("signal_min_price_eur") is not None
                        else None
                    ),
                    signal_min_review_count=(
                        int(entry["signal_min_review_count"])
                        if entry.get("signal_min_review_count") is not None
                        else None
                    ),
                    cex_live_lookups_per_run=(
                        int(entry["cex_live_lookups_per_run"])
                        if entry.get("cex_live_lookups_per_run") is not None
                        else None
                    ),
                    catalogue_checks_per_run=(
                        int(entry["catalogue_checks_per_run"])
                        if entry.get("catalogue_checks_per_run") is not None
                        else None
                    ),
                    catalogue_time_budget_seconds=(
                        int(entry["catalogue_time_budget_seconds"])
                        if entry.get("catalogue_time_budget_seconds") is not None
                        else None
                    ),
                    cex_any_price_max_lookups=(
                        int(entry["cex_any_price_max_lookups"])
                        if entry.get("cex_any_price_max_lookups") is not None
                        else None
                    ),
                    cex_priority_keywords=list(keywords) if keywords is not None else None,
                    cex_any_price_require_keyword=(
                        bool(entry["cex_any_price_require_keyword"])
                        if entry.get("cex_any_price_require_keyword") is not None
                        else None
                    ),
                )
            )
        return out
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
    roots: list[CategoryRoot] = []
    seen: set[str] = set()
    feed_roots: list[CategoryRoot] = []
    if section.include_global_discovery_feeds:
        feed_roots.extend(base.discovery_feeds)
    feed_roots.extend(section.discovery_feeds)
    for root in feed_roots + list(section.categories):
        if root.path in seen:
            continue
        seen.add(root.path)
        roots.append(root)
    keywords = (
        section.cex_priority_keywords
        if section.cex_priority_keywords is not None
        else base.cex_priority_keywords
    )
    require_kw = (
        section.cex_any_price_require_keyword
        if section.cex_any_price_require_keyword is not None
        else base.cex_any_price_require_keyword
    )
    return CrawlConfig(
        roots=roots,
        exclude=set(base.exclude),
        max_items=max_items,
        max_requests=max_requests,
        pages_per_category=base.pages_per_category,
        request_delay_seconds=base.request_delay_seconds,
        max_alerts_per_run=base.max_alerts_per_run,
        alert_cooldown_minutes=base.alert_cooldown_minutes,
        history_per_product=base.history_per_product,
        baseline_max_age_days=base.baseline_max_age_days,
        report_drop_percent=base.report_drop_percent,
        general_drop_percent=base.general_drop_percent,
        premium_min_price_eur=base.premium_min_price_eur,
        premium_history_drop_percent=base.premium_history_drop_percent,
        rapid_drop_min_price_eur=base.rapid_drop_min_price_eur,
        rapid_drop_percent=base.rapid_drop_percent,
        rapid_drop_min_saving_eur=base.rapid_drop_min_saving_eur,
        premium_spot_check_limit=base.premium_spot_check_limit,
        premium_alerts_per_run=base.premium_alerts_per_run,
        discovery_feeds=list(base.discovery_feeds),
        daily_summary_hour=base.daily_summary_hour,
        deep_drop_tiers=list(base.deep_drop_tiers),
        cex_enabled=base.cex_enabled,
        verify_max_candidates=base.verify_max_candidates,
        catalogue_checks_per_run=(
            section.catalogue_checks_per_run
            if section.catalogue_checks_per_run is not None
            else base.catalogue_checks_per_run
        ),
        catalogue_time_budget_seconds=(
            section.catalogue_time_budget_seconds
            if section.catalogue_time_budget_seconds is not None
            else base.catalogue_time_budget_seconds
        ),
        cex_max_lookups=max(15, base.cex_max_lookups // 2),
        cex_request_delay_seconds=base.cex_request_delay_seconds,
        cex_live_lookups_per_run=(
            section.cex_live_lookups_per_run
            if section.cex_live_lookups_per_run is not None
            else base.cex_live_lookups_per_run
        ),
        cex_quote_ttl_hours=base.cex_quote_ttl_hours,
        cex_no_match_ttl_hours=base.cex_no_match_ttl_hours,
        cex_error_retry_hours=base.cex_error_retry_hours,
        cex_alert_quote_max_age_hours=base.cex_alert_quote_max_age_hours,
        cex_min_cash_profit_eur=base.cex_min_cash_profit_eur,
        cex_min_cash_profit_percent=base.cex_min_cash_profit_percent,
        cex_extra_cost_eur=base.cex_extra_cost_eur,
        cex_delivery_eur=base.cex_delivery_eur,
        cex_min_roi_percent=base.cex_min_roi_percent,
        cex_payout_haircut_percent=base.cex_payout_haircut_percent,
        cex_max_alerts_per_run=base.cex_max_alerts_per_run,
        cex_scan_any_price=base.cex_scan_any_price,
        cex_any_price_max_lookups=(
            section.cex_any_price_max_lookups
            if section.cex_any_price_max_lookups is not None
            else max(20, base.cex_any_price_max_lookups // 2)
        ),
        cex_any_price_min_amazon_eur=base.cex_any_price_min_amazon_eur,
        cex_any_price_max_amazon_eur=base.cex_any_price_max_amazon_eur,
        cex_any_price_require_keyword=require_kw,
        cex_any_price_max_alerts_per_run=base.cex_any_price_max_alerts_per_run,
        cex_priority_keywords=list(keywords),
        signal_min_price_eur=section.signal_min_price_eur,
        signal_min_review_count=section.signal_min_review_count,
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
        baseline_max_age_days=base.baseline_max_age_days,
        report_drop_percent=base.report_drop_percent,
        general_drop_percent=base.general_drop_percent,
        premium_min_price_eur=base.premium_min_price_eur,
        premium_history_drop_percent=base.premium_history_drop_percent,
        rapid_drop_min_price_eur=base.rapid_drop_min_price_eur,
        rapid_drop_percent=base.rapid_drop_percent,
        rapid_drop_min_saving_eur=base.rapid_drop_min_saving_eur,
        premium_spot_check_limit=base.premium_spot_check_limit,
        premium_alerts_per_run=base.premium_alerts_per_run,
        discovery_feeds=list(base.discovery_feeds),
        daily_summary_hour=base.daily_summary_hour,
        deep_drop_tiers=list(base.deep_drop_tiers),
        cex_enabled=base.cex_enabled,
        verify_max_candidates=base.verify_max_candidates,
        catalogue_checks_per_run=base.catalogue_checks_per_run,
        catalogue_time_budget_seconds=base.catalogue_time_budget_seconds,
        cex_max_lookups=base.cex_max_lookups,
        cex_request_delay_seconds=base.cex_request_delay_seconds,
        cex_live_lookups_per_run=base.cex_live_lookups_per_run,
        cex_quote_ttl_hours=base.cex_quote_ttl_hours,
        cex_no_match_ttl_hours=base.cex_no_match_ttl_hours,
        cex_error_retry_hours=base.cex_error_retry_hours,
        cex_alert_quote_max_age_hours=base.cex_alert_quote_max_age_hours,
        cex_min_cash_profit_eur=base.cex_min_cash_profit_eur,
        cex_min_cash_profit_percent=base.cex_min_cash_profit_percent,
        cex_extra_cost_eur=base.cex_extra_cost_eur,
        cex_delivery_eur=base.cex_delivery_eur,
        cex_min_roi_percent=base.cex_min_roi_percent,
        cex_payout_haircut_percent=base.cex_payout_haircut_percent,
        cex_max_alerts_per_run=base.cex_max_alerts_per_run,
        cex_scan_any_price=base.cex_scan_any_price,
        cex_any_price_max_lookups=base.cex_any_price_max_lookups,
        cex_any_price_min_amazon_eur=base.cex_any_price_min_amazon_eur,
        cex_any_price_max_amazon_eur=base.cex_any_price_max_amazon_eur,
        cex_any_price_require_keyword=base.cex_any_price_require_keyword,
        cex_any_price_max_alerts_per_run=base.cex_any_price_max_alerts_per_run,
        cex_priority_keywords=list(base.cex_priority_keywords),
    )
