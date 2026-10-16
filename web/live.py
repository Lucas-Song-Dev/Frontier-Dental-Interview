"""Fetch allowed Safco URLs. Listing ?page= and ?p= are never requested."""

from __future__ import annotations

import email.utils
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ORIGIN = "https://www.safcodental.com"
ROBOTS_URL = f"{ORIGIN}/robots.txt"
ATTEMPTS = 3
RETRY_CAP_SECONDS = 60
_PAGE_ACCEPT = "text/html,text/plain"
_PAGINATION = re.compile(r"[?&](page|p)=")


class FetchBlocked(Exception):
    """The URL was not requested. Robots is unloaded, or the URL is disallowed."""


class FetchFailed(RuntimeError):
    """A fetch failed. retryable is set for 429, 5xx, and timeouts."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


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


def _product_token(user_agent: str) -> str:
    token = user_agent.split("/", 1)[0].strip()
    if not token:
        return ""
    return token.split(None, 1)[0].lower()


class _Rule:
    """One Allow or Disallow path. An empty Disallow allows every path."""

    def __init__(self, path: str, allowance: bool) -> None:
        if path == "" and not allowance:
            allowance = True
        path = re.sub(r"[*]{2,}", "*", path)
        path = re.sub(r"[$][$*]+", "$", path)
        path = _normalize_pattern(path)
        fullmatch = path.endswith("$")
        path = path.rstrip("$")
        if "$" in path:
            raise ValueError("$ not at the end of path")
        self.fullmatch = fullmatch
        self.path = path
        self.allowance = allowance
        self.pattern = re.compile(_translate_pattern(path), re.DOTALL) if "*" in path else None

    def applies_to(self, filename: str) -> int:
        """Matching length plus one, or 0 when the path does not match."""
        if self.pattern is not None:
            matched = self.pattern.fullmatch(filename) if self.fullmatch else self.pattern.match(filename)
            if matched:
                return matched.end() + 1
            return 0
        if self.fullmatch:
            if filename == self.path:
                return len(self.path) + 1
            return 0
        if filename.startswith(self.path):
            return len(self.path) + 1
        return 0


def _normalize(path: str) -> str:
    unquoted = urllib.parse.unquote(path, errors="surrogateescape")
    return urllib.parse.quote(unquoted, errors="surrogateescape")


def _normalize_uri(path: str) -> str:
    path, sep, query = path.partition("?")
    path = _normalize(path)
    if sep:
        query = re.sub(r"[^=&]+", lambda match: _normalize(match[0]), query)
        path += "?" + query
    return path


def _normalize_pattern(path: str) -> str:
    path, sep, query = path.partition("?")
    path = re.sub(r"[^*$]+", lambda match: _normalize(match[0]), path)
    if sep:
        query = re.sub(r"[^=&*$]+", lambda match: _normalize(match[0]), query)
        path += "?" + query
    return path


def _translate_pattern(path: str) -> str:
    parts = [re.escape(part) for part in path.split("*")]
    for index in range(1, len(parts) - 1):
        parts[index] = f"(?:.*?{parts[index]})"
    parts[-1] = f".*{parts[-1]}"
    return "".join(parts)


def _request_path(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    path = urllib.parse.urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment))
    path = _normalize_uri(path)
    return path or "/"


def _parse_groups(body: str) -> dict[str, list[_Rule]]:
    """Group names are lowercase product tokens. Records are split by a new User-agent after a rule."""
    groups: dict[str, list[_Rule]] = {}
    agents: list[str] = []
    rules: list[_Rule] = []
    started = False

    def flush() -> None:
        nonlocal agents, rules, started
        if not agents:
            return
        for agent in agents:
            groups.setdefault(agent.lower(), []).extend(rules)
        agents = []
        rules = []
        started = False

    for raw in body.splitlines():
        hashed = raw.find("#")
        if hashed >= 0:
            raw = raw[:hashed]
        line = raw.strip()
        if not line or ":" not in line:
            continue
        field, value = line.split(":", 1)
        field = field.strip().lower()
        value = value.strip()
        if field == "user-agent":
            if started:
                flush()
            agents.append(value)
        elif field in {"allow", "disallow"} and agents:
            started = True
            try:
                rules.append(_Rule(value, field == "allow"))
            except ValueError:
                pass
        elif field in {"crawl-delay", "request-rate"} and agents:
            started = True
    flush()
    return groups


def _group_allows(rules: list[_Rule], url: str) -> bool:
    path = _request_path(url)
    if path == "/robots.txt":
        return True
    best = -1
    allowance = True
    for rule in rules:
        length = rule.applies_to(path)
        if not length:
            continue
        if length > best:
            best = length
            allowance = rule.allowance
        elif length == best and not allowance:
            allowance = rule.allowance
    return allowance


class RobotsPolicy:
    """A loaded robots.txt. Empty allow-all rules are still a loaded policy."""

    def __init__(self, groups: dict[str, list[_Rule]], user_agent: str) -> None:
        self._groups = groups
        self._user_agent = user_agent

    @classmethod
    def parse(cls, body: str, user_agent: str) -> RobotsPolicy:
        return cls(_parse_groups(body), user_agent)

    def allowed(self, url: str) -> bool:
        """True when every matching group allows the URL.

        The product token must equal the group name, so ``Catalog`` does not
        match ``CatalogDesk``. A specific group does not drop ``User-agent: *``.
        """
        token = _product_token(self._user_agent)
        names: list[str] = []
        if token in self._groups and token != "*":
            names.append(token)
        if "*" in self._groups:
            names.append("*")
        if not names:
            return True
        return all(_group_allows(self._groups[name], url) for name in names)


class RateLimiter:
    """The only sleeper. Delay 0 does not sleep. The 1 second floor stays in settings."""

    def __init__(self, delay: float, *, cap: float = RETRY_CAP_SECONDS) -> None:
        self.delay = delay
        self.cap = cap
        self._last = 0.0

    def wait(self, at_least: float | None = None) -> None:
        """Sleep the remainder of the delay. ``at_least`` replaces it when larger, and is capped."""
        gap = 0.0
        if self._last:
            gap = self.delay - (time.monotonic() - self._last)
            if gap < 0:
                gap = 0.0
        pause = gap
        if at_least is not None and at_least > pause:
            pause = self.cap if at_least > self.cap else at_least
        if pause > 0:
            time.sleep(pause)
        self._last = time.monotonic()


def _retry_after_seconds(value: str) -> float | None:
    """Seconds from a Retry-After header. None means the header did not parse."""
    text = value.strip()
    if not text:
        return None
    if text.lstrip("+-").isdigit():
        return float(text)
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    try:
        return parsed.timestamp() - datetime.now(UTC).timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _http_failure(url: str, exc: urllib.error.HTTPError) -> FetchFailed:
    retryable = exc.code == 429 or exc.code >= 500
    retry_after: float | None = None
    if exc.code in (429, 503) and exc.headers is not None:
        header = exc.headers.get("Retry-After")
        if header is not None:
            retry_after = _retry_after_seconds(str(header))
    return FetchFailed(
        f"{url} returned HTTP {exc.code}",
        retryable=retryable,
        retry_after=retry_after,
    )


class Fetcher:
    """Owns the limiter and the policy. Callers cannot pass an empty rule list."""

    def __init__(self, delay: float) -> None:
        self.limiter = RateLimiter(delay)
        self.policy: RobotsPolicy | None = None
        self.host = origin()
        self.robots_url = f"{self.host}/robots.txt"

    def fetch_robots(self, url: str | None = None) -> str:
        """Fetch this origin's robots.txt and store it. Any other URL is refused."""
        if url is not None and url != self.robots_url:
            raise FetchBlocked(f"Not a robots.txt URL: {url}")
        body, _content_type = self._request(self.robots_url, _PAGE_ACCEPT, gated=False)
        text = body.decode("utf-8", errors="replace")
        self.policy = RobotsPolicy.parse(text, user_agent())
        return text

    def get(self, url: str) -> str:
        body, _content_type = self._request(url, _PAGE_ACCEPT, gated=True)
        return body.decode("utf-8", errors="replace")

    def fetch_bytes(self, url: str) -> tuple[bytes, str]:
        """Fetch an allowed image. The same robots check and delay as a page fetch."""
        return self._request(url, "image/*,*/*", gated=True)

    def accepts(self, url: str) -> bool:
        try:
            self._check(url)
        except FetchBlocked:
            return False
        return True

    def sitemap_slugs(self) -> list[str]:
        sitemap_url = f"{self.host}/sitemap.xml"
        index = self.get(sitemap_url)
        maps = [loc for loc in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", index) if "product" in loc.lower()]
        slugs: list[str] = []
        for loc in maps:
            body = self.get(loc)
            for url in re.findall(r"<loc>\s*([^<]+?)\s*</loc>", body):
                if "?" in url or "/product/" not in url:
                    continue
                slug = url.split("/product/", 1)[1].strip("/")
                if slug:
                    slugs.append(slug)
        return slugs

    def _check(self, url: str) -> None:
        if self.policy is None:
            raise FetchBlocked(f"robots.txt is not loaded; refused {url}")
        if not self.policy.allowed(url):
            raise FetchBlocked(f"robots.txt disallows {url}")
        if _PAGINATION.search(url):
            raise FetchBlocked(f"Listing pagination is disallowed: {url}")

    def _request(self, url: str, accept: str, *, gated: bool) -> tuple[bytes, str]:
        if gated:
            self._check(url)
        last: FetchFailed | None = None
        at_least: float | None = None
        for attempt in range(ATTEMPTS):
            try:
                return self._once(url, accept, at_least, gated=gated)
            except FetchBlocked:
                raise
            except FetchFailed as exc:
                last = exc
                if not exc.retryable or attempt + 1 == ATTEMPTS:
                    raise
                at_least = exc.retry_after
        assert last is not None
        raise last

    def _once(
        self,
        url: str,
        accept: str,
        at_least: float | None,
        *,
        gated: bool,
    ) -> tuple[bytes, str]:
        self.limiter.wait(at_least)
        request = urllib.request.Request(url, headers={"User-Agent": user_agent(), "Accept": accept})
        try:
            with urllib.request.urlopen(request, timeout=30, context=_ssl_context()) as response:
                final_url = response.geturl()
                content_type = response.headers.get("Content-Type", "")
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise _http_failure(url, exc) from exc
        except urllib.error.URLError as exc:
            raise FetchFailed(f"Could not reach {url}", retryable=True) from exc
        if gated:
            self._check(final_url)
        return body, content_type


def _product_slugs(html: str) -> list[str]:
    """Listing product slugs. JSON-LD or the hit grid first, then other hrefs."""
    from extract import listing_cards

    slugs: list[str] = []
    seen: set[str] = set()
    for card in listing_cards(html):
        if not card.slug or card.slug in seen:
            continue
        seen.add(card.slug)
        slugs.append(card.slug)
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
