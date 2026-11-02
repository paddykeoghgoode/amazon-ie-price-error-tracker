from __future__ import annotations

import argparse
import hashlib
import asyncio
import json
import logging
import os
from pathlib import Path

import httpx

from app.config import settings
from app import database as db
from app.scraper.amazon_ie import extract_asin
from app.services.watch_links import (
    public_issue_url, resolve_amazon_shortlink, safe_amazon_url,
    shortlinks_in_text,
)
from app.services.monitor import add_watch_url, run_once, send_notification
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
    return "watch" in labels or title.startswith("watch:")


def _issue_fingerprint(issue: dict) -> str:
    """Cache shortened-link resolution until the user actually edits the issue."""
    source = f"{issue.get('title') or ''}\0{issue.get('body') or ''}"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


async def fetch_watch_issue_urls(
    repo: str,
    token: str,
    *,
    previous_submissions: list[dict] | None = None,
    submissions: list[dict] | None = None,
) -> list[str]:
    """Collect open issues, resolving amzn.eu on an unauthenticated client."""
    if not repo:
        return []
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    remembered = {
        int(item["issue_number"]): item
        for item in (previous_submissions or [])
        if isinstance(item, dict) and str(item.get("issue_number") or "").isdigit()
    }
    urls: list[str] = []
    api = f"https://api.github.com/repos/{repo}/issues"
    async with (
        httpx.AsyncClient(timeout=30.0, headers=headers) as github_client,
        httpx.AsyncClient(timeout=15.0) as amazon_client,
    ):
        page = 1
        while page <= 5:
            resp = await github_client.get(
                api, params={"state": "open", "per_page": 100, "page": page},
            )
            if resp.status_code == 404:
                logger.warning("GitHub repo %s not found when listing watch issues", repo)
                break
            resp.raise_for_status()
            issues = resp.json()
            if not isinstance(issues, list) or not issues:
                break
            for issue in issues:
                if issue.get("pull_request") or not _is_watch_issue(issue):
                    continue
                number = int(issue["number"])
                title, body = issue.get("title") or "", issue.get("body") or ""
                fingerprint = _issue_fingerprint(issue)
                asins = extract_asins_from_text(title, body)
                shortlinks = shortlinks_in_text(title, body)
                if not asins:
                    previous = remembered.get(number) or {}
                    previously_resolved = previous.get("asin")
                    if (
                        previous.get("source_fingerprint") == fingerprint
                        and previously_resolved
                        and extract_asin(str(previously_resolved)) == previously_resolved
                    ):
                        asins = [previously_resolved]
                    else:
                        for link in shortlinks[:3]:
                            asin = await resolve_amazon_shortlink(amazon_client, link)
                            if asin and asin not in asins:
                                asins.append(asin)
                        if not asins and shortlinks:
                            logger.warning(
                                "Shortened Amazon URL in issue #%s could not be resolved", number,
                            )
                urls.extend(asins)
                if submissions is not None:
                    submissions.append({
                        "issue_number": number,
                        "issue_url": public_issue_url(repo, number),
                        "asin": asins[0] if asins else None,
                        "source_fingerprint": fingerprint,
                        "status": "pending_scan" if asins else "link_unresolved",
                    })
            if len(issues) < 100:
                break
            page += 1
    return list(dict.fromkeys(urls))


async def resolve_watchlist_entries(urls: list[str]) -> list[str]:
    """watchlist.json can contain short links as well as full URLs and ASINs."""
    resolved: list[str] = []
    async with httpx.AsyncClient(timeout=15.0) as amazon_client:
        for original in urls:
            asin = extract_asin(original)
            if not asin:
                for link in shortlinks_in_text(original)[:1]:
                    asin = await resolve_amazon_shortlink(amazon_client, link)
                    if asin:
                        break
            if asin:
                resolved.append(asin)
            else:
                logger.warning("Skipping unknown watchlist entry; use an Amazon URL or ASIN")
    return list(dict.fromkeys(resolved))


