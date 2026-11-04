from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path

import httpx

from app import database as db
from app.bestseller_sections import (
    config_for_all_sections,
    config_for_section,
    load_sections,
    next_priority_section,
    rotate_section_id,
)
from app.config import settings
from app.deals import export_deals, merge_section_deals, render_deals_markdown
from app.services.bestsellers import (
    crawl,
    httpx_fetcher,
    load_config,
    process_crawl,
    send_alerts,
    send_cex_alerts,
    send_notification,
)
from app.services.cex_arbitrage import enrich_with_cex
from app.services.health import send_health_messages
from app.services.premium_drops import spot_check_list_prices
from app.services.discovery_checks import choose_detail_checks, apply_detail_checks
from app.services.amazon_target_discovery import discover_amazon_targets
from app.services.alert_policy import load_alert_policy
from app.services.resale_relevance import relevance
from app.services.cex_targets import (
    load_target_config, target_state, sync_mappings, refresh_mapped_quotes,
    discover_targets, attach_target_quotes, target_funnel,
    import_target_seed,
)
from app.services.monitor import BROWSER_HEADERS
from app.services.catalogue import (
    make_section_catalogue, select_check_batch, apply_batch_results,
    catalogue_progress, merge_catalogue,
    reuse_cex_metadata,
)
from app.snapshot import read_snapshot, write_snapshot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("crawl_bestsellers")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crawl Amazon.ie bestseller lists for price errors")
    parser.add_argument("--config", type=Path, default=Path("bestsellers.json"))
    parser.add_argument("--database", type=Path, default=Path("data/bestsellers.db"))
    parser.add_argument("--output", type=Path, default=Path("docs/deals.json"))
    parser.add_argument(
        "--section",
        default="rotate",
        help="Section id from bestsellers.json, 'all' for full crawl, or 'rotate' (after latest published section)",
    )
    parser.add_argument(
        "--run-number",
        type=int,
        default=None,
        help="Fallback rotation index if no published section checkpoint is available",
    )
    parser.add_argument(
        "--merge-previous",
        type=Path,
        default=None,
        help="When crawling one section, merge into this existing deals.json snapshot",
    )
    parser.add_argument("--markdown", type=Path, default=None, help="Also write a Markdown report here")
    parser.add_argument("--max-items", type=int, default=None, help="Override max_items from the config")
    parser.add_argument("--no-notify", action="store_true", help="Detect and report, but send no messages")
    parser.add_argument("--publish-fragment", action="store_true", help="Write only this section so publishing can merge with the latest Pages snapshot")
    parser.add_argument("--previous-catalogue", type=Path, default=None, help="Previously published ASIN checklist for fair, resumable batches")
    parser.add_argument("--catalogue-output", type=Path, default=None, help="Write complete listing inventory / progress for this section")
    parser.add_argument(
        "--skip-health",
        action="store_true",
        help="Skip daily/welcome health Telegram messages (used between sections in a full crawl)",
    )
    return parser.parse_args()


def resolve_section_id(args: argparse.Namespace, sections: list) -> str:
    if args.section == "rotate":
        # A workflow run number includes watchlist-only ticks, issues and manual
        # dispatches; using it for rotation would repeatedly skip sections.
        # Choose the successor of the last SUCCESSFULLY PUBLISHED section instead.
        previous_path = getattr(args, "merge_previous", None)
        if previous_path and previous_path.exists():
            try:
                previous = read_snapshot(previous_path)
                priority_id = next_priority_section(sections, previous)
                if priority_id is not None:
                    logger.info("Priority rotation selected %s from published checkpoints", priority_id)
                    return priority_id
                ordered = [s.id for s in sections if s.id != "all"]
                last_id = previous.get("last_section_id")
                if last_id not in ordered:
                    historical = previous.get("sections") or {}
                    completed = {
                        sid: snap for sid, snap in historical.items()
                        if sid in ordered and isinstance(snap, dict)
                    }
                    if completed:
                        last_id = max(
                            completed,
                            key=lambda sid: str(completed[sid].get("generated_at") or ""),
                        )
                if last_id in ordered:
                    next_id = ordered[(ordered.index(last_id) + 1) % len(ordered)]
                    logger.info(
                        "Rotating after last published section %s -> %s", last_id, next_id
                    )
                    return next_id
            except (OSError, ValueError, TypeError) as exc:
                logger.warning("Previous section checkpoint unreadable (%s)", type(exc).__name__)
        # First-ever scan or local run with no published checkpoint.
        run_no = args.run_number
        if run_no is None:
            run_no = int(os.environ.get("GITHUB_RUN_NUMBER", "0") or 0)
        return next_priority_section(sections, {}) or rotate_section_id(sections, run_no)
    if args.section == "all":
        return "all"
    return args.section


