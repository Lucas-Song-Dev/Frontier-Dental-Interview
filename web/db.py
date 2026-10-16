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
    availability TEXT NOT NULL,
    brand TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    category_path TEXT NOT NULL DEFAULT '',
    image_urls TEXT NOT NULL DEFAULT '[]',
    alternatives TEXT NOT NULL DEFAULT '[]',
    attributes TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE variants (
    sku TEXT PRIMARY KEY,
    product_slug TEXT NOT NULL REFERENCES products(slug),
    name TEXT NOT NULL,
    item_group TEXT NOT NULL,
    mfr_number TEXT NOT NULL,
    stock TEXT NOT NULL,
    price_amount REAL,
    pack_size TEXT NOT NULL DEFAULT '',
    specification TEXT NOT NULL DEFAULT '',
    attributes TEXT NOT NULL DEFAULT '{}'
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
    v.pack_size,
    v.specification,
    v.attributes,
    p.slug AS product_slug,
    p.name AS product_name,
    p.parent_sku,
    p.price_amount AS parent_price,
    p.availability,
    p.brand,
    p.url,
    p.description,
    p.category_path,
    p.image_urls,
    p.alternatives,
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

CREATE TABLE agent_steps (
    position INTEGER PRIMARY KEY,
    stage TEXT NOT NULL,
    subject TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL
);

CREATE TABLE checkpoints (
    url TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    stage TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""

CATALOG_VIEW = """
DROP VIEW IF EXISTS catalog_rows;
CREATE VIEW catalog_rows AS
SELECT
    v.sku,
    v.name AS variant_name,
    v.item_group,
    v.mfr_number,
    v.stock,
    v.price_amount AS variant_price,
    v.pack_size,
    v.specification,
    v.attributes,
    p.slug AS product_slug,
    p.name AS product_name,
    p.parent_sku,
    p.price_amount AS parent_price,
    p.availability,
    p.brand,
    p.url,
    p.description,
    p.category_path,
    p.image_urls,
    p.alternatives,
    c.key AS category_key,
    c.title AS category_title
FROM variants AS v
JOIN products AS p ON p.slug = v.product_slug
JOIN categories AS c ON c.key = p.category_key;
"""

PRODUCT_COLUMNS = (
    ("brand", "TEXT NOT NULL DEFAULT ''"),
    ("url", "TEXT NOT NULL DEFAULT ''"),
    ("description", "TEXT NOT NULL DEFAULT ''"),
    ("category_path", "TEXT NOT NULL DEFAULT ''"),
    ("image_urls", "TEXT NOT NULL DEFAULT '[]'"),
    ("alternatives", "TEXT NOT NULL DEFAULT '[]'"),
    ("attributes", "TEXT NOT NULL DEFAULT '{}'"),
)

VARIANT_COLUMNS = (
    ("pack_size", "TEXT NOT NULL DEFAULT ''"),
    ("specification", "TEXT NOT NULL DEFAULT ''"),
    ("attributes", "TEXT NOT NULL DEFAULT '{}'"),
)

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
One row per child SKU. The same columns are in catalog_rows and in
data/sample/catalog.csv, catalog.json, and catalog.xlsx.

product_name          Parent product name.
brand                 Brand or manufacturer.
parent_sku            Parent SKU from JSON-LD.
sku                   Child SKU from masterData.
variant_name          Child product name.
mfr_number            Manufacturer part number.
item_group            Variant group, such as needle type.
category_key          sutures or gloves.
category_title        Listing title.
category_path         Breadcrumb under Home, excluding the product name.
url                   Public product URL.
variant_price         Child price in USD, or null.
parent_price          Parent JSON-LD price in USD, or null.
pack_size             Pack size parsed from the child specification or name.
stock                 Child stock label.
availability          Parent availability from JSON-LD.
description           Parent description.
specification         Child specification text.
attributes            JSON attributes for the child row.
image_urls            JSON list of parent image URLs.
alternatives          JSON list of recommendation cards: name, url.
product_slug          Product URL slug.

Tables: categories, products, variants, listing_cards, gate, settings, jobs,
agent_steps, checkpoints. catalog_rows joins variants to products and categories.
settings holds crawl interval, delay, concurrency, caps, and user_level.
user_level is viewer, operator, or admin.
jobs records each snapshot refresh.
agent_steps is the latest navigator, classifier, extractor, validator, and retry trace.
The stage column names the pipeline stage: navigator, classifier, extractor, validator, or retry.
checkpoints records fetched, extracted, out_of_scope, unresolved, failed, and skipped URLs.
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


def ensure_database() -> bool:
    """Create catalog.db. Return True when existing product rows predate brand extraction."""
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
            return _migrate()
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
    return False


def _migrate() -> bool:
    """Add extraction columns and the run tables. True when brand was missing."""
    connection = connect()
    try:
        rebuild = "brand" not in _column_names(connection, "products")
        _add_columns(connection, "products", PRODUCT_COLUMNS)
        _add_columns(connection, "variants", VARIANT_COLUMNS)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_steps (
                position INTEGER PRIMARY KEY,
                stage TEXT NOT NULL,
                subject TEXT NOT NULL,
                outcome TEXT NOT NULL,
                detail TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                url TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                stage TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        _copy_stage_column(connection, "agent_steps")
        _copy_stage_column(connection, "checkpoints")
        connection.executescript(CATALOG_VIEW)
        connection.commit()
        return rebuild
    finally:
        connection.close()


def _copy_stage_column(connection: sqlite3.Connection, table: str) -> None:
    """Copy a legacy agent column into stage and drop the old column."""
    columns = _column_names(connection, table)
    if "agent" not in columns:
        return
    if table == "agent_steps":
        connection.execute(
            """
            CREATE TABLE agent_steps_next (
                position INTEGER PRIMARY KEY,
                stage TEXT NOT NULL,
                subject TEXT NOT NULL,
                outcome TEXT NOT NULL,
                detail TEXT NOT NULL
            )
            """
        )
        stage_expr = "stage" if "stage" in columns else "agent"
        if "stage" in columns:
            stage_expr = "CASE WHEN stage != '' THEN stage ELSE agent END"
        connection.execute(
            f"""
            INSERT INTO agent_steps_next (position, stage, subject, outcome, detail)
            SELECT position, {stage_expr}, subject, outcome, detail FROM agent_steps
            """
        )
        connection.execute("DROP TABLE agent_steps")
        connection.execute("ALTER TABLE agent_steps_next RENAME TO agent_steps")
        return
    connection.execute(
        """
        CREATE TABLE checkpoints_next (
            url TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            stage TEXT NOT NULL,
            detail TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )
        """
    )
    stage_expr = "CASE WHEN stage != '' THEN stage ELSE agent END" if "stage" in columns else "agent"
    connection.execute(
        f"""
        INSERT INTO checkpoints_next (url, status, stage, detail, updated_at)
        SELECT url, status, {stage_expr}, detail, updated_at FROM checkpoints
        """
    )
    connection.execute("DROP TABLE checkpoints")
    connection.execute("ALTER TABLE checkpoints_next RENAME TO checkpoints")


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _add_columns(connection: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]) -> None:
    present = _column_names(connection, table)
    for name, declaration in columns:
        if name in present:
            continue
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")


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
        connection.execute("DELETE FROM agent_steps")
        connection.execute("DELETE FROM checkpoints")
        connection.executemany(
            "INSERT INTO categories (key, title, stats) VALUES (?, ?, ?)",
            [(key, category.title, category.stats) for key, category in categories.items()],
        )
        connection.executemany(
            """
            INSERT INTO products (
                slug, category_key, name, parent_sku, price_amount, availability,
                brand, url, description, category_path, image_urls, alternatives, attributes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    product.slug,
                    product.category,
                    product.name,
                    product.sku,
                    price_amount(product.price),
                    product.availability,
                    product.brand,
                    product.url,
                    product.description,
                    product.category_path,
                    product.image_urls,
                    product.alternatives,
                    product.attributes,
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
                        variant.pack_size,
                        variant.specification,
                        variant.attributes,
                    )
                )
        connection.executemany(
            """
            INSERT INTO variants (
                sku, product_slug, name, item_group, mfr_number, stock, price_amount,
                pack_size, specification, attributes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        steps = list(getattr(catalog, "steps", []) or [])
        connection.executemany(
            "INSERT INTO agent_steps (position, stage, subject, outcome, detail) VALUES (?, ?, ?, ?, ?)",
            [
                (index, step["stage"], step["subject"], step["outcome"], step["detail"])
                for index, step in enumerate(steps)
            ],
        )
        _write_checkpoints(connection, list(getattr(catalog, "checkpoints", []) or []))
        connection.commit()
        return {
            "products": len(products),
            "variants": len(variant_rows),
            "cards": len(card_rows),
        }
    finally:
        connection.close()


def _write_checkpoints(connection: sqlite3.Connection, checkpoints: list[dict[str, str]]) -> None:
    connection.executemany(
        """
        INSERT INTO checkpoints (url, status, stage, detail, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(url) DO UPDATE SET
            status = excluded.status,
            stage = excluded.stage,
            detail = excluded.detail,
            updated_at = excluded.updated_at
        """,
        [
            (item["url"], item["status"], item["stage"], item["detail"], _utc_now())
            for item in checkpoints
        ],
    )


def _drop_stored_out_of_scope(
    connection: sqlite3.Connection, checkpoints: list[dict[str, str]]
) -> None:
    """Remove a parent this run marked out of scope. Unresolved rows stay."""
    for item in checkpoints:
        if item.get("status") != "out_of_scope":
            continue
        url = str(item.get("url") or "")
        slugs: set[str] = set()
        if "/product/" in url:
            slug = url.split("/product/", 1)[1].split("?", 1)[0].strip("/")
            if slug:
                slugs.add(slug)
        if url:
            for row in connection.execute("SELECT slug FROM products WHERE url = ?", (url,)):
                slugs.add(str(row["slug"]))
        for slug in slugs:
            connection.execute("DELETE FROM variants WHERE product_slug = ?", (slug,))
            connection.execute("DELETE FROM products WHERE slug = ?", (slug,))


def terminal_urls() -> set[str]:
    """URLs a later crawl skips. Unresolved and failed URLs stay eligible."""
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT url FROM checkpoints WHERE status IN ('extracted', 'out_of_scope')"
        ).fetchall()
    except sqlite3.Error:
        return set()
    finally:
        connection.close()
    return {str(row["url"]) for row in rows}


def upsert_crawl(catalog: object) -> dict[str, int]:
    """Store this crawl without deleting unrelated parents or earlier checkpoints.

    Child rows are replaced only for a parent that was extracted again.
    A URL checkpointed out_of_scope drops that parent and its children.
    An empty product list leaves the other catalog products in place.
    An empty card list leaves that category's listing cards in place.
    A card is saved when this run extracted the parent or the parent is already stored.
    """
    categories = catalog.categories  # type: ignore[attr-defined]
    products = catalog.products  # type: ignore[attr-defined]
    connection = connect()
    try:
        if categories:
            connection.executemany(
                """
                INSERT INTO categories (key, title, stats) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    title = excluded.title,
                    stats = excluded.stats
                """,
                [(key, category.title, category.stats) for key, category in categories.items()],
            )
            stored = {
                str(row["slug"]) for row in connection.execute("SELECT slug FROM products")
            }
            card_rows = []
            for category in categories.values():
                if not category.cards:
                    continue
                connection.execute("DELETE FROM listing_cards WHERE category_key = ?", (category.key,))
                for card in category.cards:
                    saved = bool(card.saved) or card.slug in stored or card.slug in products
                    card_rows.append(
                        (
                            category.key,
                            card.slug,
                            card.name,
                            card.manufacturer,
                            price_amount(card.price),
                            1 if saved else 0,
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
        variant_rows = []
        for product in products.values():
            connection.execute(
                """
                INSERT INTO products (
                    slug, category_key, name, parent_sku, price_amount, availability,
                    brand, url, description, category_path, image_urls, alternatives, attributes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(slug) DO UPDATE SET
                    category_key = excluded.category_key,
                    name = excluded.name,
                    parent_sku = excluded.parent_sku,
                    price_amount = excluded.price_amount,
                    availability = excluded.availability,
                    brand = excluded.brand,
                    url = excluded.url,
                    description = excluded.description,
                    category_path = excluded.category_path,
                    image_urls = excluded.image_urls,
                    alternatives = excluded.alternatives,
                    attributes = excluded.attributes
                """,
                (
                    product.slug,
                    product.category,
                    product.name,
                    product.sku,
                    price_amount(product.price),
                    product.availability,
                    product.brand,
                    product.url,
                    product.description,
                    product.category_path,
                    product.image_urls,
                    product.alternatives,
                    product.attributes,
                ),
            )
            connection.execute("DELETE FROM variants WHERE product_slug = ?", (product.slug,))
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
                        variant.pack_size,
                        variant.specification,
                        variant.attributes,
                    )
                )
        for sku, _slug, *_rest in variant_rows:
            connection.execute("DELETE FROM variants WHERE sku = ?", (sku,))
        if variant_rows:
            connection.executemany(
                """
                INSERT INTO variants (
                    sku, product_slug, name, item_group, mfr_number, stock, price_amount,
                    pack_size, specification, attributes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                variant_rows,
            )
        connection.execute("DELETE FROM agent_steps")
        steps = list(getattr(catalog, "steps", []) or [])
        connection.executemany(
            "INSERT INTO agent_steps (position, stage, subject, outcome, detail) VALUES (?, ?, ?, ?, ?)",
            [
                (index, step["stage"], step["subject"], step["outcome"], step["detail"])
                for index, step in enumerate(steps)
            ],
        )
        checkpoints = list(getattr(catalog, "checkpoints", []) or [])
        _write_checkpoints(connection, checkpoints)
        _drop_stored_out_of_scope(connection, checkpoints)
        connection.commit()
        return {
            "products": len(products),
            "variants": len(variant_rows),
            "cards": sum(len(category.cards) for category in categories.values()) if categories else 0,
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


def update_job(job_id: int, message: str) -> None:
    """Replace the note on a job that is still running."""
    connection = connect()
    try:
        connection.execute(
            "UPDATE jobs SET message = ? WHERE id = ? AND status = 'running'",
            (message, job_id),
        )
        connection.commit()
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
            "SELECT sku, variant_name, item_group, mfr_number, stock, variant_price, pack_size, "
            "product_slug, product_name, brand, parent_sku, parent_price, availability, "
            "category_key, category_title, category_path, url FROM catalog_rows "
            "ORDER BY category_title, product_name, sku"
        ).fetchall()
    finally:
        connection.close()
    return [{key: row[key] for key in row.keys()} for row in rows]


def list_steps() -> list[dict[str, str]]:
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT stage, subject, outcome, detail FROM agent_steps ORDER BY position"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    return [{key: str(row[key]) for key in row.keys()} for row in rows]


def list_checkpoints() -> list[dict[str, str]]:
    connection = connect()
    try:
        rows = connection.execute(
            "SELECT url, status, stage, detail, updated_at FROM checkpoints ORDER BY status, url"
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        connection.close()
    return [{key: str(row[key]) for key in row.keys()} for row in rows]


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
