from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

import httpx

from app.config import settings
from app import database as db
from app.scraper.amazon_ie import extract_asin
from app.services.monitor import add_watch_url, run_once
from app.snapshot import (
    export_snapshot,
    import_snapshot,
    read_snapshot,
    render_tracker_markdown,
    write_snapshot,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("scan")


def load_watchlist(path: Path) -> list[str]:
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    urls: list[str] = []
    for item in payload.get("products") or []:
        if isinstance(item, str):
            urls.append(item)
        elif isinstance(item, dict):
            value = item.get("url") or item.get("asin")
            if value:
                urls.append(str(value))
    return urls


def extract_asins_from_text(*parts: str | None) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if not part:
            continue
        for token in part.replace("\n", " ").split():
            asin = extract_asin(token.strip("<>()[].,;\"'"))
            if asin and asin not in seen:
                seen.add(asin)
                found.append(asin)
        # Also try the whole blob in case it's a raw ASIN line
        blob = extract_asin(part.strip())
        if blob and blob not in seen:
            seen.add(blob)
            found.append(blob)
    return found


def _is_watch_issue(issue: dict) -> bool:
    title = (issue.get("title") or "").strip().lower()
    labels = {str(lbl.get("name", "")).lower() for lbl in (issue.get("labels") or []) if isinstance(lbl, dict)}
    return "watch" in labels or title.startswith("watch")


async def fetch_watch_issue_urls(repo: str, token: str) -> list[str]:
    if not repo:
        return []
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    urls: list[str] = []
    api = f"https://api.github.com/repos/{repo}/issues"
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        page = 1
        while page <= 5:
            resp = await client.get(
                api,
                params={"state": "open", "per_page": 100, "page": page},
            )
            if resp.status_code == 404:
                logger.warning("GitHub repo %s not found when listing watch issues", repo)
                break
            resp.raise_for_status()
            issues = resp.json()
            if not issues:
                break
            for issue in issues:
                if issue.get("pull_request"):
                    continue
                if not _is_watch_issue(issue):
                    continue
                asins = extract_asins_from_text(issue.get("title"), issue.get("body"))
                urls.extend(asins)
            if len(issues) < 100:
                break
            page += 1
    return urls


def add_all(urls: list[str]) -> None:
    seen: set[str] = set()
    for raw in urls:
        asin = extract_asin(raw)
        if not asin or asin in seen:
            continue
        seen.add(asin)
        try:
            add_watch_url(asin)
        except ValueError as exc:
            logger.warning("Skipping %s: %s", raw, exc)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Scan watchlist and write GitHub Pages data.json")
    parser.add_argument("--watchlist", type=Path, default=Path("watchlist.json"))
    parser.add_argument("--previous", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=Path("docs/data.json"))
    parser.add_argument("--markdown", type=Path, default=Path("TRACKER.md"))
    parser.add_argument("--skip-fetch", action="store_true", help="Export current DB without hitting Amazon")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    db.init_db()

    if args.previous and args.previous.exists() and not db.list_products():
        logger.info("Importing previous snapshot from %s", args.previous)
        import_snapshot(read_snapshot(args.previous))

    env_repo = os.environ.get("GITHUB_REPOSITORY")
    repo = env_repo or "paddykeoghgoode/amazon-ie-price-error-tracker"
    token = os.environ.get("GITHUB_TOKEN", "")
    watch_urls = load_watchlist(args.watchlist)
    issue_urls = asyncio.run(fetch_watch_issue_urls(repo, token)) if env_repo else []
    add_all(watch_urls + issue_urls)
    logger.info(
        "Watching %s product(s) (watchlist=%s issues=%s telegram=%s)",
        len(db.list_products(enabled_only=True)),
        len(watch_urls),
        len(issue_urls),
        settings.telegram_enabled,
    )

    if not args.skip_fetch:
        asyncio.run(run_once())

    payload = export_snapshot(github_repo=repo)
    write_snapshot(args.output, payload)
    args.markdown.write_text(render_tracker_markdown(payload), encoding="utf-8")
    logger.info("Wrote snapshot to %s and %s", args.output, args.markdown)


if __name__ == "__main__":
    main()
