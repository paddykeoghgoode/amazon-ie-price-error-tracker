from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app import database as db
from app.config import settings
from app.services.bestsellers import CrawlResult, ProcessResult
from app.snapshot import _fmt_dublin, _fmt_euro


def export_deals(
    result: CrawlResult, processed: ProcessResult, *, github_repo: str, max_drops: int = 50
) -> dict[str, Any]:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "github_repo": github_repo,
        "telegram_configured": settings.telegram_enabled,
        "min_discount_percent": settings.min_discount_percent,
        "blocked": result.blocked,
        "requests": result.requests,
        "errors": result.errors,
        "categories_crawled": len(result.categories),
        "items_seen": len(result.items),
        "items_priced": processed.priced,
        "items_with_history": processed.with_history,
        "new_alerts": len(processed.alerts),
        "drops": processed.drops[:max_drops],
        "alerts": db.recent_alerts(limit=50),
    }


def render_deals_markdown(payload: dict[str, Any]) -> str:
    telegram = "ready" if payload.get("telegram_configured") else "not configured (add Actions secrets)"
    lines = [
        "# Amazon.ie bestseller price errors",
        "",
        f"Last crawl: **{_fmt_dublin(payload.get('generated_at'))}** · Telegram: **{telegram}**",
        "",
        f"- Items tracked this crawl: **{payload.get('items_seen', 0):,}** "
        f"({payload.get('items_priced', 0):,} with a price) across "
        f"{payload.get('categories_crawled', 0):,} bestseller lists",
        f"- Items with enough history to judge: **{payload.get('items_with_history', 0):,}** "
        "(an item needs 3 crawls before it can be flagged)",
        f"- Flag rule: price ≥{payload.get('min_discount_percent', 75):g}% below that item's recent median price",
        f"- Pages fetched: {payload.get('requests', 0)} ({payload.get('errors', 0)} failed)",
    ]
    if payload.get("blocked"):
        lines.extend(
            [
                "",
                "> **Amazon blocked this crawl partway through** (captcha or repeated errors). "
                "Results above only cover the lists fetched before that.",
            ]
        )

    lines.extend(["", "## Price-error alerts", ""])
    alerts = payload.get("alerts") or []
    if not alerts:
        lines.append("_No suspected errors yet._")
    for alert in alerts:
        title = alert.get("product_title") or alert.get("asin") or "Alert"
        sent = "Telegram sent" if alert.get("notified_telegram") else "not sent"
        lines.append(
            f"- [{title}]({alert.get('url')}) — **{_fmt_euro(alert.get('price_eur'))}** — "
            f"{alert.get('reason') or ''} — {_fmt_dublin(alert.get('created_at'))} · {sent}"
        )

    lines.extend(["", "## Biggest drops this crawl", ""])
    drops = payload.get("drops") or []
    if not drops:
        lines.append("_No item is noticeably below its usual price right now._")
    else:
        lines.extend(["| Product | Now | Usual | Drop | List |", "| --- | --- | --- | --- | --- |"])
        for d in drops:
            title = (d.get("title") or d.get("asin") or "").replace("|", "/")
            rank = f"#{d['rank']} in " if d.get("rank") else ""
            lines.append(
                f"| [{title}]({d.get('url')}) | {_fmt_euro(d.get('price_eur'))} | "
                f"{_fmt_euro(d.get('median_eur'))} | {d.get('drop_percent'):g}% | "
                f"{rank}{(d.get('category') or '').replace('|', '/')} |"
            )
    lines.extend(
        [
            "",
            "---",
            "",
            "Rewritten by the **Crawl bestsellers** GitHub Action. Categories are set in "
            "[`bestsellers.json`](bestsellers.json). Times are Ireland (Europe/Dublin).",
            "",
        ]
    )
    return "\n".join(lines) + "\n"
