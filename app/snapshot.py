from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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


def write_snapshot(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def read_snapshot(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
