from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from app import database as db
from app.config import settings
from app.services.bestsellers import CrawlConfig, CrawlResult, ProcessResult
from app.services.drop_tiers import tiered_deep_drops
from app.services.alert_policy import load_alert_policy
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
    "general_drops",
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
    "seller_verification_enabled",
    "catalogue_stats",
    "new_alerts",
    "scan_status",
    "price_baselines",
    "premium_drops",
    "rapid_drops",
    "premium_min_price_eur",
    "premium_history_drop_percent",
)


def _dedupe_by_asin(rows: list[dict]) -> list[dict]:
    out: dict[str, dict] = {}
    for row in rows:
        asin = row.get("asin")
        if asin:
            out[str(asin)] = row
    return list(out.values())


def _latest_scanned_by_asin(rows: list[dict]) -> list[dict]:
    """Use the newest *section observation* when an ASIN appears more than once."""
    latest: dict[str, dict] = {}
    for row in rows:
        asin = str(row.get("asin") or "")
        if not asin:
            continue
        previous = latest.get(asin)
        if previous is None or str(row.get("last_scanned_at") or "") >= str(previous.get("last_scanned_at") or ""):
            latest[asin] = row
    return list(latest.values())


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


def _unverified_research_rows(rows: list[dict]) -> list[dict]:
    """Prevent historical scans from retaining an obsolete seller-verified flag."""
    cleaned = []
    for original in rows:
        row = {**original}
        for key in ("seller_verified", "seller_name", "seller_type",
                    "seller_status", "fulfilment_status", "verified_at"):
            row.pop(key, None)
        row["seller_check"] = "disabled"
        cleaned.append(row)
    return cleaned


def _disable_seller_claims(section: dict) -> dict:
    snap = {**section, "seller_verification_enabled": False,
            "verification_stats": {"status": "disabled"}}
    for kind in ("drops", "general_drops", "cex_flips", "cex_arbitrage", "cex_near_margin", "cex_lookups"):
        if kind in snap:
            snap[kind] = _unverified_research_rows(snap[kind] or [])
    if "deep_drops" in snap:
        snap["deep_drops"] = {
            tier: _unverified_research_rows(rows or [])
            for tier, rows in (snap["deep_drops"] or {}).items()
        }
    stats = dict(snap.get("catalogue_stats") or {})
    if stats:
        stats["verified"] = 0
        stats["cex_eligible_matched"] = 0
        snap["catalogue_stats"] = stats
    return snap


