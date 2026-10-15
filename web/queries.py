"""Named read-only queries. The server, not the browser, decides who may run each one."""

from __future__ import annotations

import re
from dataclasses import dataclass

LEVELS = ("viewer", "operator", "admin")


@dataclass(frozen=True)
class QuerySpec:
    id: str
    title: str
    level: str
    phrase: str
    sql: str
    needs_sku: bool = False


QUERIES: tuple[QuerySpec, ...] = (
    QuerySpec(
        "in_stock_count",
        "Child SKUs in stock",
        "viewer",
        "How many child SKUs are in stock?",
        "SELECT sku, variant_name, stock, variant_price, product_name "
        "FROM catalog_rows WHERE stock = 'In stock' ORDER BY product_name, sku LIMIT 200",
    ),
    QuerySpec(
        "special_order_variants",
        "Special-order variants",
        "viewer",
        "Show special-order variants",
        "SELECT sku, variant_name, stock, variant_price, product_name "
        "FROM catalog_rows WHERE stock = 'Special order' "
        "ORDER BY product_name, sku",
    ),
    QuerySpec(
        "cheapest_perma",
        "Cheapest Perma Sharp sutures",
        "viewer",
        "What is the cheapest Perma Sharp suture?",
        "SELECT sku, variant_name, stock, variant_price, product_name "
        "FROM catalog_rows WHERE product_name LIKE '%Perma Sharp%' "
        "AND variant_price IS NOT NULL ORDER BY variant_price ASC, sku LIMIT 5",
    ),
    QuerySpec(
        "cheapest_overall",
        "Cheapest child SKUs",
        "viewer",
        "What is the cheapest child SKU?",
        "SELECT sku, variant_name, stock, variant_price, product_name "
        "FROM catalog_rows WHERE variant_price IS NOT NULL "
        "ORDER BY variant_price ASC, sku LIMIT 5",
    ),
    QuerySpec(
        "lavender_in_stock",
        "Lavender Nitrile sizes in stock",
        "viewer",
        "Which Lavender Nitrile sizes are in stock?",
        "SELECT sku, variant_name, stock, variant_price, product_name "
        "FROM catalog_rows WHERE product_name LIKE '%Lavender%' "
        "AND stock = 'In stock' ORDER BY sku",
    ),
    QuerySpec(
        "suture_variants",
        "Suture child SKUs",
        "viewer",
        "Show suture child SKUs",
        "SELECT sku, variant_name, item_group, stock, variant_price, product_name "
        "FROM catalog_rows WHERE category_key = 'sutures' "
        "ORDER BY product_name, variant_price, sku",
    ),
    QuerySpec(
        "glove_variants",
        "Glove child SKUs",
        "viewer",
        "Show glove child SKUs",
        "SELECT sku, variant_name, stock, variant_price, product_name "
        "FROM catalog_rows WHERE category_key = 'gloves' ORDER BY product_name, sku",
    ),
    QuerySpec(
        "product_identity",
        "Brand, pack size, and category",
        "viewer",
        "Show brand, pack size, and category path",
        "SELECT product_name, brand, sku, pack_size, category_path, url "
        "FROM catalog_rows ORDER BY category_path, product_name, sku",
    ),
    QuerySpec(
        "listing_cards",
        "Saved listing cards",
        "viewer",
        "Show the saved listing cards",
        "SELECT manufacturer, name, price_amount, saved, category_key "
        "FROM listing_cards ORDER BY category_key, name",
    ),
    QuerySpec(
        "sku_lookup",
        "Lookup one SKU",
        "viewer",
        "Look up SKU PFPJK",
        "SELECT sku, variant_name, mfr_number, stock, variant_price, product_name, parent_sku "
        "FROM catalog_rows WHERE sku = ? OR parent_sku = ? OR mfr_number = ?",
        needs_sku=True,
    ),
    QuerySpec(
        "pagination_gate",
        "Listing pagination rule",
        "operator",
        "Is listing pagination allowed?",
        "SELECT key, value FROM gate WHERE key IN ('pagination_page', 'pagination_p')",
    ),
    QuerySpec(
        "sitemap_gate",
        "Sitemap counts",
        "operator",
        "What does the sitemap list?",
        "SELECT key, value FROM gate WHERE key LIKE 'sitemap_%'",
    ),
    QuerySpec(
        "crawl_delay_gate",
        "Crawl-delay",
        "operator",
        "Is there a crawl delay?",
        "SELECT key, value FROM gate WHERE key = 'crawl_delay'",
    ),
    QuerySpec(
        "all_gate",
        "Robots gate records",
        "operator",
        "Show the robots gate",
        "SELECT key, value FROM gate ORDER BY key",
    ),
    QuerySpec(
        "recent_jobs",
        "Recent jobs",
        "admin",
        "Show recent jobs",
        "SELECT id, kind, status, started_at, finished_at, products, variants, cards, message "
        "FROM jobs ORDER BY id DESC LIMIT 20",
    ),
    QuerySpec(
        "crawl_settings",
        "Crawl settings",
        "admin",
        "Show the crawl settings",
        "SELECT key, value FROM settings WHERE key IN ("
        "'crawl_interval_minutes', 'min_delay_seconds', 'max_concurrency', "
        "'max_products', 'max_pages_per_category', 'user_level') ORDER BY key",
    ),
)

