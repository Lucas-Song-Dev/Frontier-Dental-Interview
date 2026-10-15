"""Save a product sheet and image for every card on listing page 1."""

from __future__ import annotations

import json
import re
from pathlib import Path

from db import get_settings, reload_catalog
from export_catalog import publish
from extract import extract_listing
from live import ORIGIN, ROBOTS_URL, FetchBlocked, _Clock, _disallow_rules, _get, fetch_bytes

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
IMAGES = ROOT / "data" / "sample" / "images"
SAVED_HTML = {
    "surgifoam-reg": FIXTURES / "product_a.html",
    "perma-sharp-reg-sutures": FIXTURES / "product_variants.html",
    "lavender-nitrile": FIXTURES / "product_b.html",
}
LISTINGS = (
    ("sutures", f"{ORIGIN}/catalog/sutures-surgical-products", FIXTURES / "listing_sutures.html"),
    ("gloves", f"{ORIGIN}/catalog/gloves", FIXTURES / "listing_gloves.html"),
)


def image_extension(content_type: str, url: str) -> str:
    """Use the type Safco actually returned. Product photos are JPEG, not PNG."""
    kind = content_type.split(";", 1)[0].strip().lower()
    path = url.split("?", 1)[0].lower()
    if kind == "image/png" or path.endswith(".png"):
        return ".png"
    if kind == "image/webp" or path.endswith(".webp"):
        return ".webp"
    return ".jpg"


def remember_image(image_urls: str, local_path: str) -> str:
    """Put the saved file first and keep the public https URL."""
    try:
        current = json.loads(image_urls or "[]")
    except json.JSONDecodeError:
        current = []
    if not isinstance(current, list):
        current = []
    urls = [str(item) for item in current if str(item) != local_path]
    return json.dumps([local_path, *urls], ensure_ascii=False)


def save_page_one() -> None:
    """Fetch allowed /product/ URLs for page 1 and write each photo under data/sample/images."""
    from server import catalog_from_live

    delay = int(get_settings()["min_delay_seconds"])
    clock = _Clock(delay)
    robots = _get(ROBOTS_URL, clock, [])
    rules = _disallow_rules(robots)
    listings: list[tuple[str, str, str]] = []
    jobs: list[tuple[str, str]] = []
    for key, url, path in LISTINGS:
        html_text = path.read_text(encoding="utf-8")
        listings.append((key, url, html_text))
        for card in extract_listing(html_text, key).cards:
            jobs.append((key, card.slug))
    seen = {slug for _key, slug in jobs}
    for slug in SAVED_HTML:
        if slug not in seen:
            jobs.append(("sutures", slug))
    products: list[tuple[str, str, str, str]] = []
    for key, slug in jobs:
        url = f"{ORIGIN}/product/{slug}"
        saved = SAVED_HTML.get(slug)
        if saved is not None:
            products.append((slug, key, url, saved.read_text(encoding="utf-8")))
            print(f"sheet {slug} from saved HTML", flush=True)
            continue
        try:
            products.append((slug, key, url, _get(url, clock, rules)))
            print(f"sheet {slug} fetched", flush=True)
        except (FetchBlocked, RuntimeError) as exc:
            print(f"sheet {slug} skipped: {exc}", flush=True)
    catalog = catalog_from_live(
        robots,
        {"listings": listings, "products": products, "note": "Page 1 product sheets."},
    )
    thumbs: dict[str, str] = {}
    for _key, _url, path in LISTINGS:
        thumbs.update(_listing_images(path.read_text(encoding="utf-8")))
    IMAGES.mkdir(parents=True, exist_ok=True)
    for product in catalog.products.values():
        remote = _remote_image(product.image_urls) or thumbs.get(product.slug, "")
        if not remote:
            print(f"image {product.slug} missing", flush=True)
            continue
        current = _url_list(product.image_urls)
        if remote not in current:
            current.insert(0, remote)
        product.image_urls = json.dumps(current, ensure_ascii=False)
        try:
            body, content_type = fetch_bytes(remote, clock, rules)
        except (FetchBlocked, RuntimeError) as exc:
            print(f"image {product.slug} skipped: {exc}", flush=True)
            continue
        if not body:
            print(f"image {product.slug} empty", flush=True)
            continue
        name = product.slug + image_extension(content_type, remote)
        if not re.fullmatch(r"[a-z0-9-]+\.(?:jpg|jpeg|png|webp)", name):
            print(f"image {product.slug} name rejected", flush=True)
            continue
        (IMAGES / name).write_bytes(body)
        product.image_urls = remember_image(product.image_urls, f"/images/{name}")
        print(f"image {name} {len(body)} bytes", flush=True)
    reload_catalog(catalog)
    publish(catalog, catalog.steps)
    saved = sum(1 for product in catalog.products.values() if product.variants or product.sku)
    print(f"stored {saved} product sheets", flush=True)


def _listing_images(page: str) -> dict[str, str]:
    """Card photos already rendered on listing page 1. These files are JPEG."""
    found: dict[str, str] = {}
    for chunk in page.split('<li class="ais-Hits-item')[1:]:
        href = re.search(r'href="https://www\.safcodental\.com/product/([^"?#]+)"', chunk)
        image = re.search(r'<img\b[^>]*\bsrc="(https://www\.safcodental\.com/media/[^"]+)"', chunk)
        if not href or not image:
            continue
        found.setdefault(href.group(1).strip("/"), image.group(1).replace("&amp;", "&"))
    return found


def _remote_image(image_urls: str) -> str:
    for url in _url_list(image_urls):
        if url.startswith("https://"):
            return url
    return ""


def _url_list(image_urls: str) -> list[str]:
    try:
        current = json.loads(image_urls or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(current, list):
        return []
    return [str(item) for item in current]


if __name__ == "__main__":
    save_page_one()
