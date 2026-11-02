from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

DUBLIN_TZ = ZoneInfo("Europe/Dublin")

from app.config import settings
from app import database as db
from app.detector.price_error import evaluate_price


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def import_snapshot(data: dict[str, Any]) -> None:
    """Hydrate SQLite from a previously published Pages snapshot."""
    db.init_db()
    asin_to_id: dict[str, int] = {}
    for product in data.get("products") or []:
        asin = (product.get("asin") or "").upper()
        url = product.get("url")
        if not asin or not url:
            continue
        pid = db.add_product(asin, url, product.get("title"))
        asin_to_id[asin] = pid
        history = list(reversed(product.get("history") or []))
        for check in history:
            db.record_price_check(
                pid,
                check.get("price_eur"),
                check.get("list_price_eur"),
                check.get("currency"),
                raw_note=check.get("raw_note"),
                checked_at=check.get("checked_at"),
            )

    for alert in reversed(data.get("alerts") or []):
        asin = (alert.get("asin") or "").upper()
        url = alert.get("url")
        if not asin or not url:
            continue
        pid = asin_to_id.get(asin) or db.add_product(asin, url, alert.get("product_title"))
        asin_to_id[asin] = pid
        alert_id = db.create_alert(
            product_id=pid,
            price_eur=float(alert["price_eur"]),
            list_price_eur=alert.get("list_price_eur"),
            discount_percent=alert.get("discount_percent"),
            reason=alert.get("reason") or "Imported alert",
            created_at=alert.get("created_at"),
        )
        db.mark_alert_notified(
            alert_id,
            telegram=bool(alert.get("notified_telegram")),
            whatsapp=bool(alert.get("notified_whatsapp")),
        )


def export_snapshot(*, github_repo: str | None = None) -> dict[str, Any]:
    db.init_db()
    products_out: list[dict[str, Any]] = []
    for product in db.list_products():
        pid = int(product["id"])
        latest = db.latest_price_check(pid)
        history = db.price_history(pid, limit=30)
        price = latest.get("price_eur") if latest else None
        list_price = latest.get("list_price_eur") if latest else None
        verdict = evaluate_price(
            product_id=pid,
            price_eur=price,
            list_price_eur=list_price,
        )
        status = "ok"
        note = None
        if latest and latest.get("raw_note") and price is None:
            status = "fetch_error"
            note = latest.get("raw_note")
        elif verdict.is_error:
            status = "price_error"
            note = verdict.reason
        products_out.append(
            {
                "asin": product["asin"],
                "url": product["url"],
                "title": product.get("title"),
                "enabled": bool(product.get("enabled")),
                "price_eur": price,
                "list_price_eur": list_price,
                "discount_percent": verdict.discount_percent if verdict.is_error else None,
                "status": status,
                "note": note,
                "checked_at": latest.get("checked_at") if latest else None,
                "history": history,
            }
        )

    repo = github_repo or ""
    pages_base = f"https://{repo.split('/')[0]}.github.io/{repo.split('/')[-1]}/" if repo else ""
    return {
        "generated_at": _utc_now(),
        "telegram_configured": settings.telegram_enabled,
        "whatsapp_configured": settings.whatsapp_enabled,
        "min_discount_percent": settings.min_discount_percent,
        "github_repo": repo,
        "pages_url": pages_base,
        "new_watch_issue_url": (
            f"https://github.com/{repo}/issues/new?template=watch.yml"
            if repo
            else ""
        ),
        "products": products_out,
        "alerts": db.recent_alerts(limit=50),
    }


def _fmt_dublin(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(DUBLIN_TZ).strftime("%d %b %Y, %H:%M %Z")
    except ValueError:
        return iso


def _fmt_euro(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"€{float(value):.2f}"
    except (TypeError, ValueError):
        return "—"


def _md_status(status: str | None) -> str:
    if status == "price_error":
        return "🚨 price error"
    if status == "fetch_error":
        return "⚠️ fetch failed"
    return "watching"


def render_tracker_markdown(payload: dict[str, Any]) -> str:
    repo = payload.get("github_repo") or "paddykeoghgoode/amazon-ie-price-error-tracker"
    issue_url = payload.get("new_watch_issue_url") or f"https://github.com/{repo}/issues/new?template=watch.yml"
    generated = _fmt_dublin(payload.get("generated_at"))
    telegram = "ready" if payload.get("telegram_configured") else "not configured (add Actions secrets)"
    lines = [
        "# Amazon.ie price error tracker",
        "",
        f"Last scan: **{generated}** · Telegram: **{telegram}** · Flag at ≥{payload.get('min_discount_percent', 75)}% off",
        "",
        f"Add a product: [open a Watch issue]({issue_url}) or edit [`watchlist.json`](watchlist.json).",
        "",
        "## Watched products",
        "",
    ]
    products = payload.get("products") or []
    if not products:
        lines.append("_Nothing on the watch list yet._")
    else:
        lines.extend(
            [
                "| Product | Now | Was | Status | Checked |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for product in products:
            title = product.get("title") or product.get("asin") or "Untitled"
            url = product.get("url") or "#"
            note = f"<br>{product['note']}" if product.get("note") else ""
            lines.append(
                "| "
                + " | ".join(
                    [
                        f"[{title}]({url})<br>`{product.get('asin', '')}`",
                        _fmt_euro(product.get("price_eur")),
                        _fmt_euro(product.get("list_price_eur")),
                        _md_status(product.get("status")) + note,
                        _fmt_dublin(product.get("checked_at")),
                    ]
                )
                + " |"
            )
    lines.extend(["", "## Price-error alerts", ""])
    alerts = payload.get("alerts") or []
    if not alerts:
        lines.append("_No suspected errors yet._")
    else:
        for alert in alerts:
            title = alert.get("product_title") or alert.get("asin") or "Alert"
            url = alert.get("url") or "#"
            telegram_state = "Telegram sent" if alert.get("notified_telegram") else "Telegram pending"
            was = (
                f" (was {_fmt_euro(alert.get('list_price_eur'))})"
                if alert.get("list_price_eur") is not None
                else ""
            )
            lines.append(
                f"- [{title}]({url}) — **{_fmt_euro(alert.get('price_eur'))}**{was}"
                f" — {alert.get('reason') or ''} — {_fmt_dublin(alert.get('created_at'))} · {telegram_state}"
            )
    lines.extend(
        [
            "",
            "---",
            "",
            "This file is rewritten by GitHub Actions after each scan. "
            "Times are Ireland (Europe/Dublin).",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def write_snapshot(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_snapshot(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
