from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

import httpx

from app import database as db
from app.config import settings
from app.deals import export_deals, render_deals_markdown
from app.services.bestsellers import crawl, httpx_fetcher, load_config, process_crawl, send_alerts
from app.services.health import send_health_messages
from app.services.monitor import BROWSER_HEADERS
from app.snapshot import write_snapshot

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
    parser.add_argument("--markdown", type=Path, default=None, help="Also write a Markdown report here")
    parser.add_argument("--max-items", type=int, default=None, help="Override max_items from the config")
    parser.add_argument("--no-notify", action="store_true", help="Detect and report, but send no messages")
    return parser.parse_args()


async def run(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if args.max_items is not None:
        config.max_items = args.max_items

    async with httpx.AsyncClient(headers=BROWSER_HEADERS, timeout=30.0) as client:
        result = await crawl(httpx_fetcher(client), config)
    logger.info(
        "Crawled %s lists with %s requests (%s failed, blocked=%s): %s items",
        len(result.categories),
        result.requests,
        result.errors,
        result.blocked,
        len(result.items),
    )

    processed = process_crawl(result, config)
    logger.info(
        "%s priced, %s with history, %s new alerts",
        processed.priced,
        processed.with_history,
        len(processed.alerts),
    )

    repo = os.environ.get("GITHUB_REPOSITORY") or "paddykeoghgoode/amazon-ie-price-error-tracker"
    owner, _, name = repo.partition("/")
    dashboard_url = f"https://{owner}.github.io/{name}/"
    if not args.no_notify:
        if processed.alerts:
            sent = await send_alerts(processed.alerts, config, details_url=dashboard_url)
            logger.info("Sent %s alert message(s)", sent)
        health = await send_health_messages(result, processed, config, dashboard_url=dashboard_url)
        logger.info("Sent %s status message(s)", len(health))

    payload = export_deals(result, processed, github_repo=repo)
    write_snapshot(args.output, payload)
    if args.markdown:
        args.markdown.write_text(render_deals_markdown(payload), encoding="utf-8")
    logger.info("Wrote %s", args.output)


def main() -> None:
    args = parse_args()
    # Bestseller history lives in its own database so it never mixes with the watch list.
    settings.database_path = args.database
    db.init_db()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
