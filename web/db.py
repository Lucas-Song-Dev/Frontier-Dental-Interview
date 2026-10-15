"""SQLite snapshot of the catalog desk. Reads are SELECT-only."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT.parent / "data" / "catalog.db"

SCHEMA = """
CREATE TABLE categories (
    key TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    stats TEXT NOT NULL
);

CREATE TABLE products (
    slug TEXT PRIMARY KEY,
    category_key TEXT NOT NULL REFERENCES categories(key),
    name TEXT NOT NULL,
    parent_sku TEXT NOT NULL,
    price_amount REAL,
    availability TEXT NOT NULL
);

CREATE TABLE variants (
    sku TEXT PRIMARY KEY,
    product_slug TEXT NOT NULL REFERENCES products(slug),
    name TEXT NOT NULL,
    item_group TEXT NOT NULL,
    mfr_number TEXT NOT NULL,
    stock TEXT NOT NULL,
    price_amount REAL
);

CREATE TABLE listing_cards (
    id INTEGER PRIMARY KEY,
    category_key TEXT NOT NULL REFERENCES categories(key),
    slug TEXT NOT NULL,
    name TEXT NOT NULL,
    manufacturer TEXT NOT NULL,
    price_amount REAL,
    saved INTEGER NOT NULL
);

CREATE TABLE gate (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE VIEW catalog_rows AS
SELECT
    v.sku,
    v.name AS variant_name,
    v.item_group,
    v.mfr_number,
    v.stock,
    v.price_amount AS variant_price,
    p.slug AS product_slug,
    p.name AS product_name,
    p.parent_sku,
    p.price_amount AS parent_price,
    p.availability,
    c.key AS category_key,
    c.title AS category_title
FROM variants AS v
JOIN products AS p ON p.slug = v.product_slug
JOIN categories AS c ON c.key = p.category_key;

CREATE INDEX idx_variants_product ON variants(product_slug);
CREATE INDEX idx_variants_stock ON variants(stock);
CREATE INDEX idx_cards_category ON listing_cards(category_key);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    products INTEGER NOT NULL DEFAULT 0,
    variants INTEGER NOT NULL DEFAULT 0,
    cards INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL DEFAULT ''
);
"""

DEFAULT_SETTINGS = {
    "crawl_interval_minutes": "360",
    "min_delay_seconds": "3",
    "max_concurrency": "1",
    "max_products": "40",
    "max_pages_per_category": "5",
    "user_level": "viewer",
}

SETTING_BOUNDS = {
    "crawl_interval_minutes": (15, 10080),
    "min_delay_seconds": (1, 60),
    "max_concurrency": (1, 2),
    "max_products": (1, 5000),
    "max_pages_per_category": (1, 20),
}

SCHEMA_DOC = """
Tables in data/catalog.db (snapshot only, not the live Safco site):

categories(key, title, stats)
products(slug, category_key, name, parent_sku, price_amount, availability)
variants(sku, product_slug, name, item_group, mfr_number, stock, price_amount)
listing_cards(id, category_key, slug, name, manufacturer, price_amount, saved)
gate(key, value)
catalog_rows: variants joined to their parent product and category.
  columns: sku, variant_name, item_group, mfr_number, stock, variant_price,
  product_slug, product_name, parent_sku, parent_price, availability,
  category_key, category_title

price_amount and variant_price are numbers in USD, or null.
stock values include "In stock", "Special order", "Backorder",
"Direct from manufacturer".
category_key is "sutures" or "gloves".
saved is 1 when a listing card has a full product sheet.
settings(key, value) holds crawl interval, delay, concurrency, caps, and user_level.
user_level is viewer, operator, or admin. The server uses it to allow listed queries.
jobs records each snapshot refresh.
Queries must be a single read-only SELECT or WITH.
"""

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|attach|detach|pragma|replace|create|vacuum|reindex)\b",
    re.IGNORECASE,
)


def price_amount(value: str) -> float | None:
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)", value.replace(",", ""))
    if not match:
        return None
    return float(match.group(1))


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _utc_now() -> str:
    from datetime import datetime

    return datetime.now(UTC).replace(microsecond=0).isoformat()


def ensure_database() -> None:
    """Create catalog.db, or rebuild it when settings and jobs are missing."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        probe = sqlite3.connect(DB_PATH)
        try:
            probe.execute("SELECT 1 FROM settings LIMIT 1")
            probe.execute("SELECT 1 FROM jobs LIMIT 1")
            ready = True
        except sqlite3.Error:
            ready = False
        finally:
            probe.close()
        if ready:
            return
        DB_PATH.unlink()
    connection = connect()
    try:
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?)",
            list(DEFAULT_SETTINGS.items()),
        )
        connection.commit()
    finally:
        connection.close()


