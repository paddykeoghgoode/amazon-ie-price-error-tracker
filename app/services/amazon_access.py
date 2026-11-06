"""Shared Amazon fetch backoff for lightweight watchlist scans."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import database as db

WATCHLIST_AMAZON_BLOCKED_META = "watchlist_amazon_blocked_until"
WATCHLIST_AMAZON_COOLDOWN_MINUTES = 30


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def watchlist_amazon_blocked_until(*, now: datetime | None = None) -> datetime | None:
    return _parse_iso(db.get_meta(WATCHLIST_AMAZON_BLOCKED_META))


def watchlist_amazon_cooling_down(*, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    until = watchlist_amazon_blocked_until(now=now)
    return until is not None and now < until


def start_watchlist_amazon_cooldown(*, now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    until = now + timedelta(minutes=WATCHLIST_AMAZON_COOLDOWN_MINUTES)
    iso = until.isoformat()
    db.set_meta(WATCHLIST_AMAZON_BLOCKED_META, iso)
    return iso


def clear_watchlist_amazon_cooldown() -> None:
    db.clear_meta(WATCHLIST_AMAZON_BLOCKED_META)
