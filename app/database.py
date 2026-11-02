import math
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_db() -> None:
    path = settings.database_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                asin TEXT NOT NULL UNIQUE,
                url TEXT NOT NULL,
                title TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS price_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                price_eur REAL,
                list_price_eur REAL,
                currency TEXT,
                checked_at TEXT NOT NULL,
                raw_note TEXT,
                FOREIGN KEY (product_id) REFERENCES products(id)
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                price_eur REAL NOT NULL,
                list_price_eur REAL,
                discount_percent REAL,
                reason TEXT NOT NULL,
                notified_telegram INTEGER NOT NULL DEFAULT 0,
                notified_whatsapp INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (product_id) REFERENCES products(id)
            );

            CREATE INDEX IF NOT EXISTS idx_price_checks_product
                ON price_checks(product_id, checked_at DESC);
            CREATE INDEX IF NOT EXISTS idx_alerts_product
                ON alerts(product_id, created_at DESC);

            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            -- A compact, stable reference that survives rolling 20-price pruning.
            CREATE TABLE IF NOT EXISTS price_baselines (
                product_id INTEGER PRIMARY KEY,
                reference_eur REAL NOT NULL,
                confirmed_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                FOREIGN KEY (product_id) REFERENCES products(id)
            );
            """
        )


def get_meta(key: str) -> str | None:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_meta(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def alerts_since(since_iso: str) -> int:
    with _connect() as conn:
        row = conn.execute("SELECT COUNT(*) FROM alerts WHERE created_at >= ?", (since_iso,)).fetchone()
        return int(row[0])


@contextmanager
def _connect():
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def add_product(asin: str, url: str, title: str | None = None) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO products (asin, url, title, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(asin) DO UPDATE SET
                url = excluded.url,
                title = COALESCE(excluded.title, products.title)
            RETURNING id
            """,
            (asin, url, title, _utc_now()),
        )
        return int(cur.fetchone()[0])


def list_products(enabled_only: bool = False) -> list[dict]:
    with _connect() as conn:
        q = "SELECT * FROM products"
        if enabled_only:
            q += " WHERE enabled = 1"
        q += " ORDER BY id DESC"
        return [dict(r) for r in conn.execute(q).fetchall()]


def set_product_enabled(product_id: int, enabled: bool) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE products SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, product_id),
        )