async def crawl_section(
    config,
    *,
    section_id: str,
    section_name: str,
    args: argparse.Namespace,
    notify: bool,
    previous_catalogue_section: dict | None = None,
    published_catalogue: dict | None = None,
) -> tuple[dict, dict]:
    async with httpx.AsyncClient(headers=BROWSER_HEADERS, timeout=30.0) as client:
        result = await crawl(httpx_fetcher(client), config)
    logger.info(
        "[%s] Crawled %s lists with %s requests (%s failed, blocked=%s): %s items",
        section_id,
        len(result.categories),
        result.requests,
        result.errors,
        result.blocked,
        len(result.items),
    )

    if result.blocked:
        raise RuntimeError(f"[{section_id}] Amazon blocked the crawl; preserve published checkpoint")

    # One scan of bestseller pages records thousands of observed prices. Seller
    # verification is intentionally DISABLED: it is not an eligibility gate
    # for price-drop leads, CeX matching or Telegram research alerts.
    processed = process_crawl(result, config, allow_alerts=notify)
    section_catalogue = make_section_catalogue(
        result,
        previous_catalogue_section,
        section_id=section_id,
        section_name=section_name,
    )
    reuse_cex_metadata(section_catalogue, published_catalogue or {})
    target_options = load_target_config(args.config)
    targets = target_state(published_catalogue or {})
    cex_state: dict = {}
    started = time.monotonic()
    priority_section = any(s.id == section_id and s.priority_rotation for s in load_sections(args.config))
    amazon_discovery_stats = {"search_requests": 0, "candidates": 0, "status": "not_priority_section"}
    discovered = []
    if target_options.enabled:
        import_target_seed(targets, target_options)
        sync_mappings(targets, list(((published_catalogue or {}).get("sections") or {}).values())
                      + [section_catalogue], target_options)
        if priority_section:
            async with httpx.AsyncClient(headers=BROWSER_HEADERS, timeout=20.0) as client:
                await refresh_mapped_quotes(targets, target_options, config, client, cex_state)
                await discover_targets(targets, target_options, config, client, cex_state)
                discovered, amazon_discovery_stats = await discover_amazon_targets(
                    targets, config, client, cex_state,
                    existing_rows=[r for section in ((published_catalogue or {}).get('sections') or {}).values() for r in section.get('items', [])] + section_catalogue['items'],
                )
                by_asin = {r['asin']: r for r in section_catalogue['items']}
                for row in discovered:
                    if row['asin'] in by_asin:
                        by_asin[row['asin']].update(row)
                    else:
                        section_catalogue['items'].append(row)
        attach_target_quotes(section_catalogue["items"], targets)
    urgent = [
        row["asin"]
        for row in (processed.rapid_drops + processed.premium_drops + processed.drops)
        if (row.get("drop_percent") or 0) >= 25 or float(row.get("price_eur") or 0) >= config.premium_min_price_eur
    ]
    if priority_section:
        seen_urgent = set(urgent)
        keywords = [k.lower() for k in config.cex_priority_keywords]
        # Oldest keyword opportunities progress instead of repeatedly selecting
        # the first bestseller cards; expensive items win ties.
        keyword_rows = sorted(section_catalogue.get("items") or [], key=lambda r: (
            bool(r.get("last_checked_at")), r.get("last_checked_at") or "",
            -float(r.get("price_eur") or 0),
        ))
        for row in keyword_rows:
            if not row.get("active", True) or row.get("price_eur") is None:
                continue
            hay = f"{row.get('title') or ''} {row.get('category') or ''}".lower()
            if any(k in hay for k in keywords) and row["asin"] not in seen_urgent:
                urgent.append(row["asin"])
                seen_urgent.add(row["asin"])
            if len(urgent) >= 45:
                break
    relevant_items = []
    for row in section_catalogue.get("items") or []:
        allowed, reason, brand = relevance(row)
        row.update(resale_eligible=allowed, resale_reason=reason, resale_brand=brand)
        if allowed or (target_options.enabled and row.get("cex_target_box_id") and reason != "explicitly_excluded_brand"):
            relevant_items.append(row)
    target_candidates = sorted((r for r in relevant_items if r.get("cex_target_box_id")),
                               key=lambda r: (r.get("last_checked_at") or "", r["asin"]))
    urgent = [r["asin"] for r in target_candidates[:12]] + urgent
    batch = select_check_batch(
        {**section_catalogue, "items": relevant_items}, limit=config.catalogue_checks_per_run,
        priority_asins=urgent,
    )
    drop_by_asin = {row["asin"]: row for row in processed.drops}
    candidates = [
        {
            **row,
            "drop_percent": drop_by_asin.get(row["asin"], {}).get("drop_percent"),
            "drop_tier": drop_by_asin.get(row["asin"], {}).get("drop_tier"),
            "reference_eur": drop_by_asin.get(row["asin"], {}).get("reference_eur"),
            "observed_at": section_catalogue["generated_at"],
        }
        for row in batch if float(row.get('price_eur') or 0) > 0
    ]
    # CeX quotes are cached independently of changing Amazon listing prices.
    quote_cache: dict[str, dict] = {
        row["asin"]: row["cex_quote_cache"]
        for row in section_catalogue["items"]
        if row.get("asin") and isinstance(row.get("cex_quote_cache"), dict)
    }
    lookups: list[dict] = []
    flips: list[dict] = []
    checked_batch: list[dict] = []
    # Small batches and a time budget keep GitHub Actions from timing out.
    # No additional Amazon product-page/seller requests are made here.
    async with httpx.AsyncClient(headers=BROWSER_HEADERS, timeout=20.0) as client:
        for offset in range(0, len(candidates), 20):
            if time.monotonic() - started >= config.catalogue_time_budget_seconds:
                logger.warning("[%s] CeX research time budget reached; remaining ASINs stay queued", section_id)
                break
            group = candidates[offset:offset + 20]
            part_lookups, part_flips = await enrich_with_cex(
                group, config, client=client, max_lookups=len(group),
                cache=quote_cache, run_state=cex_state,
            )
            checked_batch.extend(group)
            lookups.extend(part_lookups)
            flips.extend(part_flips)
            logger.info("[%s] CeX research %s/%s queued, live=%s cache=%s deferred=%s",
                        section_id, len(checked_batch), len(candidates),
                        cex_state.get("live_lookups", 0),
                        cex_state.get("cache_hits", 0),
                        cex_state.get("deferred", 0))
            if cex_state.get("rate_limited"):
                logger.warning("[%s] CeX limited requests: stop this section's CeX lookups", section_id)
                break

    # One shared detail-page budget serves CeX identity, known drops and
    # first-seen high-value list-price research. No separate verification crawl.
    policy = load_alert_policy()
    detail_config = replace(config, premium_spot_check_limit=max(
        0, config.premium_spot_check_limit - amazon_discovery_stats['search_requests']))
    detail_rows = choose_detail_checks(
        section_catalogue, lookups,
        processed.rapid_drops + processed.premium_drops + processed.drops,
        config=detail_config, policy=policy,
    )
    direct_asins = {r['asin'] for r in discovered}
    detail_rows = (discovered + [r for r in detail_rows if r['asin'] not in direct_asins])[:detail_config.premium_spot_check_limit]
    if amazon_discovery_stats['status'] in ('provider_blocked', 'provider_cooldown'):
        detail_rows = []
    try:
        blocked_until = datetime.fromisoformat(targets.get('amazon_blocked_until') or '')
        if blocked_until.tzinfo and datetime.now(timezone.utc) < blocked_until:
            detail_rows = []
    except ValueError:
        pass
    snapshots = {}
    diagnostics = {}
    async with httpx.AsyncClient(headers=BROWSER_HEADERS, timeout=30.0) as detail_client:
        processed.alerts.extend(await spot_check_list_prices(
            detail_client, detail_rows, config=detail_config, allow_alerts=notify,
            delay_seconds=config.request_delay_seconds, snapshots=snapshots, diagnostics=diagnostics,
        ))
    detail_stats = apply_detail_checks(
        section_catalogue, detail_rows, snapshots, lookups, quote_cache, diagnostics=diagnostics,
    )
    if 'provider_blocked' in diagnostics.values():
        targets['amazon_blocked_until'] = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    flips = [row for row in lookups if row.get("cex_cash_flip")]
    eligible_asins = {
        asin for asin, (item, _) in result.items.items()
        if relevance({"title": item.title})[0]
    } | {row["asin"] for row in lookups if relevance(row)[0]}
    processed.alerts = [alert for alert in processed.alerts if alert.asin in eligible_asins]
    logger.info("[%s] Product detail research: %s", section_id, detail_stats)

    apply_batch_results(
        section_catalogue, checked_batch, [], {}, lookups,
        cex_enabled=config.cex_enabled, quote_cache=quote_cache,
    )
    if target_options.enabled:
        sync_mappings(targets, [section_catalogue], target_options)
        section_catalogue["cex_targets"] = targets
    section_catalogue["checked_this_run"] = len(checked_batch)
    processed.catalogue_stats = catalogue_progress(
        section_catalogue, checked_this_run=len(checked_batch),
    )
    processed.verified_asins = set()
    processed.verification_stats = {"status": "disabled"}
    processed.cex_lookups = lookups
    processed.cex_flips = sorted(
        [row for row in flips if (row.get("drop_percent") or 0) >= 30],
        key=lambda row: row.get("cex_cash_profit_eur") or 0, reverse=True,
    )
    processed.cex_arbitrage = sorted(
        [row for row in flips if (row.get("drop_percent") or 0) < 30],
        key=lambda row: row.get("cex_cash_profit_eur") or 0, reverse=True,
    )
    processed.cex_stats = {
        "deep_candidates": sum((row.get("drop_percent") or 0) >= 30 for row in checked_batch),
        "any_price_candidates": sum((row.get("drop_percent") or 0) < 30 for row in checked_batch),
        "attempted": cex_state.get("live_lookups", 0) + cex_state.get("cache_hits", 0),
        "live_lookups": cex_state.get("live_lookups", 0),
        "cache_hits": cex_state.get("cache_hits", 0),
        "deferred": cex_state.get("deferred", 0),
        "errors": cex_state.get("errors", 0),
        "rate_limited": bool(cex_state.get("rate_limited", False)),
        "provisional_matches": len(lookups),
        "brand_not_prioritised": len(section_catalogue["items"]) - len(relevant_items),
        "detail_checks": detail_stats,
        "cex_first": {**target_funnel(targets, config),
                      "catalogue_candidates": len(target_candidates),
                      "catalogue_searches": cex_state.get("target_searches", 0),
                      "mapped_quote_refreshes": cex_state.get("mapped_quote_refreshes", 0)},
        "amazon_target_discovery": amazon_discovery_stats,
        "exact_matches": sum(r.get("match_confidence") in ("barcode_verified", "model_verified") for r in lookups),
        "title_only_matches": sum(r.get("match_confidence") == "title_only" for r in lookups),
        "lookups": len(lookups),
        "cash_flips": len(processed.cex_flips),
        "arbitrage": len(processed.cex_arbitrage),
    }
    logger.info("[%s] Seller verification disabled; %s observed drops; %s CeX research matches; cache %s",
                section_id, len(processed.drops), len(lookups), cex_state)

    repo = os.environ.get("GITHUB_REPOSITORY") or "paddykeoghgoode/amazon-ie-price-error-tracker"
    owner, _, name = repo.partition("/")
    dashboard_url = f"https://{owner}.github.io/{name}/"
    payload = export_deals(
        result,
        processed,
        github_repo=repo,
        config=config,
        section_id=section_id,
        section_name=section_name,
    )
    # Keep a buy-opportunity alert fingerprint across Actions runs, and only
    # mark it after the notifier confirms delivery. A deeper confirmed drop may
    # alert again; repeatedly rechecking the same price should not spam Telegram.
    catalogue_rows = {row["asin"]: row for row in section_catalogue["items"]}
    def should_send_cex(row: dict) -> bool:
        # Mapped opportunities have one notification owner: the fast workflow.
        # It checks today's product-page identity and price before sending.
        if target_options.enabled and row.get("asin") in targets["mappings"]:
            return False
        if not row.get("cex_quote_fresh_for_alert"):
            return False
        previous = (catalogue_rows.get(row.get("asin")) or {}).get("last_cex_alert") or {}
        try:
            last_at = datetime.fromisoformat(previous.get("sent_at") or "")
            age = datetime.now(timezone.utc) - last_at
        except (TypeError, ValueError):
            return True
        # Same-price opportunities must not re-alert every day. Only a
        # further material discount warrants another phone notification.
        prior_price = float(previous.get("price_eur") or 0)
        current_price = float(row.get("price_eur") or 0)
        return (
            age >= timedelta(0)
            and prior_price - current_price >= 25
            and prior_price > 0
            and (prior_price - current_price) / prior_price >= 0.10
        )

    def mark_cex_sent(row: dict) -> None:
        target = catalogue_rows.get(row.get("asin"))
        if target is not None:
            target["last_cex_alert"] = {
                "price_eur": row.get("price_eur"),
                "sent_at": datetime.now(timezone.utc).isoformat(),
                "cex_cash_eur": row.get("cex_cash_eur"),
            }

    if notify:
        seen_alert_asins: set[str] = set()
        unique_alerts: list = []
        for alert in processed.alerts:
            if alert.asin in seen_alert_asins:
                continue
            seen_alert_asins.add(alert.asin)
            unique_alerts.append(alert)
        processed.alerts = unique_alerts
        if processed.alerts:
            await send_alerts(processed.alerts, config, details_url=dashboard_url)
        if processed.cex_flips:
            await send_cex_alerts(
                processed.cex_flips, config, dashboard_url=dashboard_url, deep_drop=True,
                should_send=should_send_cex, on_sent=mark_cex_sent,
            )
        if processed.cex_arbitrage:
            await send_cex_alerts(
                processed.cex_arbitrage,
                config, dashboard_url=dashboard_url,
                deep_drop=False, max_alerts=config.cex_any_price_max_alerts_per_run,
                should_send=should_send_cex, on_sent=mark_cex_sent,
            )
        # Run status, health and all research leads are visible on GitHub/Pages.
        # No Telegram/WhatsApp scan-complete, welcome, health or daily digests.
        # The only messages are qualifying high-ticket drop/CeX leads.
    # export_deals was created before the async notifications. Refresh delivery
    # flags after sends so Pages doesn't say "Telegram pending" for sent alerts.
    payload["alerts"] = db.recent_alerts(limit=50)
    return payload, section_catalogue