def merge_section_deals(previous: dict | None, incoming: dict) -> dict:
    """Merge one section crawl into the rolling deals snapshot."""
    section_id = str(incoming.get("section_id") or "default")
    sections: dict[str, dict] = {
        sid: _disable_seller_claims(snap)
        for sid, snap in ((previous or {}).get("sections") or {}).items()
    }
    sections[section_id] = _disable_seller_claims({
        key: incoming[key] for key in _SECTION_STORE_KEYS if key in incoming
    })
    if incoming.get("section_name"):
        sections[section_id]["section_name"] = incoming["section_name"]
    # Keep the first sighting time for the same ASIN and listing price while
    # a section is rescanned; a changed price is a new finding.
    old_section = ((previous or {}).get("sections") or {}).get(section_id) or {}
    for kind in ("drops", "general_drops", "cex_flips", "cex_arbitrage"):
        previous_findings = {
            (row.get("asin"), row.get("price_eur")): row.get("found_at")
            for row in old_section.get(kind) or []
        }
        for row in sections[section_id].get(kind) or []:
            original = previous_findings.get((row.get("asin"), row.get("price_eur")))
            if original:
                row["found_at"] = original

    drops: list[dict] = []
    general_drops: list[dict] = []
    premium_drops: list[dict] = []
    rapid_drops: list[dict] = []
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
    verify_stats = {"status": "disabled"}
    stats = {"deep_candidates": 0, "any_price_candidates": 0, "attempted": 0, "live_lookups": 0, "cache_hits": 0, "deferred": 0, "errors": 0, "rate_limited": 0, "provisional_matches": 0, "lookups": 0, "cash_flips": 0, "arbitrage": 0}
    catalogue_stats = {"discovered": 0, "active": 0, "archived": 0, "priced": 0, "checked": 0, "remaining": 0, "verified": 0, "cex_attempted": 0, "cex_matched": 0, "cex_eligible_matched": 0, "cex_cache_records": 0, "unpriced": 0, "checked_this_run": 0}

    for snap in sections.values():
        drops.extend(snap.get("drops") or [])
        general_drops.extend(snap.get("general_drops") or [])
        premium_drops.extend(snap.get("premium_drops") or [])
        rapid_drops.extend(snap.get("rapid_drops") or [])
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
        for key in catalogue_stats:
            catalogue_stats[key] += int((snap.get('catalogue_stats') or {}).get(key) or 0)
        st = snap.get("cex_stats") or {}
        for key in stats:
            stats[key] += int(st.get(key) or 0)

    # Preserve when a deal was first detected, not just when the section reran.
    for collection in (drops, general_drops, cex_flips, cex_arbitrage):
        for row in collection:
            if not row.get("found_at"):
                row["found_at"] = incoming.get("generated_at")
    drops = sorted(_dedupe_by_asin(drops), key=lambda r: r.get("drop_percent") or 0, reverse=True)
    general_drops = sorted(
        _latest_scanned_by_asin(general_drops),
        key=lambda r: (r.get("drop_percent") or 0, r.get("savings_eur") or 0),
        reverse=True,
    )
    premium_drops = sorted(_dedupe_by_asin(premium_drops), key=lambda r: r.get("drop_percent") or 0, reverse=True)
    rapid_drops = sorted(_dedupe_by_asin(rapid_drops), key=lambda r: r.get("drop_percent") or 0, reverse=True)
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
        "alert_policy": incoming.get("alert_policy") or base.get("alert_policy") or asdict(load_alert_policy()),
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
        "general_drops": general_drops,
        "general_drop_percent": incoming.get("general_drop_percent", base.get("general_drop_percent", 10)),
        "premium_drops": premium_drops[:30],
        "rapid_drops": rapid_drops[:30],
        "premium_min_price_eur": incoming.get("premium_min_price_eur", base.get("premium_min_price_eur", 100)),
        "premium_history_drop_percent": incoming.get(
            "premium_history_drop_percent", base.get("premium_history_drop_percent", 45)
        ),
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
        "seller_verification_enabled": False,
        "catalogue_stats": catalogue_stats,
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
        "alert_policy": asdict(load_alert_policy()),
        "min_discount_percent": settings.min_discount_percent,
        "blocked": result.blocked,
        "requests": result.requests,
        "errors": result.errors,
        "categories_crawled": len(result.categories),
        "items_seen": len(result.items),
        "items_priced": processed.priced,
        "items_with_history": processed.with_history,
        "new_alerts": len(processed.alerts),
        "seller_verification_enabled": False,
        "verification_stats": {"status": "disabled"},
        "catalogue_stats": processed.catalogue_stats,
        "scan_status": "blocked" if result.blocked else ("partial" if result.errors else "completed"),
        "drops": [{**row, "found_at": datetime.now(timezone.utc).isoformat()} for row in processed.drops[:max_drops]],
        "general_drop_percent": config.general_drop_percent if config else 10.0,
        "general_drops": [
            {
                **row,
                "found_at": datetime.now(timezone.utc).isoformat(),
                "last_scanned_at": datetime.now(timezone.utc).isoformat(),
            }
            for row in processed.general_drops
        ],
        "premium_drops": [
            {**row, "found_at": datetime.now(timezone.utc).isoformat()} for row in processed.premium_drops[:30]
        ],
        "rapid_drops": [
            {**row, "found_at": datetime.now(timezone.utc).isoformat()} for row in processed.rapid_drops[:30]
        ],
        "premium_min_price_eur": config.premium_min_price_eur if config else 100.0,
        "premium_history_drop_percent": config.premium_history_drop_percent if config else 45.0,
        "alerts": db.recent_alerts(limit=50),
        "deep_drop_tiers": [int(t) for t in tiers],
        "deep_drops": tiered_deep_drops(processed.drops, tiers),
        "cex_enabled": bool(config.cex_enabled if config else True),
        "cex_min_cash_profit_eur": config.cex_min_cash_profit_eur if config else 5.0,
        "cex_min_cash_profit_percent": config.cex_min_cash_profit_percent if config else 10.0,
        "cex_lookups": processed.cex_lookups[:30],
        "cex_flips": [{**row, "found_at": row.get("observed_at") or datetime.now(timezone.utc).isoformat()} for row in processed.cex_flips[:20]],
        "cex_arbitrage": [{**row, "found_at": row.get("observed_at") or datetime.now(timezone.utc).isoformat()} for row in processed.cex_arbitrage[:30]],
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
        "- **Research leads only:** seller, listing condition and final checkout price are not verified.",
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
