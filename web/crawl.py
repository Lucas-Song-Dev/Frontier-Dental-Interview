"""Walk allowed product URLs. Category is decided after the page is fetched.

Product links on the seed pages and one level of child category pages come first.
The rest of products.xml follows. Listing ?page= is not requested.

Extracted and out-of-scope URLs are skipped. Unresolved and failed URLs are tried again.
One run stops at the kept-parent cap and at the fetch budget.
"""

from __future__ import annotations

from collections.abc import Callable

import live
from db import get_settings, terminal_urls
from live import FetchFailed
from scope import scope_page


def crawl(
    on_progress: Callable[[str], None] | None = None,
    on_product: Callable[[tuple[str, str, str, str]], None] | None = None,
) -> tuple[str, dict[str, object]]:
    """Fetch the seed listings and the next slice of product URLs.

    Product links on the seed pages and on one level of child category pages are
    fetched in this run. ``?page=`` is not requested. ``max_pages_per_category``
    limits only the products.xml tail. When ``on_product`` is set, page HTML is
    handed off and not retained.
    """
    settings = get_settings()
    delay = int(settings["min_delay_seconds"])
    kept_cap = int(settings["max_products"])
    fetch_budget = int(settings["max_pages_per_category"])
    host = live.origin()
    fetcher = live.Fetcher(delay)

    def report(message: str) -> None:
        if on_progress is not None:
            on_progress(message)

    robots_url = f"{host}/robots.txt"
    report(f"Reading {robots_url}")
    robots = fetcher.fetch_robots()
    listings: list[tuple[str, str, str]] = []
    for key, url in live.seeds():
        report(f"Fetching {url}")
        listings.append((key, url, fetcher.get(url)))
    skip = terminal_urls()
    discovered: list[str] = []
    seen: set[str] = set()
    children: list[tuple[str, str]] = []
    seen_children: set[str] = set()

    def add_slug(slug: str, bucket: list[str]) -> None:
        if slug in seen:
            return
        seen.add(slug)
        bucket.append(slug)

    for key, url, html in listings:
        for slug in live._product_slugs(html):
            add_slug(slug, discovered)
        for child in live.child_category_urls(html, url):
            if child in seen_children:
                continue
            seen_children.add(child)
            children.append((key, child))
    failed: list[dict[str, str]] = []
    for key, child in children:
        if not fetcher.accepts(child):
            continue
        report(f"Fetching {child}")
        try:
            html = fetcher.get(child)
        except FetchFailed as exc:
            failed.append({"url": child, "detail": str(exc)})
            continue
        listings.append((key, child, html))
        for slug in live._product_slugs(html):
            add_slug(slug, discovered)
    report(f"Reading {host}/sitemap.xml")
    sitemap: list[str] = []
    for slug in fetcher.sitemap_slugs():
        add_slug(slug, sitemap)
    products: list[tuple[str, str, str, str]] = []
    attempts = 0
    kept = 0

    def fetch_slug(slug: str) -> None:
        nonlocal attempts, kept
        url = f"{host}/product/{slug}"
        if url in skip or not fetcher.accepts(url):
            return
        attempts += 1
        report(f"Fetching {url}")
        try:
            html = fetcher.get(url)
        except FetchFailed as exc:
            failed.append({"url": url, "detail": str(exc)})
            return
        item = (slug, "", url, html)
        if on_product is not None:
            on_product(item)
        else:
            products.append(item)
        if scope_page(html).status == "in_scope":
            kept += 1

    for slug in discovered:
        fetch_slug(slug)
    sitemap_attempts = 0
    for slug in sitemap:
        if sitemap_attempts >= fetch_budget or kept >= kept_cap:
            break
        before = attempts
        fetch_slug(slug)
        if attempts > before:
            sitemap_attempts += 1
    return robots, {
        "listings": listings,
        "products": products,
        "failed": failed,
        "attempts": attempts,
    }