BY_ID = {spec.id: spec for spec in QUERIES}


def normalize_level(level: str) -> str:
    cleaned = level.strip().lower()
    if cleaned in LEVELS:
        return cleaned
    return "viewer"


def level_allows(spec: QuerySpec, level: str) -> bool:
    return LEVELS.index(normalize_level(level)) >= LEVELS.index(spec.level)


def describe(level: str) -> list[dict[str, object]]:
    current = normalize_level(level)
    return [
        {
            "id": spec.id,
            "title": spec.title,
            "level": spec.level,
            "phrase": spec.phrase,
            "sql": spec.sql,
            "needs_sku": spec.needs_sku,
            "allowed": level_allows(spec, current),
        }
        for spec in QUERIES
    ]


def allowed_ids(level: str) -> list[str]:
    return [str(item["id"]) for item in describe(level) if item["allowed"]]


def bind_sku(raw: str) -> tuple[str, str, str]:
    sku = raw.strip() or "PFPJK"
    if not re.fullmatch(r"[A-Za-z0-9-]{2,40}", sku):
        raise ValueError("SKU must be 2 to 40 letters, numbers, or hyphens.")
    return (sku, sku, sku)


def match_question(question: str) -> tuple[str, dict[str, str]] | None:
    """Pick a catalog id from the question. The caller still checks the level."""
    low = question.lower().replace("-", " ")
    sku = _sku(question)
    if re.search(r"pagination|\?page|\?p=", low):
        return ("pagination_gate", {})
    if "sitemap" in low:
        return ("sitemap_gate", {})
    if "crawl delay" in low or re.search(r"\bdelay\b", low):
        return ("crawl_delay_gate", {})
    if re.search(r"\bjobs?\b", low):
        return ("recent_jobs", {})
    if re.search(r"\bsettings?\b|\bconcurrency\b|\binterval\b", low):
        return ("crawl_settings", {})
    if re.search(r"robots|disallow|user-agent|user agent|\bgate\b", low):
        return ("all_gate", {})
    if "special order" in low:
        return ("special_order_variants", {})
    if re.search(r"\b(cheapest|lowest|least expensive)\b", low) and "perma" in low:
        return ("cheapest_perma", {})
    if re.search(r"\b(cheapest|lowest|least expensive)\b", low):
        return ("cheapest_overall", {})
    if "lavender" in low and "stock" in low:
        return ("lavender_in_stock", {})
    if re.search(r"\b(how many|count|number of)\b", low) and "stock" in low:
        return ("in_stock_count", {})
    if re.search(r"\b(listing|cards|manufacturer)\b", low):
        return ("listing_cards", {})
    if "glove" in low:
        return ("glove_variants", {})
    if "suture" in low or "perma" in low or "surgifoam" in low:
        return ("suture_variants", {})
    if sku:
        return ("sku_lookup", {"sku": sku})
    if "stock" in low:
        return ("in_stock_count", {})
    return None


def _sku(question: str) -> str | None:
    found: list[str] = []
    for match in re.finditer(r"\b([A-Za-z]{2,}[A-Za-z0-9]*|\d{4,})\b", question):
        token = match.group(1)
        if token.lower() in _SKU_SKIP:
            continue
        found.append(token)
    for token in found:
        if token.isupper() or any(character.isdigit() for character in token):
            return token
    return None


_SKU_SKIP = {
        "how",
        "many",
        "what",
        "which",
        "show",
        "list",
        "the",
        "stock",
        "price",
        "with",
        "from",
        "that",
        "this",
        "under",
        "over",
        "special",
        "order",
        "direct",
        "manufacturer",
        "backorder",
        "cheapest",
        "listing",
        "cards",
        "perma",
        "sharp",
        "surgifoam",
        "lavender",
        "nitrile",
        "gloves",
        "glove",
        "sutures",
        "suture",
        "child",
        "skus",
        "sku",
        "sizes",
        "size",
        "saved",
        "recent",
        "jobs",
        "settings",
        "crawl",
        "delay",
        "robots",
        "gate",
        "sitemap",
        "pagination",
        "allowed",
        "look",
        "lookup",
    }
