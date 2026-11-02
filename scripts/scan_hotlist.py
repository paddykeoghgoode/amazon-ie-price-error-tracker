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
from app.snapshot import read_snapshot, write_snapshot

def candidates(deals, now, limit=12):
    rows = {}
    for row in (deals.get("drops") or []) + (deals.get("cex_flips") or []):
        if float(row.get("drop_percent") or 0) < 30:
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
            try:
                snap = await fetch_product(client, asin)
                if snap.price_eur is not None and snap.price_eur > 0:
                    item.update(status="price_read", price_eur=snap.price_eur)
                    earlier = prior.get(asin) or {}
                    threshold = min(float(earlier.get("price_eur") or float("inf")),
                                    float(row.get("price_eur") or float("inf")))
                    if notify and snap.price_eur < threshold - 0.01:
                        await send_notification(
                            f"🚨 Observed further Amazon price drop (UNVERIFIED OFFER)\n"
                            f"{snap.title or asin}\n"
                            f"Page price €{snap.price_eur:.2f}, previously €{threshold:.2f}\n"
                            "⚠️ Seller, condition, variant and checkout price NOT checked. Research only.\n"
                            f"Checked {item['checked_at']} UTC\n{row.get('url')}")
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
