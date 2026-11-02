from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app import database as db
from app.config import settings
from app.services.bestsellers import CrawlConfig, CrawlResult, ProcessResult
from app.services.drop_tiers import tiered_deep_drops
from app.snapshot import _fmt_dublin, _fmt_euro

_SECTION_STORE_KEYS = (
    "generated_at",
    "section_id",
    "section_name",
    "items_seen",
    "items_priced",
    "items_with_history",
    "categories_crawled",
    "requests",
    "errors",
    "blocked",
    "drops",
    "deep_drops",
    "deep_drop_tiers",
    "cex_flips",
    "cex_arbitrage",
    "cex_near_margin",
    "cex_lookups",
    "cex_stats",
    "cex_min_cash_profit_eur",
    "cex_min_cash_profit_percent",
    "cex_enabled",
    "verification_stats",
    "new_alerts",
    "scan_status",
    "price_baselines",
)


def _dedupe_by_asin(rows: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for row in rows:
        asin = row.get("asin")
        if asin:
            out[str(asin)] = row
    return list(out.values())


def _merge_deep_drops(sections: dict[str, dict]) -> dict[str, list[dict]]:
    tiers: set[int] = set()
    for snap in sections.values():
        for t in snap.get("deep_drop_tiers") or []:
            tiers.add(int(t))
    merged: dict[str, list[dict]] = {str(t): [] for t in sorted(tiers)}
    for snap in sections.values():
        for key, rows in (snap.get("deep_drops") or {}).items():
            merged.setdefault(key, []).extend(rows)
    for key in merged:
        merged[key] = sorted(
            _dedupe_by_asin(merged[key]),
            key=lambda r: r.get("drop_percent") or 0,
            reverse=True,
        )
    return merged


def merge_section_deals(previous: dict | None, incoming: dict) -> dict:
    """Merge one section crawl into the rolling deals snapshot."""
    section_id = str(incoming.get("section_id") or "default")
    sections: dict[str, dict] = dict((previous or {}).get("sections") or {})
    sections[section_id] = {key: incoming[key] for key in _SECTION_STORE_KEYS if key in incoming}
    if incoming.get("section_name"):
        sections[section_id]["section_name"] = incoming["section_name"]

    drops: list[dict] = []
    cex_flips: list[dict] = []
    cex_arbitrage: list[dict] = []
    cex_near: list[dict] = []
    cex_lookups: list[dict] = []
    items_seen = 0
    items_priced = 0
    items_with_history = 0
    categories_crawled = 0
    requests = 0
    errors = 0
    blocked = False
    verify_stats = {"attempted": 0, "amazon_verified": 0, "rejected_or_unknown": 0}
    stats = {"deep_candidates": 0, "any_price_candidates": 0, "lookups": 0, "cash_flips": 0, "arbitrage": 0}

    for snap in sections.values():
        drops.extend(snap.get("drops") or [])
        cex_flips.extend(snap.get("cex_flips") or [])
        cex_arbitrage.extend(snap.get("cex_arbitrage") or [])
        cex_near.extend(snap.get("cex_near_margin") or [])
        cex_lookups.extend(snap.get("cex_lookups") or [])
        items_seen += int(snap.get("items_seen") or 0)
        items_priced += int(snap.get("items_priced") or 0)
        items_with_history += int(snap.get("items_with_history") or 0)
        categories_crawled += int(snap.get("categories_crawled") or 0)
        requests += int(snap.get("requests") or 0)
        errors += int(snap.get("errors") or 0)
        blocked = blocked or bool(snap.get("blocked"))
        for key in verify_stats:
            verify_stats[key] += int((snap.get('verification_stats') or {}).get(key) or 0)
        st = snap.get("cex_stats") or {}
        for key in stats:
            stats[key] += int(st.get(key) or 0)

    drops = sorted(_dedupe_by_asin(drops), key=lambda r: r.get("drop_percent") or 0, reverse=True)
    cex_flips = sorted(
        _dedupe_by_asin(cex_flips), key=lambda r: r.get("cex_cash_profit_eur") or 0, reverse=True
    )
    cex_arbitrage = sorted(
        _dedupe_by_asin(cex_arbitrage), key=lambda r: r.get("cex_cash_profit_eur") or 0, reverse=True
    )
    cex_near = sorted(
        _dedupe_by_asin(cex_near), key=lambda r: r.get("cex_cash_profit_eur") or 0, reverse=True
    )[:15]
    cex_lookups = sorted(
        _dedupe_by_asin(cex_lookups), key=lambda r: r.get("cex_cash_profit_eur") or 0, reverse=True
    )[:30]

    base = previous or {}
    merged: dict[str, Any] = {
        **{k: v for k, v in base.items() if k not in _SECTION_STORE_KEYS and k != "sections"},
        "generated_at": incoming.get("generated_at"),
        "github_repo": incoming.get("github_repo") or base.get("github_repo"),
        "telegram_configured": incoming.get("telegram_configured", base.get("telegram_configured")),
        "min_discount_percent": incoming.get("min_discount_percent", base.get("min_discount_percent", 75)),
        "sections": sections,
        "section_order": list(sections.keys()),
        "last_section_id": section_id,
        "last_section_name": incoming.get("section_name"),
        "last_section_at": incoming.get("generated_at"),
        "items_seen": items_seen,
        "items_priced": items_priced,
        "items_with_history": items_with_history,
        "categories_crawled": categories_crawled,
        "requests": requests,
        "errors": errors,
        "blocked": blocked,
        "drops": drops[:50],
        "deep_drop_tiers": incoming.get("deep_drop_tiers") or base.get("deep_drop_tiers") or [40, 50, 66],
        "deep_drops": _merge_deep_drops(sections),
        "cex_enabled": incoming.get("cex_enabled", base.get("cex_enabled", True)),
        "cex_min_cash_profit_eur": incoming.get("cex_min_cash_profit_eur", base.get("cex_min_cash_profit_eur", 5)),
        "cex_min_cash_profit_percent": incoming.get(
            "cex_min_cash_profit_percent", base.get("cex_min_cash_profit_percent", 10)
        ),
        "cex_flips": cex_flips[:20],
        "cex_arbitrage": cex_arbitrage[:30],
        "cex_near_margin": cex_near,
        "cex_lookups": cex_lookups,
        "cex_stats": stats,
        "verification_stats": verify_stats,
        "price_baselines": {asin: row for snap in sections.values() for asin, row in (snap.get("price_baselines") or {}).items()},
        "alerts": incoming.get("alerts") or base.get("alerts") or [],
    }
    return merged


def export_deals(
    result: CrawlResult,
    processed: ProcessResult,
    *,
    github_repo: str,
    config: CrawlConfig | None = None,
    max_drops: int = 50,
    section_id: str | None = None,
    section_name: str | None = None,
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
        "verification_stats": processed.verification_stats,
        "scan_status": "blocked" if result.blocked else ("partial" if result.errors else "completed"),
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
        "cex_near_margin": sorted(
            [
                row
                for row in processed.cex_lookups
                if not row.get("cex_cash_flip") and (row.get("cex_cash_profit_eur") or 0) > 0
            ],
            key=lambda r: r.get("cex_cash_profit_eur") or 0,
            reverse=True,
        )[:15],
        "cex_stats": processed.cex_stats,
        "price_baselines": db.export_price_baselines(set(result.items)),
    }
    if section_id:
        payload["section_id"] = section_id
    if section_name:
        payload["section_name"] = section_name
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