def reload_catalog(catalog: object) -> dict[str, int]:
    """Replace catalog rows. Settings and jobs stay in place."""
    categories = catalog.categories  # type: ignore[attr-defined]
    products = catalog.products  # type: ignore[attr-defined]
    connection = connect()
    try:
        connection.execute("DELETE FROM variants")
        connection.execute("DELETE FROM listing_cards")
        connection.execute("DELETE FROM products")
        connection.execute("DELETE FROM categories")
        connection.execute("DELETE FROM gate")
        connection.executemany(
            "INSERT INTO categories (key, title, stats) VALUES (?, ?, ?)",
            [(key, category.title, category.stats) for key, category in categories.items()],
        )
        connection.executemany(
            """
            INSERT INTO products
                (slug, category_key, name, parent_sku, price_amount, availability)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    product.slug,
                    product.category,
                    product.name,
                    product.sku,
                    price_amount(product.price),
                    product.availability,
                )
                for product in products.values()
            ],
        )
        variant_rows = []
        for product in products.values():
            for variant in product.variants:
                variant_rows.append(
                    (
                        variant.sku,
                        product.slug,
                        variant.name,
                        variant.group,
                        variant.mfr,
                        variant.stock,
                        price_amount(variant.price),
                    )
                )
        connection.executemany(
            """
            INSERT INTO variants
                (sku, product_slug, name, item_group, mfr_number, stock, price_amount)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            variant_rows,
        )
        card_rows = []
        for category in categories.values():
            for card in category.cards:
                card_rows.append(
                    (
                        category.key,
                        card.slug,
                        card.name,
                        card.manufacturer,
                        price_amount(card.price),
                        1 if card.saved else 0,
                    )
                )
        connection.executemany(
            """
            INSERT INTO listing_cards
                (category_key, slug, name, manufacturer, price_amount, saved)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            card_rows,
        )
        connection.executemany(
            "INSERT INTO gate (key, value) VALUES (?, ?)",
            [
                ("recommendation", "yes"),
                ("crawl_allowed", ""),
                ("seed_paths", "allowed"),
                ("product_paths", "allowed"),
                ("pagination_page", "disallowed"),
                ("pagination_p", "disallowed"),
                ("crawl_delay", "none"),
                ("user_agent", "*"),
                ("sitemap_products", "3939"),
                ("sitemap_categories", "282"),
            ],
        )
        connection.commit()
        return {
            "products": len(products),
            "variants": len(variant_rows),
            "cards": len(card_rows),
        }
    finally:
        connection.close()


def ensure_settings() -> None:
    """Insert any settings keys added after the database file was created."""
    connection = connect()
    try:
        connection.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
            list(DEFAULT_SETTINGS.items()),
        )
        connection.commit()
    finally:
        connection.close()


def get_settings() -> dict[str, str]:
    connection = connect()
    try:
        rows = connection.execute("SELECT key, value FROM settings").fetchall()
    finally:
        connection.close()
    found = {row["key"]: row["value"] for row in rows}
    return {key: found.get(key, default) for key, default in DEFAULT_SETTINGS.items()}


def save_settings(incoming: dict[str, str]) -> None:
    cleaned: dict[str, str] = {}
    for key, (low, high) in SETTING_BOUNDS.items():
        raw = (incoming.get(key) or "").strip()
        if not raw.isdigit():
            raise ValueError(f"{key.replace('_', ' ')} must be a whole number.")
        value = int(raw)
        if value < low or value > high:
            raise ValueError(f"{key.replace('_', ' ')} must be between {low} and {high}.")
        cleaned[key] = str(value)
    if "user_level" in incoming:
        level = incoming["user_level"].strip().lower()
        if level not in {"viewer", "operator", "admin"}:
            raise ValueError("User level must be viewer, operator, or admin.")
        cleaned["user_level"] = level
    connection = connect()
    try:
        connection.executemany(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            list(cleaned.items()),
        )
        connection.commit()
    finally:
        connection.close()


def start_job(kind: str) -> int:
    connection = connect()
    try:
        cursor = connection.execute(
            "INSERT INTO jobs (kind, status, started_at) VALUES (?, 'running', ?)",
            (kind, _utc_now()),
        )
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        connection.close()


def finish_job(
    job_id: int,
    status: str,
    products: int = 0,
    variants: int = 0,
    cards: int = 0,
    message: str = "",
) -> None:
    connection = connect()
    try:
        connection.execute(
            """
            UPDATE jobs
            SET status = ?, finished_at = ?, products = ?, variants = ?, cards = ?, message = ?
            WHERE id = ?
            """,
            (status, _utc_now(), products, variants, cards, message, job_id),
        )
        connection.commit()
    finally:
        connection.close()


def list_jobs() -> list[sqlite3.Row]:
    connection = connect()
    try:
        return connection.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT 50"
        ).fetchall()
    finally:
        connection.close()


def list_catalog_rows() -> list[dict[str, object]]:
    """Every child SKU joined to its product and category."""
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT sku, variant_name, item_group, mfr_number, stock, variant_price, "
            "product_slug, product_name, parent_sku, parent_price, availability, "
            "category_key, category_title FROM catalog_rows "
            "ORDER BY category_title, product_name, sku"
        ).fetchall()
    finally:
        connection.close()
    return [{key: row[key] for key in row.keys()} for row in rows]


def run_select(sql: str, params: tuple = ()) -> tuple[list[str], list[sqlite3.Row]]:
    statement = sql.strip().rstrip(";").strip()
    if not statement or ";" in statement:
        raise ValueError("Send one SELECT statement.")
    if not re.match(r"(?is)^(select|with)\b", statement):
        raise ValueError("Only read-only SELECT queries are allowed.")
    if _FORBIDDEN.search(statement):
        raise ValueError("Only read-only SELECT queries are allowed.")
    if not re.search(r"(?is)\blimit\b", statement):
        statement = f"{statement} LIMIT 40"
    connection = connect()
    try:
        cursor = connection.execute(statement, params)
        columns = [item[0] for item in cursor.description or []]
        rows = cursor.fetchall()
    finally:
        connection.close()
    return columns, rows
