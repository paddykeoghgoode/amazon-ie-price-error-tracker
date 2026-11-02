from __future__ import annotations

import argparse
import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app import database as db
from app.bestseller_sections import (
    config_for_all_sections,
    config_for_section,
    load_sections,
    rotate_section_id,
)
from app.config import settings
from app.scraper.amazon_ie import ProductSnapshot
from app.services.monitor import detect_verified_price
from app.deals import export_deals, merge_section_deals, render_deals_markdown
from app.services.bestsellers import (
    crawl,
    httpx_fetcher,
    load_config,
    process_crawl,
    send_alerts,
    send_cex_alerts,
    send_notification,
    verify_candidates,
)
from app.services.cex_arbitrage import enrich_with_cex
from app.services.health import send_health_messages
from app.services.monitor import BROWSER_HEADERS
from app.services.catalogue import (
    make_section_catalogue, select_check_batch, apply_batch_results,
    catalogue_progress, merge_catalogue,
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
        help="Section id from bestsellers.json, 'all' for full crawl, or 'rotate' (uses --run-number)",
    )
    parser.add_argument(
        "--run-number",
        type=int,
        default=None,
        help="GitHub run number for rotate mode (defaults to GITHUB_RUN_NUMBER)",
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
        run_no = args.run_number
        if run_no is None:
            run_no = int(os.environ.get("GITHUB_RUN_NUMBER", "0") or 0)
        return rotate_section_id(sections, run_no)
    if args.section == "all":
        return "all"
    return args.section


async def send_section_summary(section_name: str, payload: dict, dashboard_url: str) -> None:
    stats = payload.get("cex_stats") or {}
    flips = int(stats.get("cash_flips") or 0)
    arb = int(stats.get("arbitrage") or 0)
    alerts = int(payload.get("new_alerts") or 0)
    message = (
        f"✅ {section_name} bestseller scan done\n\n"
        f"Price-error alerts this section: {alerts}\n"
        f"CeX cash flips: {flips} (on sale) · {arb} (other)\n"
        f"Catalogue processed: {int((payload.get('catalogue_stats') or {}).get('checked') or 0):,} / "
        f"{int((payload.get('catalogue_stats') or {}).get('priced') or 0):,} priced listings\n"
        f"CeX checks so far: {int((payload.get('catalogue_stats') or {}).get('cex_attempted') or 0):,}\n\n"
        f"{dashboard_url}"
    )
    await send_notification(message)


async def crawl_section(
    config,
    *,
    section_id: str,
    section_name: str,
    args: argparse.Namespace,
    notify: bool,
    previous_catalogue_section: dict | None = None,
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

    processed = process_crawl(result, config)
    section_catalogue = make_section_catalogue(
        result,
        previous_catalogue_section,
        section_id=section_id,
        section_name=section_name,
    )
    # Full ASIN catalogue: never filter by CeX keywords, item price band, or
    # arbitrary top-25/top-50 shortlist. The only cap is work PER RUN, not which
    # listings will ever get examined.
    urgent = [row["asin"] for row in processed.drops if (row.get("drop_percent") or 0) >= 30]
    batch = select_check_batch(
        section_catalogue,
        limit=config.catalogue_checks_per_run,
        priority_asins=urgent,
    )
    drop_by_asin = {row["asin"]: row for row in processed.drops}
    candidates = [
        {**row, "drop_percent": drop_by_asin.get(row["asin"], {}).get("drop_percent"),
         "drop_tier": drop_by_asin.get(row["asin"], {}).get("drop_tier"),
         "reference_eur": drop_by_asin.get(row["asin"], {}).get("reference_eur")}
        for row in batch
    ]
    outcomes: dict[str, str] = {}
    verified: list[dict] = []
    lookups: list[dict] = []  # eligible seller matches only, safe for deal alerts
    research_lookups: list[dict] = []  # all CeX matches, including seller-unverified
    flips: list[dict] = []
    checked_batch: list[dict] = []
    # The public section catalogue is the durable cache, not the ephemeral
    # GitHub Actions process. Reuse quotes even when Amazon's item price changes.
    quote_cache: dict[str, dict] = {
        row["asin"]: row["cex_quote_cache"]
        for row in section_catalogue["items"]
        if row.get("asin") and isinstance(row.get("cex_quote_cache"), dict)
    }
    cex_state: dict = {}
    verification_stats = {
        "attempted": 0, "amazon_verified": 0, "rejected_or_unknown": 0,
        "rejection_reasons": {}, "sample_failures": [],
    }
    started = time.monotonic()
    # Commit progress in short groups; stop safely after the configured budget
    # rather than letting Actions kill the whole job after a slow Amazon/CeX page.
    async with httpx.AsyncClient(headers=BROWSER_HEADERS, timeout=15.0) as client:
        for offset in range(0, len(candidates), 20):
            if offset and time.monotonic() - started >= config.catalogue_time_budget_seconds:
                logger.warning("[%s] Batch time budget reached, unexamined ASINs remain queued", section_id)
                break
            group = candidates[offset:offset + 20]
            part_verified, part_stats = await verify_candidates(
                client,
                group,
                limit=len(group),
                delay_seconds=config.request_delay_seconds,
                on_result=lambda asin, status: outcomes.__setitem__(asin, status),
            )
            # Search CeX for EVERY priced listing in the group, even when
            # Amazon's seller cannot be determined. Unverified results are
            # kept only as provisional catalogue research, never buy alerts.
            verified_group = {row["asin"]: row for row in part_verified}
            research_candidates = [
                verified_group.get(row["asin"], row) for row in group
            ]
            all_matches, all_flips = await enrich_with_cex(
                research_candidates, config, client=client,
                max_lookups=len(research_candidates),
                cache=quote_cache,
                run_state=cex_state,
            )
            verified_matches = [
                row for row in all_matches if row["asin"] in verified_group
            ]
            verified_flips = [
                row for row in all_flips if row["asin"] in verified_group
            ]
            checked_batch.extend(group)
            verified.extend(part_verified)
            research_lookups.extend(all_matches)
            lookups.extend(verified_matches)
            flips.extend(verified_flips)
            for key in ("attempted", "amazon_verified", "rejected_or_unknown"):
                verification_stats[key] += int(part_stats.get(key) or 0)
            for reason, count in (part_stats.get("rejection_reasons") or {}).items():
                reasons = verification_stats["rejection_reasons"]
                reasons[reason] = reasons.get(reason, 0) + count
            remaining_samples = 5 - len(verification_stats["sample_failures"])
            if remaining_samples > 0:
                verification_stats["sample_failures"].extend(
                    (part_stats.get("sample_failures") or [])[:remaining_samples]
                )
            logger.info(
                "[%s] Verified %s/%s in current progressive batch; %s CeX matches",
                section_id, verification_stats["amazon_verified"], len(checked_batch), len(lookups),
            )
            failures = part_stats.get("rejection_reasons") or {}
            if failures.get("amazon_http_429", 0) >= 2:
                logger.warning("[%s] Amazon is throttling; leaving remaining ASINs queued", section_id)
                break
    verified_by_asin = {row["asin"]: row for row in verified}
    processed.drops = [
        {**drop_by_asin[asin], **row}
        for asin, row in verified_by_asin.items() if asin in drop_by_asin
    ]
    processed.verified_asins = set(verified_by_asin)
    processed.verification_stats = verification_stats
    # process_crawl() already recorded the initial bestseller price. Now that
    # Amazon/FBA seller AND exact offered price are confirmed, evaluate that
    # observation for a real price-error alert without writing it a second time.
    for row in verified:
        product_id = db.add_product(row["asin"], row["url"], row.get("verified_title") or row.get("title"))
        snap = ProductSnapshot(
            asin=row["asin"], url=row["url"], title=row.get("verified_title") or row.get("title"),
            price_eur=row["price_eur"], list_price_eur=None, currency="EUR",
            availability="In stock", seller_name=row.get("seller_name"),
            seller_status="amazon" if row.get("seller_type") == "amazon_retail" else "third_party",
            fulfilment_status=row.get("fulfilment_status") or "unknown",
        )
        alert = detect_verified_price(
            product_id, snap, known_title=row.get("title"),
            cooldown_minutes=config.alert_cooldown_minutes,
        )
        if alert:
            processed.alerts.append(alert)
    logger.info(
        "[%s] Amazon verification: %s/%s products examined, %s",
        section_id, len(checked_batch),
        len([r for r in section_catalogue["items"] if r["price_eur"] is not None]),
        verification_stats,
    )
    if verification_stats["attempted"] >= 10 and not verification_stats["amazon_verified"]:
        logger.warning(
            "[%s] No verified offers from %s checks. Rejection reasons: %s; samples: %s",
            section_id, verification_stats["attempted"],
            verification_stats.get("rejection_reasons"),
            verification_stats.get("sample_failures"),
        )

    apply_batch_results(
        section_catalogue,
        checked_batch,
        verified,
        outcomes,
        research_lookups,
        cex_enabled=config.cex_enabled,
        quote_cache=quote_cache,
    )
    section_catalogue["checked_this_run"] = len(checked_batch)
    processed.catalogue_stats = catalogue_progress(
        section_catalogue, checked_this_run=len(checked_batch)
    )
    processed.cex_lookups = lookups
    processed.cex_flips = sorted(
        [row for row in flips if row.get("drop_percent") is not None and row["drop_percent"] >= 30],
        key=lambda row: row.get("cex_cash_profit_eur") or 0,
        reverse=True,
    )
    processed.cex_arbitrage = sorted(
        [row for row in flips if row not in processed.cex_flips],
        key=lambda row: row.get("cex_cash_profit_eur") or 0,
        reverse=True,
    )
    processed.cex_stats = {
        "deep_candidates": sum((row.get("drop_percent") or 0) >= 30 for row in verified),
        "any_price_candidates": sum((row.get("drop_percent") or 0) < 30 for row in verified),
        "attempted": cex_state.get("live_lookups", 0) + cex_state.get("cache_hits", 0),
        "live_lookups": cex_state.get("live_lookups", 0),
        "cache_hits": cex_state.get("cache_hits", 0),
        "deferred": cex_state.get("deferred", 0),
        "errors": cex_state.get("errors", 0),
        "rate_limited": bool(cex_state.get("rate_limited", False)),
        "provisional_matches": len(research_lookups) - len(lookups),
        "lookups": len(processed.cex_lookups),
        "cash_flips": len(processed.cex_flips),
        "arbitrage": len(processed.cex_arbitrage),
    }
    logger.info(
        "[%s] Catalogue: %s; CeX cache: %s; eligible matches: %s; provisional: %s; profitable: %s",
        section_id, processed.catalogue_stats, cex_state,
        len(lookups), len(research_lookups) - len(lookups), len(flips),
    )

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
        if not row.get("seller_verified") or not row.get("cex_quote_fresh_for_alert"):
            return False
        previous = (catalogue_rows.get(row.get("asin")) or {}).get("last_cex_alert") or {}
        try:
            last_at = datetime.fromisoformat(previous.get("sent_at") or "")
            age = datetime.now(timezone.utc) - last_at
        except (TypeError, ValueError):
            return True
        return (
            age >= timedelta(hours=24)
            or float(row.get("price_eur") or 0) < float(previous.get("price_eur") or 0) - 0.01
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
        if not args.skip_health:
            await send_health_messages(result, processed, config, dashboard_url=dashboard_url)
        await send_section_summary(section_name, payload, dashboard_url)
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
