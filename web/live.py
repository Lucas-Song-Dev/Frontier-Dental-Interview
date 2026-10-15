"""Fetch allowed Safco URLs. Listing ?page= and ?p= are never requested."""

from __future__ import annotations

import re
import ssl
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

ORIGIN = "https://www.safcodental.com"
ROBOTS_URL = f"{ORIGIN}/robots.txt"
ATTEMPTS = 3


class FetchBlocked(Exception):
    """robots.txt disallows this URL, so it was not requested."""


class FetchFailed(RuntimeError):
    """A fetch failed. retryable is set for 429, 5xx, and timeouts."""

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def origin() -> str:
    from config import load_config

    return str(load_config()["origin"]).rstrip("/")


def user_agent() -> str:
    from config import load_config

    return str(load_config()["user_agent"])


def seeds() -> list[tuple[str, str]]:
    from config import load_config

    raw = load_config()["seeds"]
    assert isinstance(raw, dict)
    return [(str(key), str(url)) for key, url in raw.items()]


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


def _sitemap_slugs(clock: _Clock, rules: list[str]) -> list[str]:
    sitemap_url = f"{origin()}/sitemap.xml"
    _require_allowed(sitemap_url, rules)
    index = _get(sitemap_url, clock, rules)
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
    pattern = rf'href="{re.escape(origin())}/product/([^"?#]+)"'
    for match in re.finditer(pattern, html):
        slug = match.group(1).strip("/")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        slugs.append(slug)
    return slugs


def child_category_urls(html: str, seed_url: str) -> list[str]:
    """Category URLs one level under a seed. Query strings are excluded."""
    seed = seed_url.split("?", 1)[0].rstrip("/")
    pattern = rf'href="({re.escape(origin())}/catalog/[^"?#]+)"'
    found: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(pattern, html):
        href = match.group(1).rstrip("/")
        if href == seed or not href.startswith(seed + "/"):
            continue
        if href in seen:
            continue
        seen.add(href)
        found.append(href)
    return found


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


def _ca_bundle() -> Path | None:
    """System trust store: macOS cert.pem, then the Debian/Ubuntu bundle."""
    for candidate in (
        Path("/etc/ssl/cert.pem"),
        Path("/etc/ssl/certs/ca-certificates.crt"),
    ):
        if candidate.is_file():
            return candidate
    return None


def _ssl_context() -> ssl.SSLContext:
    bundle = _ca_bundle()
    if bundle is not None:
        return ssl.create_default_context(cafile=str(bundle))
    return ssl.create_default_context()


def _get(url: str, clock: _Clock, rules: list[str]) -> str:
    last: FetchFailed | None = None
    for attempt in range(ATTEMPTS):
        try:
            body, _content_type = _read(url, clock, rules, "text/html,text/plain")
            return body.decode("utf-8", errors="replace")
        except FetchBlocked:
            raise
        except FetchFailed as exc:
            last = exc
            if not exc.retryable or attempt + 1 == ATTEMPTS:
                raise
    assert last is not None
    raise last


def fetch_bytes(url: str, clock: _Clock, rules: list[str]) -> tuple[bytes, str]:
    """Fetch an allowed image. The same robots check and delay as a page fetch."""
    return _read(url, clock, rules, "image/*,*/*")


def _read(url: str, clock: _Clock, rules: list[str], accept: str) -> tuple[bytes, str]:
    if rules:
        _require_allowed(url, rules)
    clock.wait()
    request = urllib.request.Request(url, headers={"User-Agent": user_agent(), "Accept": accept})
    try:
        with urllib.request.urlopen(request, timeout=30, context=_ssl_context()) as response:
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type", "")
            body = response.read()
    except urllib.error.HTTPError as exc:
        retryable = exc.code == 429 or exc.code >= 500
        raise FetchFailed(f"{url} returned HTTP {exc.code}", retryable=retryable) from exc
    except urllib.error.URLError as exc:
        raise FetchFailed(f"Could not reach {url}", retryable=True) from exc
    if rules:
        _require_allowed(final_url, rules)
    return body, content_type
