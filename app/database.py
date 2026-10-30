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
            """
        )


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
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO price_checks
                (product_id, price_eur, list_price_eur, currency, checked_at, raw_note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (product_id, price_eur, list_price_eur, currency, _utc_now(), raw_note),
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
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO alerts
                (product_id, price_eur, list_price_eur, discount_percent, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING id
            """,
            (product_id, price_eur, list_price_eur, discount_percent, reason, _utc_now()),
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
