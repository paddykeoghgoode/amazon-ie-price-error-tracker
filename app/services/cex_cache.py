"""Reusable CeX trade-in quotes, kept separately from Amazon offer verification.

A CeX cash quote depends on the CeX box (SKU), not on Amazon's current price.
Keep quoted cash values when Amazon prices change, but never reuse seller
verification or the *profit calculation* from an older Amazon price.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from math import isfinite
from typing import Any

from app.scraper.cex_ie import CexPrices


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return dt.astimezone(timezone.utc) if dt.tzinfo is not None else None


def age_hours(record: dict | None, now: datetime) -> float | None:
    if not isinstance(record, dict):
        return None
    checked = _timestamp(record.get("checked_at"))
    if checked is None:
        return None
    hours = (now.astimezone(timezone.utc) - checked).total_seconds() / 3600
    return hours if hours >= 0 else None


def cache_valid(
    record: dict | None, *,
    title: str | None,
    now: datetime,
    quote_ttl_hours: float = 72,
    no_match_ttl_hours: float = 168,
    error_retry_hours: float = 6,
) -> bool:
    if not isinstance(record, dict) or record.get("title") != title:
        return False
    age = age_hours(record, now)
    if age is None:
        return False
    ttl = {
        "matched": quote_ttl_hours,
        "no_match": no_match_ttl_hours,
        "error": error_retry_hours,
    }.get(record.get("status"))
    return ttl is not None and age <= ttl


def cache_price(record: dict | None) -> CexPrices | None:
    if not isinstance(record, dict) or record.get("status") != "matched":
        return None
    data = record.get("quote")
    if not isinstance(data, dict):
        return None
    try:
        prices = CexPrices(
            box_id=str(data["box_id"]),
            box_name=str(data["box_name"]),
            category_name=data.get("category_name"),
            cash_eur=float(data["cash_eur"]),
            voucher_eur=float(data["voucher_eur"]),
            retail_eur=float(data["retail_eur"]),
            url=str(data["url"]),
            barcodes=tuple(data.get("barcodes") or []),
            model_number=data.get("model_number"),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not isfinite(prices.cash_eur) or prices.cash_eur <= 0:
        return None
    return prices


def quote_entry(title: str | None, prices: CexPrices, now: datetime) -> dict:
    return {
        "title": title,
        "status": "matched",
        "checked_at": now.astimezone(timezone.utc).isoformat(),
        "quote": asdict(prices),
    }


def status_entry(title: str | None, status: str, now: datetime) -> dict:
    if status not in {"no_match", "error"}:
        raise ValueError("Invalid cached CeX status")
    return {
        "title": title,
        "status": status,
        "checked_at": now.astimezone(timezone.utc).isoformat(),
    }


def retry_allowed(record: dict | None, now: datetime, error_retry_hours: float) -> bool:
    if not isinstance(record, dict):
        return True
    stamped = _timestamp(record.get("last_error_at"))
    if stamped is None:
        return True
    return (now.astimezone(timezone.utc) - stamped).total_seconds() >= error_retry_hours * 3600


def mark_error(record: dict | None, now: datetime) -> None:
    if isinstance(record, dict):
        record["last_error_at"] = now.astimezone(timezone.utc).isoformat()
