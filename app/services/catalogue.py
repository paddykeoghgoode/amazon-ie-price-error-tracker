"""Persistent, public Amazon listing inventory with a fair incremental-check queue.

All priced ASINs discovered in a section are included, even when they have no
CeX keyword or do not meet a price threshold. One bounded batch is checked per
run, prioritising a handful of fresh drops; oldest/unexamined items otherwise
go first. Status is recovered from the published catalogue across Actions jobs.
"""
from __future__ import annotations

import csv
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.services.bestsellers import CrawlResult

CSV_COLUMNS = (
    "section", "asin", "title", "url", "category", "rank", "price_eur", "active",
    "first_seen_at", "last_seen_at", "verification_status", "last_checked_at",
    "seller_name", "seller_type", "cex_status", "cex_box_name", "cex_url",
    "cex_cash_eur", "cex_net_profit_eur", "cex_max_buy_eur",
    "match_confidence", "cex_checked_at", "cex_quote_checked_at",
    "cex_quote_source", "cex_quote_fresh_for_alert",
)
_CEX_FIELDS = (
    "cex_box_name", "cex_url", "cex_cash_eur", "cex_net_profit_eur",
    "cex_max_buy_eur", "match_confidence", "cex_checked_at",
    "cex_quote_checked_at", "cex_quote_source", "cex_quote_fresh_for_alert",
)
_SELLER_FIELDS = ("seller_name", "seller_type", "fulfilment_status")
_PRICE_EPS = 0.005


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_section_catalogue(
    result: CrawlResult,
    previous_section: dict | None,
    *,
    section_id: str,
    section_name: str,
    observed_at: str | None = None,
) -> dict:
    """Refresh listing prices while preserving checks only for the *same* price.

    Never promote an old seller/offer verification after an ASIN price changes.
    Missing-price listings are still exported, with an explicit status.
    """
    stamp = observed_at or _utc_now()
    old = {
        row.get("asin"): row for row in ((previous_section or {}).get("items") or [])
        if isinstance(row, dict) and row.get("asin")
    }
    items = []
    for asin, (item, category) in result.items.items():
        price = round(float(item.price_eur), 2) if item.price_eur is not None else None
        prior = old.get(asin) or {}
        same_price = (
            price is not None
            and prior.get("price_eur") is not None
            and abs(float(prior["price_eur"]) - price) < _PRICE_EPS
            and (prior.get("title") or "") == (item.title or "")
        )
        row: dict[str, Any] = {
            "asin": asin,
            "title": item.title,
            "url": item.url,
            "category": category,
            "rank": item.rank,
            "price_eur": price,
            "active": True,
            "first_seen_at": prior.get("first_seen_at") or stamp,
            "last_seen_at": stamp,
            "verification_status": "pending" if price is not None else "no_price",
            "last_checked_at": None,
            "cex_status": "not_checked" if price is not None else "not_applicable",
        }
        # CeX box cash prices are independent of the current Amazon price.
        # Only preserve this quote when the product identity (title) is unchanged.
        if (prior.get("title") or "") == (item.title or ""):
            if isinstance(prior.get("cex_quote_cache"), dict):
                row["cex_quote_cache"] = prior["cex_quote_cache"]
        if same_price:
            for key in (
                "verification_status", "last_checked_at", "cex_status",
                "last_cex_alert", *_SELLER_FIELDS, *_CEX_FIELDS,
            ):
                if key in prior:
                    row[key] = prior[key]
        items.append(row)
    # Preserve recently discovered listings that fall out of the bestseller
    # chart, but do not spend Amazon/CeX requests on stale, unobserved prices.
    current_asins = {row["asin"] for row in items}
    for asin, prior in old.items():
        if asin in current_asins:
            continue
        try:
            last_seen = datetime.fromisoformat(prior.get("last_seen_at") or "")
            seen_now = datetime.fromisoformat(stamp)
            fresh_enough = (
                last_seen.tzinfo is not None and seen_now.tzinfo is not None
                and timedelta(0) <= seen_now - last_seen <= timedelta(days=30)
            )
        except (TypeError, ValueError):
            fresh_enough = False
        if fresh_enough:
            items.append({**prior, "active": False})
    return {
        "section_id": section_id,
        "section_name": section_name,
        "generated_at": stamp,
        "items": items,
    }


def select_check_batch(
    section: dict, *, limit: int, priority_asins: list[str] | None = None,
    urgent_limit: int = 20,
) -> list[dict]:
    """Rotate through *every* priced listing without a fixed shortlist cut-off.

    Unchecked rows go before previously checked ones. For all other rows, the
    oldest verification is refreshed first. The urgent lane never consumes
    more than a small part of the batch, preventing high-ranked products from
    starving the rest of the catalogue.
    """
    if limit <= 0:
        return []
    items = [
        row for row in section.get("items") or []
        if row.get("price_eur") is not None and row.get("asin") and row.get("active", True)
    ]
    oldest = sorted(items, key=lambda r: (bool(r.get("last_checked_at")), r.get("last_checked_at") or ""))
    by_asin = {row["asin"]: row for row in oldest}
    urgent = []
    for asin in priority_asins or []:
        row = by_asin.get(asin)
        if row and row not in urgent and len(urgent) < min(urgent_limit, limit):
            urgent.append(row)
    selected = list(urgent)
    seen = {row["asin"] for row in selected}
    for row in oldest:
        if len(selected) >= limit:
            break
        if row["asin"] not in seen:
            selected.append(row)
            seen.add(row["asin"])
    return selected