async def comment_on_new_watch_issue(
    repo: str, token: str, submissions: list[dict],
) -> None:
    """Confirm a new or edited issue; scheduled scans never comment again."""
    if not token or os.environ.get("GITHUB_EVENT_NAME") != "issues":
        return
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        return
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        if event.get("action") not in {"opened", "edited", "reopened"}:
            return
        number = int(event["issue"]["number"])
    except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
        return
    request = next((x for x in submissions if x.get("issue_number") == number), None)
    if not request:
        return
    asin = request.get("asin")
    if asin:
        message = (
            f"✅ Tracking request processed: **{asin}** — {safe_amazon_url(asin)}\n\n"
            "The watchlist scans this product on future runs. Its current "
            "price or any Amazon fetch error will appear in the "
            f"[Watchlist dashboard](https://{repo.split('/')[0]}.github.io/{repo.split('/')[1]}/#watchlist). "
            "Seller, condition and checkout price are not verified."
        )
    else:
        message = (
            "⚠️ I couldn't identify an Amazon product from this request. "
            "Please edit the issue with a full Amazon product URL "
            "(https://www.amazon.ie/dp/ASIN) or its 10-character ASIN. "
            "Short links can occasionally be blocked by Amazon; the next "
            "watchlist scan will retry them."
        )
    try:
        async with httpx.AsyncClient(timeout=15.0, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }) as client:
            result = await client.post(
                f"https://api.github.com/repos/{repo}/issues/{number}/comments",
                json={"body": message},
            )
            result.raise_for_status()
    except httpx.HTTPError as exc:
        logger.warning("Could not acknowledge watch issue #%s (%s)", number, type(exc).__name__)


def annotate_watch_submissions(submissions: list[dict], snapshot: dict) -> list[dict]:
    products = {p.get("asin"): p for p in snapshot.get("products") or []}
    for item in submissions:
        asin = item.get("asin")
        if not asin:
            continue
        product = products.get(asin)
        if not product:
            item["status"] = "pending_scan"
        elif product.get("status") == "fetch_error":
            item["status"] = "fetch_error"
        elif product.get("price_eur") is not None:
            item["status"] = "tracking"
        else:
            item["status"] = "price_unavailable"
        item["checked_at"] = product.get("checked_at") if product else None
    return submissions


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
    parser.add_argument("--markdown", type=Path, default=None, help="Also write a Markdown report here")
    parser.add_argument("--skip-fetch", action="store_true", help="Export current DB without hitting Amazon")
    parser.add_argument(
        "--no-summary",
        action="store_true",
        help="Skip the post-scan Telegram/WhatsApp summary (alerts still send)",
    )
    return parser.parse_args()


async def send_watchlist_summary(payload: dict) -> None:
    products = payload.get("products") or []
    price_errors = sum(1 for p in products if p.get("status") == "price_error")
    fetch_errors = sum(1 for p in products if p.get("status") == "fetch_error")
    url = payload.get("pages_url") or "https://github.io/"
    message = (
        "✅ Watchlist scan done\n\n"
        f"Products watched: {len(products)}\n"
        f"Price errors now: {price_errors}\n"
        f"Fetch failed: {fetch_errors}\n\n"
        f"{url}"
    )
    await send_notification(message)


def main() -> None:
    args = parse_args()
    db.init_db()

    previous = read_snapshot(args.previous) if args.previous and args.previous.exists() else {}
    if previous and not db.list_products():
        logger.info("Importing previous snapshot from %s", args.previous)
        import_snapshot(previous)

    env_repo = os.environ.get("GITHUB_REPOSITORY")
    repo = env_repo or "paddykeoghgoode/amazon-ie-price-error-tracker"
    token = os.environ.get("GITHUB_TOKEN", "")
    watch_urls = load_watchlist(args.watchlist)
    issue_submissions: list[dict] = []
    issue_urls = (
        asyncio.run(fetch_watch_issue_urls(
            repo, token, previous_submissions=previous.get("watch_submissions") or [],
            submissions=issue_submissions,
        ))
        if env_repo else []
    )
    configured_asins = asyncio.run(resolve_watchlist_entries(watch_urls))
    add_all(configured_asins + issue_urls)
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
    payload["watch_submissions"] = annotate_watch_submissions(issue_submissions, payload)
    write_snapshot(args.output, payload)
    if not args.skip_fetch and not args.no_summary:
        asyncio.run(send_watchlist_summary(payload))
    if env_repo:
        asyncio.run(comment_on_new_watch_issue(repo, token, issue_submissions))
    if args.markdown:
        args.markdown.write_text(render_tracker_markdown(payload), encoding="utf-8")
    logger.info("Wrote snapshot to %s", args.output)


if __name__ == "__main__":
    main()
