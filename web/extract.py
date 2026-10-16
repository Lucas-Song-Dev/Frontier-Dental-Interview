"""Parse Safco listing and product HTML. HTML in, data out."""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Any, cast
from urllib.parse import unquote, urlparse

_PACK = re.compile(r"(\d[\d,]*)\s*/\s*([A-Za-z]+)")
_JSON_LD = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>',
    re.S,
)


@dataclass
class ParsedCard:
    slug: str
    name: str
    manufacturer: str
    price: str


@dataclass
class ParsedListing:
    key: str
    title: str
    stats: str
    cards: list[ParsedCard]


@dataclass
class ParsedVariant:
    sku: str
    name: str
    group: str
    mfr: str
    stock: str
    price: str
    pack_size: str = ""
    specification: str = ""
    attributes: str = "{}"
    brand: str = ""


@dataclass
class ParsedProduct:
    slug: str
    name: str
    sku: str
    price: str
    availability: str
    category: str
    variants: list[ParsedVariant] = field(default_factory=list)
    brand: str = ""
    url: str = ""
    description: str = ""
    category_path: str = ""
    image_urls: str = "[]"
    alternatives: str = "[]"
    attributes: str = "{}"
    recovered: bool = False


def unescape(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip()
    for _ in range(2):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text


def money(amount: str) -> str:
    return f"${amount}"


def classify_page(url: str, page: str) -> str:
    """Label a page from its URL and the markers these templates actually emit."""
    if "window.masterData" in page or json_ld_product(page):
        return "product"
    if "ais-Hits-item" in page or "ais-Stats" in page:
        return "listing"
    if "/product/" in url:
        return "product"
    if "/catalog/" in url:
        return "listing"
    return "other"


def json_ld_product(page: str) -> dict:
    for match in _JSON_LD.finditer(page):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if isinstance(node, dict) and node.get("@type") == "Product":
                return node
    return {}


def availability_label(offer: object) -> str:
    if not isinstance(offer, dict):
        return ""
    raw = str(offer.get("availability") or "")
    if raw.endswith("InStock"):
        return "In stock"
    if raw.endswith("OutOfStock"):
        return "Out of stock"
    return raw.rsplit("/", 1)[-1]


def pack_size(name: str, specification: str) -> str:
    for text in (specification, name):
        match = _PACK.search(text)
        if match:
            count = match.group(1).replace(",", "")
            return f"{count}/{match.group(2).lower()}"
    return ""


def extract_variants(page: str) -> list[ParsedVariant]:
    marker = "window.masterData = "
    start = page.find(marker)
    if start < 0:
        raise ValueError("masterData missing")
    raw, _end = json.JSONDecoder().raw_decode(page[start + len(marker) :])
    data = json.loads(raw) if isinstance(raw, str) else raw
    if isinstance(data, list):
        paired: dict[str, object] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            sku = str(item.get("sku") or item.get("product_sku") or "")
            if sku:
                paired[sku] = item
        data = paired
    if not isinstance(data, dict):
        raise ValueError("masterData is not a product map")
    variants: list[ParsedVariant] = []
    for sku, item in data.items():
        if not isinstance(item, dict):
            continue
        price_html = item.get("price") or ""
        if isinstance(price_html, dict):
            price_html = price_html.get("price") or ""
        amounts = re.findall(r'data-price-amount="([0-9.]+)"', str(price_html))
        name = unescape(str(item.get("name") or ""))
        specification = unescape(str(item.get("description") or ""))
        if specification.lower() == "false":
            specification = ""
        pack = pack_size(name, specification)
        group = unescape(str(item.get("itemgroup") or ""))
        attributes: dict[str, str] = {}
        if group:
            attributes["item_group"] = group
        if pack:
            attributes["pack_size"] = pack
        if specification:
            attributes["specification"] = specification
        hazard = item.get("hazard_type")
        if hazard not in (None, "", [], "[]", False, "false", "False"):
            attributes["hazard"] = unescape(str(hazard))
        variants.append(
            ParsedVariant(
                sku=str(sku),
                name=name,
                group=group,
                mfr=unescape(str(item.get("manufacturer_part_number") or "")),
                stock=unescape(str(item.get("stock_availability_label") or "")),
                price=money(amounts[0]) if amounts else "",
                pack_size=pack,
                specification=specification,
                attributes=json.dumps(attributes, ensure_ascii=False),
                brand=unescape(str(item.get("manufacturer_name") or "")),
            )
        )
    variants.sort(key=lambda row: (row.group, row.name, row.sku))
    return variants


def extract_product(page: str, slug: str, category: str, url: str = "") -> ParsedProduct:
    linked = json_ld_product(page)
    recovered = False
    try:
        variants = extract_variants(page)
    except ValueError:
        if not linked.get("name"):
            raise
        variants = []
        recovered = True
    offers = linked.get("offers") or {}
    price = offers.get("price") if isinstance(offers, dict) else ""
    name = unescape(str(linked.get("name") or slug))
    brand = _brand(linked)
    if not brand:
        brand = next((row.brand for row in variants if row.brand), "")
    description = unescape(str(linked.get("description") or ""))
    if not description:
        block = re.search(
            r'class="[^"]*product-description[^"]*"[^>]*>(.*?)</div>',
            page,
            re.S,
        )
        if block:
            description = unescape(re.sub(r"<[^>]+>", " ", block.group(1)))
    groups = sorted({row.group for row in variants if row.group})
    attributes = {"item_groups": groups} if groups else {}
    return ParsedProduct(
        slug=slug,
        name=name,
        sku=unescape(str(linked.get("sku") or "")),
        price=money(str(price)) if price else "",
        availability=availability_label(offers),
        category=category,
        variants=variants,
        brand=brand,
        url=url or f"https://www.safcodental.com/product/{slug}",
        description=description,
        category_path=category_path(page, name),
        image_urls=json.dumps(image_urls(page, linked), ensure_ascii=False),
        alternatives=json.dumps(alternative_products(page, slug), ensure_ascii=False),
        attributes=json.dumps(attributes, ensure_ascii=False),
        recovered=recovered,
    )


def listing_cards(page: str) -> list[ParsedCard]:
    """Product cards on a listing page.

    A non-empty JSON-LD product ItemList is the list the server sent. The hit
    grid is the fallback for HTML saved after JavaScript filled the container.
    The two lists are not merged.
    """
    linked = _jsonld_cards(page)
    if linked:
        return linked
    return _hit_cards(page)


def extract_listing(page: str, key: str) -> ParsedListing:
    heading = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    title = unescape(re.sub(r"<[^>]+>", "", heading.group(1))) if heading else key
    stats_at = page.find('class="ais-Stats"')
    stats = ""
    if stats_at >= 0:
        block = unescape(re.sub(r"<[^>]+>", " ", page[stats_at : stats_at + 700]))
        found = re.search(r"Items\s+\d+\s*-\s*\d+\s+of\s+\d+", block)
        stats = found.group(0) if found else ""
    return ParsedListing(key=key, title=title, stats=stats, cards=listing_cards(page))


def _jsonld_cards(page: str) -> list[ParsedCard]:
    cards: list[ParsedCard] = []
    seen: set[str] = set()
    for match in _JSON_LD.finditer(page):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        for node in _json_nodes(data):
            if "ItemList" not in _node_types(node):
                continue
            elements = node.get("itemListElement")
            if not isinstance(elements, list):
                continue
            for element in elements:
                item = _as_dict(element)
                if item is None:
                    continue
                card = _card_from_list_item(item)
                if card is None or card.slug in seen:
                    continue
                seen.add(card.slug)
                cards.append(card)
    return cards


def _json_nodes(data: object) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    pending: list[object] = [data]
    while pending:
        current = pending.pop(0)
        if isinstance(current, list):
            pending.extend(current)
            continue
        node = _as_dict(current)
        if node is None:
            continue
        found.append(node)
        graph = node.get("@graph")
        if isinstance(graph, list):
            pending.extend(graph)
    return found


def _card_from_list_item(element: dict[str, Any]) -> ParsedCard | None:
    nested = _as_dict(element.get("item"))
    product = nested if nested is not None and "Product" in _node_types(nested) else None
    if product is None and "Product" in _node_types(element):
        product = element
    if product is None:
        return None
    url = product.get("url") or element.get("url") or ""
    slug = _product_slug(url if isinstance(url, str) else "")
    if not slug:
        return None
    name = unescape(str(product.get("name") or element.get("name") or ""))
    return ParsedCard(
        slug=slug,
        name=name,
        manufacturer=_brand(cast(dict[Any, Any], product)),
        price=_offer_price(product.get("offers")),
    )


def _as_dict(value: object) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return cast(dict[str, Any], value)
    return None


def _node_types(node: dict[str, Any]) -> set[str]:
    raw = node.get("@type")
    if isinstance(raw, str):
        return {raw}
    if isinstance(raw, list):
        return {item for item in raw if isinstance(item, str)}
    return set()


def _product_slug(url: str) -> str:
    if "/product/" not in url:
        return ""
    slug = url.split("/product/", 1)[1]
    slug = unquote(slug.split("?", 1)[0].split("#", 1)[0]).strip("/")
    return slug


def _offer_price(offers: object) -> str:
    if isinstance(offers, list):
        offers = offers[0] if offers else None
    if not isinstance(offers, dict):
        return ""
    price = offers.get("price")
    if price in (None, ""):
        return ""
    text = str(price).replace(",", "").strip()
    if not text:
        return ""
    if text.startswith("$"):
        return text
    return money(text)


def _hit_cards(page: str) -> list[ParsedCard]:
    cards: list[ParsedCard] = []
    seen: set[str] = set()
    for chunk in page.split('<li class="ais-Hits-item')[1:]:
        href = re.search(r'href="https://www\.safcodental\.com/product/([^"]+)"', chunk)
        name = re.search(r'itemprop="name"[^>]*>\s*([^<]+)', chunk)
        if not href or not name:
            continue
        slug = unquote(href.group(1)).strip("/")
        if slug in seen:
            continue
        seen.add(slug)
        maker = re.search(r'manufacturer-name[^>]*>\s*([^<]+)', chunk)
        amount = re.search(r"\$\s*([0-9,]+\.[0-9]{2})", chunk)
        cards.append(
            ParsedCard(
                slug=slug,
                name=unescape(name.group(1)),
                manufacturer=unescape(maker.group(1)) if maker else "",
                price=money(amount.group(1).replace(",", "")) if amount else "",
            )
        )
    return cards


def category_path(page: str, product_name: str) -> str:
    match = re.search(r'<nav[^>]*class="[^"]*breadcrumbs[^"]*"[^>]*>(.*?)</nav>', page, re.S)
    if not match:
        return ""
    block = match.group(1)
    parts: list[str] = []
    for inner in re.findall(r"<a\b[^>]*>(.*?)</a>", block, re.S):
        text = unescape(re.sub(r"<[^>]+>", "", inner))
        if text:
            parts.append(text)
    last = re.search(r'class="breadcrumb-last-node[^"]*"[^>]*>(.*?)</span>', block, re.S)
    if last:
        text = unescape(re.sub(r"<[^>]+>", "", last.group(1)))
        if text:
            parts.append(text)
    if parts and parts[0].lower() == "home":
        parts = parts[1:]
    if parts and product_name and _soft(parts[-1]) == _soft(product_name):
        parts = parts[:-1]
    return " / ".join(parts)


def image_urls(page: str, linked: dict) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    image = linked.get("image") or []
    if isinstance(image, str):
        image = [image]
    if isinstance(image, list):
        for item in image:
            if isinstance(item, dict):
                item = item.get("url") or ""
            _add_image(found, seen, str(item))
    for match in re.finditer(r'itemprop="image"[^>]*src="([^"]+)"', page):
        _add_image(found, seen, match.group(1))
    for match in re.finditer(r'<meta property="og:image" content="([^"]+)"', page):
        _add_image(found, seen, match.group(1))
    return found


def alternative_products(page: str, slug: str) -> list[dict[str, str]]:
    """Recommendation cards rendered in the saved HTML. Algolia is not called."""
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    start = 0
    while True:
        index = page.find('href="', start)
        if index < 0:
            break
        end = page.find('"', index + 6)
        if end < 0:
            break
        href = page[index + 6 : end]
        start = end + 1
        if "/product/" not in href:
            continue
        alt = href.split("/product/", 1)[1].strip("/")
        alt = unquote(alt.split("?", 1)[0].split("#", 1)[0])
        if len(alt) < 6 or alt in {slug, "clearance-item"} or alt in seen:
            continue
        name = _card_name(page[end : end + 900])
        if not name:
            continue
        seen.add(alt)
        found.append({"name": name, "url": f"https://www.safcodental.com/product/{alt}"})
    return found


def pagination_links(page: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for href in re.findall(r'href="(https://www\.safcodental\.com[^"#]*)"', page):
        if not re.search(r"[?&](?:page|p)=", href):
            continue
        if href in seen:
            continue
        seen.add(href)
        found.append(href)
    return found


def _card_name(window: str) -> str:
    """Prefer the rendered alt or title. Skip the Alpine binding :alt=\"product.image?.label\"."""
    for match in re.finditer(r'(?<![:\w])(?:alt|title)="([^"]+)"', window):
        name = re.sub(r"<[^>]+>", " ", unescape(match.group(1)))
        name = re.sub(r"\s+", " ", name).strip()
        if not name or "product.image" in name or "?" in name or "<" in name:
            continue
        return name
    return ""


def _brand(linked: dict) -> str:
    raw = linked.get("brand")
    if isinstance(raw, dict):
        return unescape(str(raw.get("name") or ""))
    if isinstance(raw, str):
        return unescape(raw)
    return ""


def _soft(value: str) -> str:
    value = value.lower().replace("®", "").replace("™", "")
    return re.sub(r"[^a-z0-9]+", "", value)


def _add_image(found: list[str], seen: set[str], url: str) -> None:
    url = unescape(url).strip()
    if url.startswith("//"):
        url = "https:" + url
    if not url.startswith("https://") or url.startswith("https://data:"):
        return
    key = urlparse(url).path
    if not key or key in seen:
        return
    seen.add(key)
    found.append(url)