def apply_batch_results(
    section: dict,
    selected: list[dict],
    verified: list[dict],
    outcomes: dict[str, str],
    cex_lookups: list[dict],
    *,
    cex_enabled: bool,
    checked_at: str | None = None,
    quote_cache: dict[str, dict] | None = None,
) -> dict:
    """Keep CeX research for ALL priced ASINs; distinguish unsafe leads.

    A title-matched CeX quote for a seller-unverified Amazon listing is never
    an actionable buy alert. It is still useful as a provisional research lead.
    """
    stamp = checked_at or _utc_now()
    rows = {row["asin"]: row for row in section.get("items") or []}
    eligible = {row["asin"]: row for row in verified}
    matches = {row["asin"]: row for row in cex_lookups}
    for item in selected:
        asin = item["asin"]
        row = rows[asin]
        row["last_checked_at"] = stamp
        cache_entry = (quote_cache or {}).get(asin) if quote_cache is not None else row.get("cex_quote_cache")
        if isinstance(cache_entry, dict):
            row["cex_quote_cache"] = cache_entry
        # Timestamp reflects the *CeX API quote*, not the Amazon seller check.
        row["cex_checked_at"] = cache_entry.get("checked_at") if (
            cex_enabled and isinstance(cache_entry, dict) and cache_entry.get("status") != "error"
        ) else None
        verified_item = eligible.get(asin)
        if verified_item:
            row["verification_status"] = "verified"
            row["seller_name"] = verified_item.get("seller_name")
            row["seller_type"] = verified_item.get("seller_type")
            row["fulfilment_status"] = verified_item.get("fulfilment_status")
        else:
            row["verification_status"] = outcomes.get(asin, "not_verified")
            for key in _SELLER_FIELDS:
                row.pop(key, None)

        matched = matches.get(asin)
        if cex_enabled and matched:
            if not verified_item:
                row["cex_status"] = "matched_unverified"
            elif not matched.get("cex_quote_fresh_for_alert", True):
                row["cex_status"] = "matched_stale_quote"
            else:
                row["cex_status"] = "matched"
            row["cex_box_name"] = matched.get("cex_title")
            row["cex_url"] = matched.get("cex_url")
            row["cex_cash_eur"] = matched.get("cex_cash_eur")
            row["cex_net_profit_eur"] = matched.get("cex_cash_profit_eur")
            row["cex_max_buy_eur"] = matched.get("cex_max_buy_eur")
            row["match_confidence"] = matched.get("match_confidence")
            row["cex_quote_checked_at"] = matched.get("cex_quote_checked_at")
            row["cex_quote_source"] = matched.get("cex_quote_source")
            row["cex_quote_fresh_for_alert"] = matched.get("cex_quote_fresh_for_alert")
        else:
            status = (cache_entry or {}).get("status")
            row["cex_status"] = (
                "disabled" if not cex_enabled else
                "no_confirmed_match" if status == "no_match" else
                "provider_retry_later" if status == "error" else
                "queued"
            )
            for key in _CEX_FIELDS:
                if key != "cex_checked_at":
                    row.pop(key, None)
    return section


def catalogue_progress(section: dict, *, checked_this_run: int = 0) -> dict:
    items = section.get("items") or []
    active = [row for row in items if row.get("active", True)]
    priced = [row for row in active if row.get("price_eur") is not None]
    checked = [row for row in priced if row.get("last_checked_at")]
    return {
        "discovered": len(items),
        "active": len(active),
        "archived": len(items) - len(active),
        "priced": len(priced),
        "checked": len(checked),
        "remaining": len(priced) - len(checked),
        "verified": sum(row.get("verification_status") == "verified" for row in checked),
        "cex_attempted": sum(bool(row.get("cex_checked_at")) for row in checked),
        "cex_matched": sum(row.get("cex_status") in ("matched", "matched_unverified", "matched_stale_quote") for row in checked),
        "cex_eligible_matched": sum(row.get("cex_status") == "matched" for row in checked),
        "cex_cache_records": sum(isinstance(row.get("cex_quote_cache"), dict) for row in checked),
        "unpriced": len(active) - len(priced),
        "checked_this_run": checked_this_run,
    }


def merge_catalogue(previous: dict | None, fragment: dict) -> dict:
    """Merge just this section, never replacing another completed section."""
    sid = fragment.get("section_id")
    if not sid or not isinstance(fragment.get("items"), list):
        raise ValueError("Expected one section fragment with an items list")
    sections = dict((previous or {}).get("sections") or {})
    older = sections.get(sid) or {}
    if older.get("generated_at", "") > fragment.get("generated_at", ""):
        return previous or {}
    stats = catalogue_progress(fragment)
    sections[sid] = {
        **fragment,
        "stats": {
            **stats,
            "checked_this_run": fragment.get("checked_this_run", stats["checked_this_run"]),
        },
    }
    return {
        "generated_at": fragment.get("generated_at"),
        "last_section_id": sid,
        "sections": sections,
    }


def _safe_csv_cell(value: object) -> object:
    """Prevent spreadsheet-formula execution from untrusted Amazon titles."""
    if value is None:
        return ""
    if isinstance(value, str) and re.match(r"^\s*[=+@\-\t\r]", value):
        return "'" + value
    return value


def write_catalogue_csv(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for section in (payload.get("sections") or {}).values():
            for item in section.get("items") or []:
                data = {**item, "section": section.get("section_name") or section.get("section_id")}
                writer.writerow({k: _safe_csv_cell(data.get(k)) for k in CSV_COLUMNS})
