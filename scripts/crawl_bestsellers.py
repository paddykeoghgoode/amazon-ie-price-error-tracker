from __future__ import annotations

import argparse
import asyncio
import logging
import os
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
from app.services.monitor import record_and_detect
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
from app.services.cex_arbitrage import enrich_with_cex, select_any_price_candidates
from app.services.health import send_health_messages
from app.services.monitor import BROWSER_HEADERS
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
        f"CeX cash flips: {flips} (on sale) · {arb} (regular Amazon price)\n\n"
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
) -> dict:
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
    deep_candidates = [
        d for d in processed.drops if d.get("drop_tier") is not None and (d.get("price_eur") or 0) >= 20
    ]
    deep_candidates.sort(key=lambda d: d.get("drop_percent") or 0, reverse=True)
    any_candidates = select_any_price_candidates(result, config, skip_asins={d['asin'] for d in deep_candidates})
    combined = deep_candidates + any_candidates
    async with httpx.AsyncClient(headers=BROWSER_HEADERS, timeout=30.0) as client:
        verified, verification_stats = await verify_candidates(client, combined, limit=config.verify_max_candidates)
        verified_by_asin = {row['asin']: row for row in verified}
        deep_candidates = [verified_by_asin[d['asin']] for d in deep_candidates if d['asin'] in verified_by_asin]
        any_candidates = [verified_by_asin[d['asin']] for d in any_candidates if d['asin'] in verified_by_asin]
        processed.drops = [verified_by_asin[d['asin']] for d in processed.drops if d['asin'] in verified_by_asin]
        processed.verified_asins = set(verified_by_asin)
        processed.verification_stats = verification_stats
        # Only confirmed Amazon-owned offers may generate actionable alerts.
        # Historical observations from bestseller pages remain useful for baselines.
        for row in verified:
            pid = db.add_product(row['asin'], row['url'], row.get('title'))
            alert = record_and_detect(
                pid,
                ProductSnapshot(
                    asin=row['asin'], url=row['url'], title=row.get('title'),
                    price_eur=row['price_eur'], list_price_eur=None,
                    currency='EUR', availability='In stock',
                    seller_name=row.get('seller_name'), seller_status='amazon',
                ),
                known_title=row.get('title'),
                cooldown_minutes=config.alert_cooldown_minutes,
                baseline_max_age_days=config.baseline_max_age_days,
            )
            if alert:
                processed.alerts.append(alert)
        logger.info('[%s] Amazon seller checks: %s', section_id, verification_stats)

        lookups, flips = await enrich_with_cex(
            deep_candidates, config, client=client, max_lookups=config.cex_max_lookups
        )
        arb_lookups, arb_flips = await enrich_with_cex(
            any_candidates,
            config,
            client=client,
            max_lookups=config.cex_any_price_max_lookups,
        )
    processed.cex_lookups = lookups + arb_lookups
    processed.cex_flips = flips
    processed.cex_arbitrage = arb_flips
    processed.cex_stats = {
        "deep_candidates": len(deep_candidates),
        "any_price_candidates": len(any_candidates),
        "lookups": len(processed.cex_lookups),
        "cash_flips": len(flips),
        "arbitrage": len(arb_flips),
    }

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
    if notify:
        if processed.alerts:
            await send_alerts(processed.alerts, config, details_url=dashboard_url)
        if processed.cex_flips:
            await send_cex_alerts(processed.cex_flips, config, dashboard_url=dashboard_url, deep_drop=True)
        if processed.cex_arbitrage:
            await send_cex_alerts(
                processed.cex_arbitrage,
                config,
                dashboard_url=dashboard_url,
                deep_drop=False,
                max_alerts=config.cex_any_price_max_alerts_per_run,
            )
        if not args.skip_health:
            await send_health_messages(result, processed, config, dashboard_url=dashboard_url)
        await send_section_summary(section_name, payload, dashboard_url)
    return payload


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

    if section_id == "all":
        merged: dict | None = None
        for section in sections:
            if section.id == "all":
                continue
            config = config_for_section(base, section)
            part = await crawl_section(
                config, section_id=section.id, section_name=section.name, args=args, notify=notify
            )
            merged = merge_section_deals(merged, part)
            args.skip_health = section.id != sections[-1].id
        payload = merged or {}
        payload["section_id"] = "all"
        payload["section_name"] = "All sections"
    else:
        section = next((s for s in sections if s.id == section_id), None)
        if section is None:
            raise SystemExit(f"Unknown section {section_id!r}. Options: {[s.id for s in sections]}")
        config = config_for_section(base, section)
        payload = await crawl_section(
            config, section_id=section.id, section_name=section.name, args=args, notify=notify
        )
        if not args.publish_fragment and args.merge_previous and args.merge_previous.exists():
            previous = read_snapshot(args.merge_previous)
            payload = merge_section_deals(previous, payload)

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
