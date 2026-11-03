"""Fast targeted price-only follow-up for recent observed Amazon drops.

No seller verification occurs. The product detail price might also be a
different variant; all messages require manual checkout confirmation.
"""
import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import httpx
from app.scraper.amazon_ie import fetch_product
from app.services.monitor import BROWSER_HEADERS, send_notification
from app.services.alert_policy import (
    hotlist_further_drop_qualifies, load_alert_policy, qualifies_observed_drop,
)
from app.snapshot import read_snapshot, write_snapshot

def candidates(deals, now, limit=12):
    rows = {}
    policy = load_alert_policy()
    for row in (deals.get("drops") or []) + (deals.get("cex_flips") or []):
        if not qualifies_observed_drop(row, policy):
            continue
        try:
            stamp = datetime.fromisoformat(row.get("found_at") or row.get("observed_at") or "")
            if stamp.tzinfo is None or not timedelta(0) <= now - stamp <= timedelta(hours=48):
                continue
        except (ValueError, TypeError):
            continue
        rows.setdefault(row.get("asin"), row)
    return sorted(rows.values(), key=lambda r: -float(r.get("drop_percent") or 0))[:limit]

async def recheck(deals, previous, limit=12, notify=True):
    now = datetime.now(timezone.utc)
    prior = {r.get("asin"): r for r in (previous.get("products") or [])}
    out = []
    async with httpx.AsyncClient(headers=BROWSER_HEADERS, timeout=25.0) as client:
        for row in candidates(deals, now, limit=limit):
            asin = row["asin"]
            item = {"asin": asin, "title": row.get("title"), "url": row.get("url"),
                    "status": "price_not_read", "found_at": row.get("found_at"),
                    "checked_at": datetime.now(timezone.utc).isoformat()}
            earlier = prior.get(asin) or {}
            try:
                snap = await fetch_product(client, asin)
                if snap.price_eur is not None and snap.price_eur > 0:
                    item.update(status="price_read", price_eur=snap.price_eur)
                    threshold = min(float(earlier.get("price_eur") or float("inf")),
                                    float(row.get("price_eur") or float("inf")))
                    last_notified = earlier.get("last_alert_at")
                    last_price = float(earlier.get("last_alert_price_eur") or 0)
                    recently_sent = False
                    if last_notified:
                        try:
                            sent_at = datetime.fromisoformat(last_notified)
                            recently_sent = (
                                sent_at.tzinfo is not None and
                                timedelta(0) <= now - sent_at < timedelta(hours=24)
                            )
                        except (TypeError, ValueError):
                            pass
                    meaningful_new_low = (
                        not recently_sent or
                        (last_price > 0 and
                         last_price - snap.price_eur >= load_alert_policy().min_hotlist_extra_drop_eur and
                         snap.price_eur <= last_price * (1 - load_alert_policy().min_hotlist_extra_drop_percent / 100))
                    )
                    if notify and meaningful_new_low and hotlist_further_drop_qualifies(
                        row, previous_eur=threshold, current_eur=snap.price_eur
                    ):
                        message = (
                            "🚨 Large high-ticket Amazon reduction (RESEARCH ONLY)\n"
                            f"{snap.title or asin}\n"
                            f"Now €{snap.price_eur:.2f}, last observed €{threshold:.2f}\n"
                            f"Extra reduction €{threshold - snap.price_eur:.2f}\n"
                            "⚠️ Seller, condition, variant and delivered checkout are not verified.\n"
                            f"{row.get('url')}"
                        )
                        ok, sms_ok = await send_notification(message)
                        if ok or sms_ok:
                            item["last_alert_price_eur"] = snap.price_eur
                            item["last_alert_at"] = item["checked_at"]
                    elif earlier.get("last_alert_at"):
                        item["last_alert_price_eur"] = earlier.get("last_alert_price_eur")
                        item["last_alert_at"] = earlier["last_alert_at"]
            except (httpx.HTTPError, ValueError, TypeError):
                pass
            out.append(item)
            await asyncio.sleep(1)
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "scan_kind": "hotlist",
            "products": out, "price_read": sum(row["status"] == "price_read" for row in out)}

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--deals", type=Path, required=True)
    p.add_argument("--previous", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--limit", type=int, default=12)
    p.add_argument("--no-notify", action="store_true")
    a = p.parse_args()
    result = asyncio.run(recheck(read_snapshot(a.deals) if a.deals.exists() else {},
                                read_snapshot(a.previous) if a.previous.exists() else {},
                                limit=a.limit, notify=not a.no_notify))
    write_snapshot(a.output, result)
    print(json.dumps({"checked": len(result["products"]), "price_read": result["price_read"]}))
if __name__ == "__main__":
    main()
