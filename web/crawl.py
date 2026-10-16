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


def crawl(on_progress: Callable[[str], None] | None = None) -> tuple[str, dict[str, object]]:
    """Fetch the seed listings and the next slice of product URLs."""
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
    candidates: list[str] = []
    seen: set[str] = set()
    children: list[str] = []
    seen_children: set[str] = set()
    for _key, url, html in listings:
        for slug in live._product_slugs(html):
            if slug in seen:
                continue
            seen.add(slug)
            candidates.append(slug)
        for child in live.child_category_urls(html, url):
            if child in seen_children:
                continue
            seen_children.add(child)
            children.append(child)
    failed: list[dict[str, str]] = []
    for child in children:
        if not fetcher.accepts(child):
            continue
        report(f"Fetching {child}")
        try:
            html = fetcher.get(child)
        except FetchFailed as exc:
            failed.append({"url": child, "detail": str(exc)})
            continue
        for slug in live._product_slugs(html):
            if slug in seen:
                continue
            seen.add(slug)
            candidates.append(slug)
    report(f"Reading {host}/sitemap.xml")
    for slug in fetcher.sitemap_slugs():
        if slug in seen:
            continue
        seen.add(slug)
        candidates.append(slug)
    products: list[tuple[str, str, str, str]] = []
    attempts = 0
    kept = 0
    for slug in candidates:
        if kept >= kept_cap or attempts >= fetch_budget:
            break
        url = f"{host}/product/{slug}"
        if url in skip:
            continue
        if not fetcher.accepts(url):
            continue
        attempts += 1
        report(f"Fetching {url}")
        try:
            html = fetcher.get(url)
        except FetchFailed as exc:
            failed.append({"url": url, "detail": str(exc)})
            continue
        products.append((slug, "", url, html))
        if scope_page(html).status == "in_scope":
            kept += 1
    return robots, {
        "listings": listings,
        "products": products,
        "failed": failed,
        "attempts": attempts,
    }
