from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app import database as db
from app.config import settings
from app.services.bestsellers import CrawlConfig, CrawlResult, ProcessResult
from app.services.drop_tiers import tiered_deep_drops
from app.snapshot import _fmt_dublin, _fmt_euro


def export_deals(
    result: CrawlResult,
    processed: ProcessResult,
    *,
    github_repo: str,
    config: CrawlConfig | None = None,
    max_drops: int = 50,
) -> dict[str, Any]:
    tiers = (config.deep_drop_tiers if config else [40.0, 50.0, 66.0])
    payload: dict[str, Any] = {
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
        "deep_drop_tiers": [int(t) for t in tiers],
        "deep_drops": tiered_deep_drops(processed.drops, tiers),
        "cex_enabled": bool(config.cex_enabled if config else True),
        "cex_min_cash_profit_eur": config.cex_min_cash_profit_eur if config else 5.0,
        "cex_min_cash_profit_percent": config.cex_min_cash_profit_percent if config else 10.0,
        "cex_lookups": processed.cex_lookups[:30],
        "cex_flips": processed.cex_flips[:20],
        "cex_arbitrage": processed.cex_arbitrage[:30],
    }
    return payload


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
            tier_note = f" ({d.get('drop_tier')}% tier)" if d.get("drop_tier") else ""
            lines.append(
                f"| [{title}]({d.get('url')}) | {_fmt_euro(d.get('price_eur'))} | "
                f"{_fmt_euro(d.get('median_eur'))} | {d.get('drop_percent'):g}%{tier_note} | "
                f"{rank}{(d.get('category') or '').replace('|', '/')} |"
            )

    lines.extend(["", "## Deep drops (40% / 50% / 66% vs usual Amazon price)", ""])
    deep = payload.get("deep_drops") or {}
    any_deep = False
    for tier in payload.get("deep_drop_tiers") or [40, 50, 66]:
        key = str(int(tier))
        rows = deep.get(key) or []
        if not rows:
            continue
        any_deep = True
        lines.append(f"### ≥{key}% off usual price")
        lines.append("")
        for d in rows[:15]:
            title = (d.get("title") or d.get("asin") or "").replace("|", "/")
            lines.append(
                f"- [{title}]({d.get('url')}) — **{_fmt_euro(d.get('price_eur'))}** "
                f"(was ~{_fmt_euro(d.get('median_eur'))}, **{d.get('drop_percent'):g}%** off)"
            )
        lines.append("")
    if not any_deep:
        lines.append("_Nothing at 40%+ below its recent Amazon median this crawl._")
        lines.append("")

    lines.extend(["", "## CeX cash trade-in (after your profit margin)", ""])
    flips = payload.get("cex_flips") or []
    min_eur = payload.get("cex_min_cash_profit_eur", 5)
    min_pct = payload.get("cex_min_cash_profit_percent", 10)
    lines.append(
        f"CeX match is approximate (search by title). A flip needs at least **€{min_eur:g}** "
        f"or **{min_pct:g}%** profit after buying on Amazon, using CeX **cash** trade-in."
    )
    lines.append("")
    if not flips:
        lines.append("_No cash flips met your margin on deep-drop items this crawl._")
    else:
        for row in flips[:15]:
            title = (row.get("title") or row.get("asin") or "").replace("|", "/")
            lines.append(
                f"- [{title}]({row.get('url')}) — buy **{_fmt_euro(row.get('price_eur'))}**, "
                f"CeX cash **{_fmt_euro(row.get('cex_cash_eur'))}** "
                f"(~**€{row.get('cex_cash_profit_eur'):g}** profit) · "
                f"[CeX listing]({row.get('cex_url')}) · {row.get('drop_percent'):g}% off usual Amazon price"
            )

    lines.extend(["", "## CeX profit without an Amazon price drop", ""])
    arb = payload.get("cex_arbitrage") or []
    lines.append(
        "Scans gaming/electronics-style bestsellers at **normal** Amazon prices and lists any where "
        "CeX cash trade-in still clears your margin (title match only)."
    )
    lines.append("")
    if not arb:
        lines.append("_Nothing met your margin on regular-price items this crawl._")
    else:
        for row in arb[:20]:
            title = (row.get("title") or row.get("asin") or "").replace("|", "/")
            lines.append(
                f"- [{title}]({row.get('url')}) — Amazon **{_fmt_euro(row.get('price_eur'))}**, "
                f"CeX cash **{_fmt_euro(row.get('cex_cash_eur'))}** "
                f"(~**€{row.get('cex_cash_profit_eur'):g}** profit) · [CeX listing]({row.get('cex_url')})"
            )
    lines.extend(
        [
            "",
            "---",
            "",
            "Rewritten by the **Scan and publish tracker** GitHub Action. Categories are set in "
            "[`bestsellers.json`](bestsellers.json). Times are Ireland (Europe/Dublin).",
            "",
        ]
    )
    return "\n".join(lines) + "\n"
