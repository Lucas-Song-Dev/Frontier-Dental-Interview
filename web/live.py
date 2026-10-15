"""Fetch allowed Safco URLs. Listing ?page= and ?p= are never requested."""

from __future__ import annotations

import re
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from db import get_settings

ORIGIN = "https://www.safcodental.com"
ROBOTS_URL = f"{ORIGIN}/robots.txt"
SITEMAP_URL = f"{ORIGIN}/sitemap.xml"
SEEDS = (
    ("sutures", f"{ORIGIN}/catalog/sutures-surgical-products"),
    ("gloves", f"{ORIGIN}/catalog/gloves"),
)
USER_AGENT = "CatalogDesk/0.1 (local catalog desk; respects robots.txt)"


class FetchBlocked(Exception):
    """robots.txt disallows this URL, so it was not requested."""


def fetch_pages(known_products: list[tuple[str, str]]) -> tuple[str, dict[str, object]]:
    """Return the live robots body and the pages that were allowed.

    known_products is (slug, category) for product URLs already on the desk.
    Those /product/ URLs are allowed. Pagination URLs are not.
    """
    delay = int(get_settings()["min_delay_seconds"])
    cap = int(get_settings()["max_products"])
    clock = _Clock(delay)
    robots = _get(ROBOTS_URL, clock, [])
    rules = _disallow_rules(robots)
    listings: list[tuple[str, str, str]] = []
    for key, url in SEEDS:
        _require_allowed(url, rules)
        listings.append((key, url, _get(url, clock, rules)))
    discovered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for slug, category in known_products:
        seen.add(slug)
        discovered.append((slug, category))
    for key, _url, html in listings:
        for slug in _product_slugs(html):
            if slug in seen:
                continue
            seen.add(slug)
            discovered.append((slug, key))
    for slug in _sitemap_slugs(clock, rules):
        if slug in seen:
            continue
        seen.add(slug)
        discovered.append((slug, _category_for_slug(slug)))
    products: list[tuple[str, str, str, str]] = []
    skipped: list[str] = []
    for slug, category in discovered[:cap]:
        url = f"{ORIGIN}/product/{slug}"
        _require_allowed(url, rules)
        try:
            products.append((slug, category, url, _get(url, clock, rules)))
        except RuntimeError as exc:
            skipped.append(str(exc))
    fetched = [url for _key, url, _html in listings] + [url for _slug, _category, url, _html in products]
    note = f"Fetched {len(fetched)} URLs from safcodental.com. " + " ".join(fetched)
    if skipped:
        note += " Skipped: " + "; ".join(skipped)
    return robots, {"listings": listings, "products": products, "note": note}


def _require_allowed(url: str, rules: list[str]) -> None:
    path = _robot_path(url)
    if any(_matches(rule, path) for rule in rules):
        raise FetchBlocked(f"robots.txt disallows {url}")
    if re.search(r"[?&](page|p)=", url):
        raise FetchBlocked(f"Listing pagination is disallowed: {url}")


def _robot_path(url: str) -> str:
    parsed = urlparse(url)
    if parsed.query:
        return f"{parsed.path}?{parsed.query}"
    return parsed.path or "/"


def _disallow_rules(body: str) -> list[str]:
    rules: list[str] = []
    agent = ""
    for raw in body.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            agent = value
        elif key == "disallow" and agent == "*" and value:
            rules.append(value)
    return rules


def _matches(pattern: str, path: str) -> bool:
    parts: list[str] = []
    for character in pattern:
        if character == "*":
            parts.append(".*")
        elif character == "$":
            parts.append("$")
        else:
            parts.append(re.escape(character))
    return re.match("".join(parts), path) is not None


def _category_for_slug(slug: str) -> str:
    if "glove" in slug or "nitrile" in slug:
        return "gloves"
    return "sutures"


def _sitemap_slugs(clock: _Clock, rules: list[str]) -> list[str]:
    _require_allowed(SITEMAP_URL, rules)
    index = _get(SITEMAP_URL, clock, rules)
    maps = [loc for loc in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", index) if "product" in loc.lower()]
    slugs: list[str] = []
    for loc in maps:
        _require_allowed(loc, rules)
        body = _get(loc, clock, rules)
        for url in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", body):
            if "?" in url or "/product/" not in url:
                continue
            slug = url.split("/product/", 1)[1].strip("/")
            if slug:
                slugs.append(slug)
    return slugs


def _product_slugs(html: str) -> list[str]:
    slugs: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r'href="https://www\.safcodental\.com/product/([^"?#]+)"', html):
        slug = match.group(1).strip("/")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)
    return slugs


class _Clock:
    def __init__(self, delay: int) -> None:
        self.delay = delay
        self._last = 0.0

    def wait(self) -> None:
        if self._last:
            remaining = self.delay - (time.monotonic() - self._last)
            if remaining > 0:
                time.sleep(remaining)
        self._last = time.monotonic()


def _ssl_context() -> ssl.SSLContext:
    bundle = Path("/etc/ssl/cert.pem")
    if bundle.is_file():
        return ssl.create_default_context(cafile=str(bundle))
    return ssl.create_default_context()


def _get(url: str, clock: _Clock, rules: list[str]) -> str:
    clock.wait()
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain"})
    try:
        with urllib.request.urlopen(request, timeout=30, context=_ssl_context()) as response:
            final_url = response.geturl()
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"{url} returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach {url}") from exc
    if rules:
        _require_allowed(final_url, rules)
    return body