def delete_product(product_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM price_checks WHERE product_id = ?", (product_id,))
        conn.execute("DELETE FROM alerts WHERE product_id = ?", (product_id,))
        conn.execute("DELETE FROM products WHERE id = ?", (product_id,))


def record_price_check(
    product_id: int,
    price_eur: float | None,
    list_price_eur: float | None,
    currency: str | None,
    raw_note: str | None = None,
    checked_at: str | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO price_checks
                (product_id, price_eur, list_price_eur, currency, checked_at, raw_note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (product_id, price_eur, list_price_eur, currency, checked_at or _utc_now(), raw_note),
        )


def update_product_title(product_id: int, title: str) -> None:
    with _connect() as conn:
        conn.execute("UPDATE products SET title = ? WHERE id = ?", (title, product_id))


def create_alert(
    product_id: int,
    price_eur: float,
    list_price_eur: float | None,
    discount_percent: float | None,
    reason: str,
    created_at: str | None = None,
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO alerts
                (product_id, price_eur, list_price_eur, discount_percent, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (
                product_id,
                price_eur,
                list_price_eur,
                discount_percent,
                reason,
                created_at or _utc_now(),
            ),
        )
        return int(cur.fetchone()[0])


def mark_alert_notified(alert_id: int, *, telegram: bool = False, whatsapp: bool = False) -> None:
    with _connect() as conn:
        if telegram:
            conn.execute(
                "UPDATE alerts SET notified_telegram = 1 WHERE id = ?",
                (alert_id,),
            )
        if whatsapp:
            conn.execute(
                "UPDATE alerts SET notified_whatsapp = 1 WHERE id = ?",
                (alert_id,),
            )


def recent_alerts(limit: int = 50) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT a.*, p.asin, p.url, p.title AS product_title
            FROM alerts a
            JOIN products p ON p.id = a.product_id
            ORDER BY a.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def last_alert_for_product(product_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM alerts
            WHERE product_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (product_id,),
        ).fetchone()
        return dict(row) if row else None


def latest_price_check(product_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM price_checks
            WHERE product_id = ?
            ORDER BY checked_at DESC
            LIMIT 1
            """,
            (product_id,),
        ).fetchone()
        return dict(row) if row else None


def price_history(product_id: int, limit: int = 30) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT price_eur, list_price_eur, currency, checked_at, raw_note
            FROM price_checks
            WHERE product_id = ?
            ORDER BY checked_at DESC
            LIMIT ?
            """,
            (product_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def prune_price_checks(keep_per_product: int = 20) -> None:
    with _connect() as conn:
        conn.execute(
            """
            DELETE FROM price_checks WHERE id IN (
                SELECT id FROM (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY product_id ORDER BY checked_at DESC, id DESC
                    ) AS rn
                    FROM price_checks
                ) WHERE rn > ?
            )
            """,
            (keep_per_product,),
        )


def median_historical_price(product_id: int, limit: int = 20) -> float | None:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT price_eur FROM price_checks
            WHERE product_id = ? AND price_eur IS NOT NULL
            ORDER BY checked_at DESC
            LIMIT ?
            """,
            (product_id, limit),
        ).fetchall()
    prices = [float(r["price_eur"]) for r in rows if r["price_eur"] is not None]
    if len(prices) < 3:
        return None
    prices.sort()
    mid = len(prices) // 2
    if len(prices) % 2:
        return prices[mid]
    return (prices[mid - 1] + prices[mid]) / 2


def get_price_baseline(product_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT reference_eur, confirmed_at, last_seen_at "
            "FROM price_baselines WHERE product_id = ?",
            (product_id,),
        ).fetchone()
        return dict(row) if row else None


def save_price_baseline(
    product_id: int, reference_eur: float, *, confirmed_at: str, last_seen_at: str
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO price_baselines (product_id, reference_eur, confirmed_at, last_seen_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(product_id) DO UPDATE SET
                reference_eur = excluded.reference_eur,
                confirmed_at = excluded.confirmed_at,
                last_seen_at = excluded.last_seen_at
            """,
            (product_id, reference_eur, confirmed_at, last_seen_at),
        )


def delete_price_baseline(product_id: int) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM price_baselines WHERE product_id = ?", (product_id,))


def export_price_baselines(asins: set[str]) -> dict[str, list]:
    """Compact Pages checkpoint: ASIN -> [reference EUR, last confirmed, last seen]."""
    if not asins:
        return {}
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT p.asin, b.reference_eur, b.confirmed_at, b.last_seen_at
            FROM price_baselines b JOIN products p ON p.id = b.product_id
            """
        ).fetchall()
    return {
        row["asin"]: [round(float(row["reference_eur"]), 2), row["confirmed_at"], row["last_seen_at"]]
        for row in rows if row["asin"] in asins
    }


def import_price_baselines(checkpoint: dict) -> int:
    """Recover anchors when the Actions cache is absent; prefer newer published state.

    Only public numeric price references are imported. No credentials or raw HTML.
    """
    if not isinstance(checkpoint, dict):
        return 0
    valid: list[tuple[str, float, str, str]] = []
    for asin, row in checkpoint.items():
        if not isinstance(asin, str) or not re.fullmatch(r"[A-Z0-9]{10}", asin):
            continue
        if not isinstance(row, list) or len(row) != 3:
            continue
        try:
            price = float(row[0])
            confirmed = datetime.fromisoformat(row[1])
            seen = datetime.fromisoformat(row[2])
            if (
                not math.isfinite(price) or price <= 0 or
                confirmed.tzinfo is None or seen.tzinfo is None or
                confirmed > seen or seen > datetime.now(timezone.utc)
            ):
                continue
        except (ValueError, TypeError, OverflowError):
            continue
        valid.append((asin, price, str(row[1]), str(row[2])))
    if not valid:
        return 0
    with _connect() as conn:
        conn.executemany(
            """
            INSERT INTO products (asin, url, created_at)
            VALUES (?, ?, ?)
            ON CONFLICT(asin) DO NOTHING
            """,
            [(asin, f"https://www.amazon.ie/dp/{asin}", _utc_now()) for asin, *_ in valid],
        )
        conn.executemany(
            """
            INSERT INTO price_baselines (product_id, reference_eur, confirmed_at, last_seen_at)
            SELECT id, ?, ?, ? FROM products WHERE asin = ?
            ON CONFLICT(product_id) DO UPDATE SET
                reference_eur = excluded.reference_eur,
                confirmed_at = excluded.confirmed_at,
                last_seen_at = excluded.last_seen_at
            WHERE excluded.last_seen_at > price_baselines.last_seen_at
            """,
            [(price, confirmed, seen, asin) for asin, price, confirmed, seen in valid],
        )
    return len(valid)