async def run(args: argparse.Namespace) -> None:
    base = load_config(args.config)
    if args.max_items is not None:
        base.max_items = args.max_items
    # Published baseline checkpoints survive GitHub cache eviction. Import before scanning.
    if args.merge_previous and args.merge_previous.exists():
        previous = read_snapshot(args.merge_previous)
        for section_data in (previous.get("sections") or {}).values():
            db.import_price_baselines(section_data.get("price_baselines") or {})
        # Also support a snapshot produced before sectioned checkpoints.
        db.import_price_baselines(previous.get("price_baselines") or {})
    sections = load_sections(args.config)
    section_id = resolve_section_id(args, sections)
    notify = not args.no_notify
    previous_catalogue = (
        read_snapshot(args.previous_catalogue)
        if args.previous_catalogue and args.previous_catalogue.exists() else {}
    )
    existing_sections = previous_catalogue.get("sections") or {}
    fragment = None

    if section_id == "all":
        merged: dict | None = None
        complete_catalogue = previous_catalogue
        for section in sections:
            if section.id == "all":
                continue
            config = config_for_section(base, section)
            part, section_inventory = await crawl_section(
                config, section_id=section.id, section_name=section.name,
                args=args, notify=notify,
                previous_catalogue_section=existing_sections.get(section.id),
                published_catalogue=complete_catalogue,
            )
            merged = merge_section_deals(merged, part)
            complete_catalogue = merge_catalogue(complete_catalogue, section_inventory)
            args.skip_health = section.id != sections[-1].id
        payload = merged or {}
        payload["section_id"] = "all"
        payload["section_name"] = "All sections"
        fragment = complete_catalogue
    else:
        section = next((section for section in sections if section.id == section_id), None)
        if section is None:
            raise SystemExit(f"Unknown section {section_id!r}. Options: {[s.id for s in sections]}")
        config = config_for_section(base, section)
        payload, fragment = await crawl_section(
            config,
            section_id=section.id,
            section_name=section.name,
            args=args,
            notify=notify,
            previous_catalogue_section=existing_sections.get(section.id),
            published_catalogue=previous_catalogue,
        )
        if not args.publish_fragment and args.merge_previous and args.merge_previous.exists():
            previous = read_snapshot(args.merge_previous)
            payload = merge_section_deals(previous, payload)

    if args.catalogue_output is not None:
        write_snapshot(args.catalogue_output, fragment or {})
        logger.info("Wrote full catalogue batch to %s", args.catalogue_output)

    # Actions publishing re-merges the raw section against the freshest site state.
    write_snapshot(args.output, payload)
    if args.markdown:
        args.markdown.write_text(render_deals_markdown(payload), encoding="utf-8")
    logger.info("Wrote %s (section=%s)", args.output, section_id)


def main() -> None:
    args = parse_args()
    settings.database_path = args.database
    db.init_db()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
