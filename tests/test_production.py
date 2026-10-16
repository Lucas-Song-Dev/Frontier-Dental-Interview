"""Delay, config, secrets, idempotent reload, and a skipped fetch. No live host."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import copy
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

import config  # noqa: E402
import crawl  # noqa: E402
import db  # noqa: E402
import live  # noqa: E402
import server  # noqa: E402

HOST = "https://www.safcodental.com"
ROBOTS_FIXTURE = Path(__file__).resolve().parents[1] / "docs" / "recon" / "robots_2026-10-15.txt"


@contextmanager
def _patched_fetcher(fake: Callable[[str], str]) -> Iterator[None]:
    def get(self: live.Fetcher, url: str) -> str:
        return fake(url)

    def fetch_robots(self: live.Fetcher, url: str | None = None) -> str:
        target = f"{live.origin()}/robots.txt"
        if url is not None and url != target:
            raise live.FetchBlocked(url)
        body = fake(target)
        self.policy = live.RobotsPolicy.parse(body, live.user_agent())
        return body

    with (
        mock.patch.object(live.Fetcher, "get", get),
        mock.patch.object(live.Fetcher, "fetch_robots", fetch_robots),
    ):
        yield


def _loaded_fetcher(delay: float, body: str) -> live.Fetcher:
    fetcher = live.Fetcher(delay)
    fetcher.policy = live.RobotsPolicy.parse(body, live.user_agent())
    return fetcher


class _Headers:
    def __init__(self, retry_after: str | None = None) -> None:
        self.retry_after = retry_after

    def get(self, key: str, default: str | None = None) -> str | None:
        if key.lower() == "retry-after":
            return self.retry_after
        return default


class _Response:
    def __init__(self, url: str, body: bytes = b"<html>ok</html>") -> None:
        self.headers = _Headers()
        self._url = url
        self._body = body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def geturl(self) -> str:
        return self._url

    def read(self) -> bytes:
        return self._body


def _http_error(url: str, code: int, retry_after: str | None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "error", _Headers(retry_after), None)


class ClockTests(unittest.TestCase):
    def test_limiter_sleeps_the_remainder_of_the_delay(self) -> None:
        limiter = live.RateLimiter(2)
        moments = iter((100.0, 100.5, 100.5))
        slept: list[float] = []
        with mock.patch.object(live.time, "monotonic", side_effect=lambda: next(moments)):
            with mock.patch.object(live.time, "sleep", side_effect=slept.append):
                limiter.wait()
                limiter.wait()
        self.assertEqual(slept, [1.5])

    def test_zero_delay_does_not_sleep(self) -> None:
        limiter = live.RateLimiter(0)
        slept: list[float] = []
        with mock.patch.object(live.time, "sleep", side_effect=slept.append):
            limiter.wait()
            limiter.wait()
        self.assertEqual(slept, [])


class ConfigTests(unittest.TestCase):
    def test_missing_file_keeps_the_assignment_seeds(self) -> None:
        with mock.patch.object(config, "CONFIG_PATH", Path("/no/such/config.yaml")):
            data = config.load_config()
        self.assertEqual(
            data["seeds"]["sutures"],
            "https://www.safcodental.com/catalog/sutures-surgical-products",
        )
        self.assertEqual(data["seeds"]["gloves"], "https://www.safcodental.com/catalog/gloves")

    def test_delay_override_does_not_keep_the_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("min_delay_seconds: 3\n", encoding="utf-8")
            with mock.patch.object(config, "CONFIG_PATH", path):
                with mock.patch.dict(os.environ, {"MIN_DELAY_SECONDS": "9", "CLAUDE_API_KEY": "secret-value"}):
                    data = config.load_config()
                    self.assertEqual(data["min_delay_seconds"], 9)
                    self.assertNotIn("secret-value", json.dumps(data))
                    self.assertTrue(config.claude_key_is_set())
                with mock.patch.dict(os.environ, {"CLAUDE_API_KEY": ""}):
                    self.assertFalse(config.claude_key_is_set())


class StageMigrationTests(unittest.TestCase):
    def test_legacy_agent_column_is_copied_into_stage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(db, "DB_PATH", Path(directory) / "catalog.db"):
                connection = db.connect()
                try:
                    connection.execute(
                        """
                        CREATE TABLE agent_steps (
                            position INTEGER PRIMARY KEY,
                            agent TEXT NOT NULL,
                            subject TEXT NOT NULL,
                            outcome TEXT NOT NULL,
                            detail TEXT NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        "INSERT INTO agent_steps VALUES (0, 'navigator', 'https://example', 'allowed', 'kept')"
                    )
                    connection.execute(
                        """
                        CREATE TABLE checkpoints (
                            url TEXT PRIMARY KEY,
                            status TEXT NOT NULL,
                            agent TEXT NOT NULL,
                            detail TEXT NOT NULL DEFAULT '',
                            updated_at TEXT NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        "INSERT INTO checkpoints VALUES ('https://example/product/a', 'extracted', 'validator', '', 't')"
                    )
                    connection.commit()
                    db._copy_stage_column(connection, "agent_steps")
                    db._copy_stage_column(connection, "checkpoints")
                    connection.commit()
                    step = connection.execute("SELECT stage FROM agent_steps").fetchone()
                    checkpoint = connection.execute("SELECT stage FROM checkpoints").fetchone()
                    self.assertEqual(step["stage"], "navigator")
                    self.assertEqual(checkpoint["stage"], "validator")
                    self.assertNotIn("agent", db._column_names(connection, "agent_steps"))
                    self.assertNotIn("agent", db._column_names(connection, "checkpoints"))
                finally:
                    connection.close()


class SettingsTests(unittest.TestCase):
    def test_delay_below_one_second_is_rejected(self) -> None:
        incoming = {
            "crawl_interval_minutes": "360",
            "min_delay_seconds": "0",
            "max_concurrency": "1",
            "max_products": "40",
            "max_pages_per_category": "5",
        }
        with self.assertRaises(ValueError):
            db.save_settings(incoming)


class ClearTests(unittest.TestCase):
    def test_clear_scraped_keeps_settings_jobs_and_category_names(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(db, "DB_PATH", Path(directory) / "catalog.db"):
                db.ensure_database()
                db.reload_catalog(server.CATALOG)
                db.start_job("live_fetch")
                before = db.get_settings()
                connection = db.connect()
                try:
                    titles = {
                        row["key"]: row["title"]
                        for row in connection.execute("SELECT key, title FROM categories")
                    }
                finally:
                    connection.close()
                db.clear_scraped()
                connection = db.connect()
                try:
                    products = connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
                    cards = connection.execute("SELECT COUNT(*) FROM listing_cards").fetchone()[0]
                    steps = connection.execute("SELECT COUNT(*) FROM agent_steps").fetchone()[0]
                    checkpoints = connection.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
                    jobs = connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
                    kept = {
                        row["key"]: row["title"]
                        for row in connection.execute("SELECT key, title, stats FROM categories")
                    }
                    stats = [
                        row["stats"]
                        for row in connection.execute("SELECT stats FROM categories")
                    ]
                finally:
                    connection.close()
                self.assertEqual(products, 0)
                self.assertEqual(cards, 0)
                self.assertEqual(steps, 0)
                self.assertEqual(checkpoints, 0)
                self.assertEqual(jobs, 1)
                self.assertEqual(kept, titles)
                self.assertEqual(stats, [""] * len(titles))
                self.assertEqual(db.get_settings(), before)

    def test_clear_is_refused_while_a_fetch_is_running(self) -> None:
        server._running_job_id = 9
        try:
            with self.assertRaises(RuntimeError):
                server.clear_scraped_catalog()
        finally:
            server._running_job_id = None


class ReloadTests(unittest.TestCase):
    def test_reload_is_idempotent_and_checkpoints_upsert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(db, "DB_PATH", Path(directory) / "catalog.db"):
                db.ensure_database()
                db.reload_catalog(server.CATALOG)
                db.reload_catalog(server.CATALOG)
                connection = db.connect()
                try:
                    count = connection.execute("SELECT COUNT(*) FROM variants").fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual(count, sum(len(product.variants) for product in server.CATALOG.products.values()))

                catalog = copy(server.CATALOG)
                first = dict(server.CATALOG.checkpoints[0])
                catalog.checkpoints = [first, {**first, "status": "skipped", "detail": "again"}]
                db.reload_catalog(catalog)
                connection = db.connect()
                try:
                    rows = connection.execute(
                        "SELECT status, detail FROM checkpoints WHERE url = ?",
                        (first["url"],),
                    ).fetchall()
                finally:
                    connection.close()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["status"], "skipped")
                self.assertEqual(rows[0]["detail"], "again")

    def test_crawl_upsert_keeps_earlier_parents(self) -> None:
        lavender = server.CATALOG.products["lavender-nitrile"]
        surgifoam = server.CATALOG.products["surgifoam-reg"]
        perma = server.CATALOG.products["perma-sharp-reg-sutures"]
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(db, "DB_PATH", Path(directory) / "catalog.db"):
                db.ensure_database()
                db.upsert_crawl(_batch(server.CATALOG.categories, {"lavender-nitrile": lavender}))
                db.upsert_crawl(_batch(server.CATALOG.categories, {"surgifoam-reg": surgifoam}))
                connection = db.connect()
                try:
                    slugs = {
                        row[0]
                        for row in connection.execute("SELECT slug FROM products").fetchall()
                    }
                finally:
                    connection.close()
                self.assertEqual(slugs, {"lavender-nitrile", "surgifoam-reg"})

                one = server.Variant(
                    sku=perma.variants[0].sku,
                    name=perma.variants[0].name,
                    group=perma.variants[0].group,
                    mfr=perma.variants[0].mfr,
                    stock=perma.variants[0].stock,
                    price=perma.variants[0].price,
                    pack_size=perma.variants[0].pack_size,
                    specification=perma.variants[0].specification,
                    attributes=perma.variants[0].attributes,
                )
                narrowed = server.Product(
                    slug=perma.slug,
                    name=perma.name,
                    sku=perma.sku,
                    price=perma.price,
                    availability=perma.availability,
                    category=perma.category,
                    variants=[one],
                    brand=perma.brand,
                    url=perma.url,
                    description=perma.description,
                    category_path=perma.category_path,
                    image_urls=perma.image_urls,
                    alternatives=perma.alternatives,
                    attributes=perma.attributes,
                )
                db.upsert_crawl(_batch(server.CATALOG.categories, {perma.slug: narrowed}))
                db.upsert_crawl(_batch({}, {}))
                connection = db.connect()
                try:
                    parents = {
                        row[0]
                        for row in connection.execute("SELECT slug FROM products").fetchall()
                    }
                    children = connection.execute(
                        "SELECT COUNT(*) FROM variants WHERE product_slug = ?",
                        (perma.slug,),
                    ).fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual(parents, {"lavender-nitrile", "surgifoam-reg", perma.slug})
                self.assertEqual(children, 1)

    def test_card_list_replaces_stored_cards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(db, "DB_PATH", Path(directory) / "catalog.db"):
                db.ensure_database()
                db.upsert_crawl(_batch({"gloves": _cards(("old-card", "Old", False))}, {}))
                db.upsert_crawl(_batch({"gloves": _cards(("new-card", "New", False))}, {}))
                connection = db.connect()
                try:
                    slugs = [
                        row["slug"]
                        for row in connection.execute(
                            "SELECT slug FROM listing_cards WHERE category_key = 'gloves'"
                        )
                    ]
                finally:
                    connection.close()
                self.assertEqual(slugs, ["new-card"])

    def test_empty_card_list_leaves_stored_cards(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(db, "DB_PATH", Path(directory) / "catalog.db"):
                db.ensure_database()
                db.upsert_crawl(_batch({"gloves": _cards(("kept-card", "Kept", False))}, {}))
                db.upsert_crawl(_batch({"gloves": _cards()}, {}))
                connection = db.connect()
                try:
                    slugs = [
                        row["slug"]
                        for row in connection.execute(
                            "SELECT slug FROM listing_cards WHERE category_key = 'gloves'"
                        )
                    ]
                finally:
                    connection.close()
                self.assertEqual(slugs, ["kept-card"])

    def test_stored_parent_marks_the_card_saved(self) -> None:
        lavender = server.CATALOG.products["lavender-nitrile"]
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(db, "DB_PATH", Path(directory) / "catalog.db"):
                db.ensure_database()
                db.upsert_crawl(_batch({"gloves": _cards()}, {"lavender-nitrile": lavender}))
                db.upsert_crawl(
                    _batch(
                        {
                            "gloves": _cards(
                                ("lavender-nitrile", "Lavender Nitrile", False),
                                ("not-stored", "Other", False),
                            )
                        },
                        {},
                    )
                )
                loaded = server.load_catalog()
                assert loaded is not None
                saved = {card.slug: card.saved for card in loaded.categories["gloves"].cards}
                self.assertEqual(saved, {"lavender-nitrile": True, "not-stored": False})


class ImageTests(unittest.TestCase):
    def test_saved_image_keeps_the_public_url(self) -> None:
        from sheets import image_extension, remember_image

        self.assertEqual(image_extension("image/jpeg", "https://www.safcodental.com/media/a.jpg?width=265"), ".jpg")
        self.assertEqual(image_extension("image/png", "https://www.safcodental.com/media/a.jpg"), ".png")
        stored = json.loads(remember_image('["https://www.safcodental.com/media/a.jpg"]', "/images/a.jpg"))
        self.assertEqual(stored[0], "/images/a.jpg")
        self.assertEqual(stored[1], "https://www.safcodental.com/media/a.jpg")
        from sheets import _listing_images

        gloves = (Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "listing_gloves.html").read_text(
            encoding="utf-8"
        )
        images = _listing_images(gloves)
        self.assertEqual(len(images), 12)
        self.assertIn(".jpg", images["lavender-nitrile"])
        self.assertTrue(images["lavender-nitrile"].startswith("https://www.safcodental.com/media/"))

    def test_image_pagination_url_is_not_fetched(self) -> None:
        fetcher = live.Fetcher(0)
        fetcher.policy = live.RobotsPolicy.parse(
            "User-agent: *\nDisallow: /*?page=\nDisallow: /*?p=\n",
            live.user_agent(),
        )

        def urlopen(request: object, timeout: int, context: object) -> object:
            raise AssertionError(getattr(request, "full_url", "requested"))

        with mock.patch.object(live.urllib.request, "urlopen", urlopen):
            with mock.patch.object(live.time, "sleep", side_effect=AssertionError("slept")):
                with self.assertRaises(live.FetchBlocked):
                    fetcher.fetch_bytes("https://www.safcodental.com/media/catalog/product/a.jpg?page=2")


class RefreshDispatchTests(unittest.TestCase):
    def test_refresh_shows_progress_before_it_finishes(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def fake_crawl(on_progress: object = None, on_product: object = None) -> tuple[str, dict[str, object]]:
            if callable(on_progress):
                on_progress("Fetching https://www.safcodental.com/catalog/gloves")
            started.set()
            self.assertTrue(release.wait(2))
            raise RuntimeError("stopped")

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(db, "DB_PATH", Path(directory) / "catalog.db"):
                db.ensure_database()
                server._running_job_id = None
                try:
                    with mock.patch.object(server, "crawl", side_effect=fake_crawl):
                        job_id = server.begin_refresh()
                        self.assertTrue(started.wait(2))
                        running = db.list_jobs()[0]
                        self.assertEqual(running["id"], job_id)
                        self.assertEqual(running["status"], "running")
                        self.assertIn("catalog/gloves", running["message"])
                        self.assertEqual(server.begin_refresh(), job_id)
                        release.set()
                        deadline = time.time() + 2
                        status = "running"
                        while time.time() < deadline:
                            status = db.list_jobs()[0]["status"]
                            if status != "running":
                                break
                            time.sleep(0.02)
                        self.assertEqual(status, "failed")
                        self.assertIn("stopped", db.list_jobs()[0]["message"])
                finally:
                    release.set()
                    server._running_job_id = None


class FetchTests(unittest.TestCase):
    def test_one_failed_product_does_not_drop_the_next(self) -> None:
        def fake_get(url: str) -> str:
            if url.endswith("/robots.txt"):
                return "User-agent: *\n"
            if url.endswith("/sitemap.xml"):
                return "<urlset><loc>https://www.safcodental.com/products.xml</loc></urlset>"
            if url.endswith("/products.xml"):
                return (
                    "<urlset><loc>https://www.safcodental.com/product/bad</loc>"
                    "<loc>https://www.safcodental.com/product/good</loc></urlset>"
                )
            if "/catalog/" in url:
                return "<h1>Listing</h1>"
            if url.endswith("/product/bad"):
                raise live.FetchFailed(f"Could not reach {url}", retryable=False)
            if url.endswith("/product/good"):
                return "<html>ok</html>"
            raise AssertionError(url)

        settings = {
            "min_delay_seconds": "0",
            "max_products": "40",
            "max_pages_per_category": "5",
        }
        with mock.patch.object(crawl, "get_settings", return_value=settings):
            with mock.patch.object(crawl, "terminal_urls", return_value=set()):
                with _patched_fetcher(fake_get):
                    _robots, pages = crawl.crawl()
        slugs = [item[0] for item in pages["products"]]
        self.assertEqual(slugs, ["good"])
        failed = pages["failed"]
        assert isinstance(failed, list)
        self.assertEqual(failed[0]["url"], "https://www.safcodental.com/product/bad")

    def test_timeout_retries_then_returns_the_body(self) -> None:
        calls = {"n": 0}

        class _Headers:
            def get(self, _key: str, default: str = "") -> str:
                return default

        class _Response:
            headers = _Headers()

            def __enter__(self) -> _Response:
                return self

            def __exit__(self, *_args: object) -> bool:
                return False

            def geturl(self) -> str:
                return "https://www.safcodental.com/product/widget"

            def read(self) -> bytes:
                return b"<html>ok</html>"

        def urlopen(request: object, timeout: int, context: object) -> _Response:
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.URLError(TimeoutError("timed out"))
            return _Response()

        slept: list[float] = []
        fetcher = _loaded_fetcher(3, "User-agent: *\nDisallow:\n")
        with mock.patch.object(live.urllib.request, "urlopen", urlopen):
            with mock.patch.object(live.time, "monotonic", return_value=100.0):
                with mock.patch.object(live.time, "sleep", side_effect=slept.append):
                    body = fetcher.get("https://www.safcodental.com/product/widget")
        self.assertEqual(body, "<html>ok</html>")
        self.assertEqual(calls["n"], 3)
        self.assertEqual(slept, [3.0, 3.0])

    def test_robots_disallow_is_not_retried(self) -> None:
        fetcher = _loaded_fetcher(5, "User-agent: *\nDisallow: /*?page=\n")
        calls: list[str] = []

        def urlopen(request: object, timeout: int, context: object) -> object:
            calls.append(getattr(request, "full_url", ""))
            raise AssertionError("requested")

        with mock.patch.object(live.urllib.request, "urlopen", urlopen):
            with mock.patch.object(live.time, "sleep", side_effect=AssertionError("slept")):
                with self.assertRaises(live.FetchBlocked):
                    fetcher.get("https://www.safcodental.com/catalog/gloves?page=2")
        self.assertEqual(calls, [])

    def test_crawl_skips_extracted_and_out_of_scope_and_retries_unresolved(self) -> None:
        requested: list[str] = []

        def fake_get(url: str) -> str:
            requested.append(url)
            if url.endswith("/robots.txt"):
                return "User-agent: *\n"
            if url.endswith("/sitemap.xml"):
                return "<urlset><loc>https://www.safcodental.com/products.xml</loc></urlset>"
            if url.endswith("/products.xml"):
                locs = "".join(
                    f"<loc>https://www.safcodental.com/product/{slug}</loc>"
                    for slug in ("a", "b", "c", "d", "e")
                )
                return f"<urlset>{locs}</urlset>"
            if "/catalog/" in url:
                return "<h1>Listing</h1>"
            if url.endswith("/product/d"):
                return (
                    '<nav class="breadcrumbs"><a>Home</a>'
                    "<a>Dental Exam Gloves</a></nav>"
                    '<script type="application/ld+json">'
                    '{"@type":"Product","name":"Widget","sku":"PARENT",'
                    '"offers":{"price":"1.00","availability":"https://schema.org/InStock"}}'
                    "</script>"
                )
            if url.endswith("/product/e"):
                return "<html>no markers</html>"
            raise AssertionError(url)

        settings = {
            "min_delay_seconds": "0",
            "max_products": "40",
            "max_pages_per_category": "5",
        }
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(db, "DB_PATH", Path(directory) / "catalog.db"):
                db.ensure_database()
                connection = db.connect()
                try:
                    connection.executemany(
                        """
                        INSERT INTO checkpoints (url, status, stage, detail, updated_at)
                        VALUES (?, ?, 'validator', '', '2026-01-01T00:00:00+00:00')
                        """,
                        [
                            ("https://www.safcodental.com/product/a", "out_of_scope"),
                            ("https://www.safcodental.com/product/b", "extracted"),
                            ("https://www.safcodental.com/product/c", "out_of_scope"),
                            ("https://www.safcodental.com/product/e", "unresolved"),
                        ],
                    )
                    connection.commit()
                finally:
                    connection.close()
                with mock.patch.object(crawl, "get_settings", return_value=settings):
                    with _patched_fetcher(fake_get):
                        _robots, pages = crawl.crawl()
        product_urls = [url for url in requested if "/product/" in url]
        self.assertEqual(
            product_urls,
            [
                "https://www.safcodental.com/product/d",
                "https://www.safcodental.com/product/e",
            ],
        )
        slugs = [item[0] for item in pages["products"]]
        self.assertEqual(slugs, ["d", "e"])

    def test_subcategory_products_are_queued_before_the_sitemap(self) -> None:
        seed = "https://www.safcodental.com/catalog/sutures-surgical-products"
        child = seed + "/sutures"
        listing = (
            f'<a href="{child}">Sutures</a>'
            '<a href="https://www.safcodental.com/catalog/acrylics-and-reline">Reline</a>'
            f'<a href="{seed}?page=2">Next</a>'
            '<a href="https://www.safcodental.com/product/from-seed">Seed</a>'
        )
        child_html = '<a href="https://www.safcodental.com/product/from-child">Child</a>'
        requested: list[str] = []

        def fake_get(url: str) -> str:
            requested.append(url)
            if url.endswith("/robots.txt"):
                return "User-agent: *\n"
            if url.endswith("/sitemap.xml"):
                return "<urlset><loc>https://www.safcodental.com/products.xml</loc></urlset>"
            if url.endswith("/products.xml"):
                return "<urlset><loc>https://www.safcodental.com/product/from-sitemap</loc></urlset>"
            if url == child:
                return child_html
            if "/catalog/" in url:
                return listing
            if "/product/" in url:
                return "<html>ok</html>"
            raise AssertionError(url)

        settings = {
            "min_delay_seconds": "0",
            "max_products": "40",
            "max_pages_per_category": "5",
        }
        with mock.patch.object(live, "seeds", return_value=[("sutures", seed)]):
            with mock.patch.object(crawl, "get_settings", return_value=settings):
                with mock.patch.object(crawl, "terminal_urls", return_value=set()):
                    with _patched_fetcher(fake_get):
                        crawl.crawl()
        self.assertEqual(requested.count(child), 1)
        self.assertFalse(any("acrylics-and-reline" in url for url in requested))
        self.assertFalse(any("page=" in url for url in requested))
        product_urls = [url for url in requested if "/product/" in url]
        self.assertEqual(
            product_urls,
            [
                "https://www.safcodental.com/product/from-seed",
                "https://www.safcodental.com/product/from-child",
                "https://www.safcodental.com/product/from-sitemap",
            ],
        )

    def test_json_ld_products_are_queued_before_the_sitemap(self) -> None:
        seed = "https://www.safcodental.com/catalog/sutures-surgical-products"
        child = seed + "/sutures"
        collection = seed + "/not-a-product"
        listing = (
            f'<a href="{child}">Sutures</a>'
            f'<a href="{seed}?page=2">Next</a>'
            '<a href="https://www.safcodental.com/product/clearance-item">Clearance</a>'
            + _product_list("from-json", collection)
        )
        child_html = _product_list("from-child", "")
        requested: list[str] = []

        def fake_get(url: str) -> str:
            requested.append(url)
            if url.endswith("/robots.txt"):
                return "User-agent: *\n"
            if url.endswith("/sitemap.xml"):
                return "<urlset><loc>https://www.safcodental.com/products.xml</loc></urlset>"
            if url.endswith("/products.xml"):
                return "<urlset><loc>https://www.safcodental.com/product/from-sitemap</loc></urlset>"
            if url == child:
                return child_html
            if "/catalog/" in url:
                return listing
            if "/product/" in url:
                return "<html>ok</html>"
            raise AssertionError(url)

        settings = {
            "min_delay_seconds": "0",
            "max_products": "40",
            "max_pages_per_category": "5",
        }
        with mock.patch.object(live, "seeds", return_value=[("sutures", seed)]):
            with mock.patch.object(crawl, "get_settings", return_value=settings):
                with mock.patch.object(crawl, "terminal_urls", return_value=set()):
                    with _patched_fetcher(fake_get):
                        crawl.crawl()
        self.assertFalse(any("page=" in url for url in requested))
        self.assertNotIn(collection, requested)
        product_urls = [url for url in requested if "/product/" in url]
        self.assertEqual(
            product_urls,
            [
                "https://www.safcodental.com/product/from-json",
                "https://www.safcodental.com/product/clearance-item",
                "https://www.safcodental.com/product/from-child",
                "https://www.safcodental.com/product/from-sitemap",
            ],
        )

    def test_child_category_products_are_fetched_past_the_sitemap_cap(self) -> None:
        seed = "https://www.safcodental.com/catalog/gloves"
        child = seed + "/nitrile-gloves"
        photo = "https://www.safcodental.com/media/catalog/product/d/r/drcas.jpg"
        listing = (
            f'<a href="{child}">Nitrile</a>'
            + _product_list("from-seed", "")
        )
        child_html = _product_list("from-child", "").replace(
            '"brand": "Acme"',
            f'"brand": "Acme", "image": ["{photo}"]',
        )
        requested: list[str] = []

        def fake_get(url: str) -> str:
            requested.append(url)
            if url.endswith("/robots.txt"):
                return "User-agent: *\n"
            if url.endswith("/sitemap.xml"):
                return "<urlset><loc>https://www.safcodental.com/products.xml</loc></urlset>"
            if url.endswith("/products.xml"):
                return (
                    "<urlset>"
                    "<loc>https://www.safcodental.com/product/from-sitemap</loc>"
                    "<loc>https://www.safcodental.com/product/second-sitemap</loc>"
                    "</urlset>"
                )
            if url == child:
                return child_html
            if "/catalog/" in url:
                return listing
            if "/product/" in url:
                return "<html>ok</html>"
            raise AssertionError(url)

        settings = {
            "min_delay_seconds": "0",
            "max_products": "40",
            "max_pages_per_category": "1",
        }
        with mock.patch.object(live, "seeds", return_value=[("gloves", seed)]):
            with mock.patch.object(crawl, "get_settings", return_value=settings):
                with mock.patch.object(crawl, "terminal_urls", return_value=set()):
                    with _patched_fetcher(fake_get):
                        _robots, pages = crawl.crawl()
        product_urls = [url for url in requested if "/product/" in url]
        self.assertEqual(
            product_urls,
            [
                "https://www.safcodental.com/product/from-seed",
                "https://www.safcodental.com/product/from-child",
                "https://www.safcodental.com/product/from-sitemap",
            ],
        )
        self.assertNotIn("https://www.safcodental.com/product/second-sitemap", product_urls)
        catalog = server.catalog_from_live("", pages)
        cards = catalog.categories["gloves"].cards
        self.assertEqual([card.slug for card in cards], ["from-seed", "from-child"])
        self.assertEqual(cards[1].image, photo)

    def test_out_of_scope_checkpoint_removes_the_stored_parent(self) -> None:
        url = "https://www.safcodental.com/product/clearance-item"
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(db, "DB_PATH", Path(directory) / "catalog.db"):
                db.ensure_database()
                connection = db.connect()
                connection.execute(
                    "INSERT INTO categories (key, title, stats) VALUES ('sutures', 'Sutures', '')"
                )
                connection.execute(
                    """
                    INSERT INTO products (
                        slug, category_key, name, parent_sku, availability, url, category_path
                    ) VALUES (
                        'clearance-item', 'sutures', 'Clearance Item', 'C', 'In stock', ?, ?
                    )
                    """,
                    (url, "Clearance / Safco Clearance"),
                )
                connection.execute(
                    """
                    INSERT INTO variants (sku, product_slug, name, item_group, mfr_number, stock)
                    VALUES ('C1', 'clearance-item', 'Child', '', '', 'In stock')
                    """
                )
                connection.commit()
                connection.close()
                catalog = server.Catalog(
                    categories={},
                    products={},
                    robots="",
                    checkpoints=[
                        {
                            "url": url,
                            "status": "out_of_scope",
                            "stage": "validator",
                            "detail": "Clearance / Safco Clearance",
                        }
                    ],
                )
                db.upsert_crawl(catalog)
                self.assertEqual(db.list_catalog_rows(), [])
                self.assertIn(url, db.terminal_urls())

    def test_empty_database_does_not_publish_the_sample(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(db, "DB_PATH", Path(directory) / "catalog.db"):
                with mock.patch.object(server, "publish", side_effect=AssertionError("published")):
                    loaded = server.open_catalog()
        self.assertIn("sutures", loaded.categories)

    def test_missing_ui_does_not_write_the_sample(self) -> None:
        import export_catalog

        sample = export_catalog.SAMPLE / "catalog.json"
        before = sample.read_bytes()
        handler = server.Handler.__new__(server.Handler)

        def _html(status: int, body: str) -> None:
            handler.status = status  # type: ignore[attr-defined]
            handler.body = body  # type: ignore[attr-defined]

        handler._html = _html  # type: ignore[method-assign]
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(server, "DIST", Path(directory)):
                server.Handler._spa(handler, "/")
        self.assertEqual(handler.status, 503)  # type: ignore[attr-defined]
        self.assertEqual(sample.read_bytes(), before)


class GateTests(unittest.TestCase):
    def test_catalog_fetch_before_robots_is_not_requested(self) -> None:
        fetcher = live.Fetcher(0)
        calls: list[str] = []

        def urlopen(request: object, timeout: int, context: object) -> object:
            calls.append(getattr(request, "full_url", ""))
            raise AssertionError("requested")

        with mock.patch.object(live.urllib.request, "urlopen", urlopen):
            with self.assertRaises(live.FetchBlocked):
                fetcher.get(f"{HOST}/catalog/gloves")
            with self.assertRaises(live.FetchBlocked):
                fetcher.fetch_bytes(f"{HOST}/media/a.jpg")
        self.assertEqual(calls, [])

    def test_fetch_robots_refuses_a_catalog_url(self) -> None:
        fetcher = live.Fetcher(0)

        def urlopen(request: object, timeout: int, context: object) -> object:
            raise AssertionError(getattr(request, "full_url", "requested"))

        with mock.patch.object(live.urllib.request, "urlopen", urlopen):
            with self.assertRaises(live.FetchBlocked):
                fetcher.fetch_robots(f"{HOST}/catalog/gloves")
        self.assertIsNone(fetcher.policy)

    def test_failed_robots_fetch_does_not_load_an_allow_all_policy(self) -> None:
        fetcher = live.Fetcher(0)
        calls: list[str] = []

        def urlopen(request: object, timeout: int, context: object) -> object:
            calls.append(getattr(request, "full_url", ""))
            raise urllib.error.URLError("down")

        with mock.patch.object(live.urllib.request, "urlopen", urlopen):
            with self.assertRaises(live.FetchFailed):
                fetcher.fetch_robots()
            with self.assertRaises(live.FetchBlocked):
                fetcher.get(f"{HOST}/catalog/gloves")
        self.assertIsNone(fetcher.policy)
        self.assertEqual(calls, [fetcher.robots_url] * live.ATTEMPTS)

    def test_allow_all_robots_still_blocks_pagination(self) -> None:
        fetcher = live.Fetcher(0)
        calls: list[str] = []
        gloves = f"{HOST}/catalog/gloves"

        def urlopen(request: object, timeout: int, context: object) -> _Response:
            url = getattr(request, "full_url", "")
            calls.append(url)
            if url == fetcher.robots_url:
                return _Response(url, b"User-agent: *\nDisallow:\n")
            return _Response(url, b"<html>ok</html>")

        with mock.patch.object(live.urllib.request, "urlopen", urlopen):
            fetcher.fetch_robots()
            assert fetcher.policy is not None
            self.assertTrue(fetcher.policy.allowed(f"{gloves}?page=2"))
            body = fetcher.get(gloves)
            with self.assertRaises(live.FetchBlocked):
                fetcher.get(f"{gloves}?page=2")
        self.assertEqual(body, "<html>ok</html>")
        self.assertEqual(calls, [fetcher.robots_url, gloves])

    def test_saved_robots_file_matches_the_recon_table(self) -> None:
        policy = live.RobotsPolicy.parse(ROBOTS_FIXTURE.read_text(encoding="utf-8"), live.user_agent())
        allowed = [
            f"{HOST}/catalog/sutures-surgical-products",
            f"{HOST}/catalog/gloves",
            f"{HOST}/catalog/gloves/nitrile-gloves",
            f"{HOST}/catalog/sutures-surgical-products/sutures",
            f"{HOST}/product/perma-sharp-reg-sutures",
            f"{HOST}/sitemap.xml",
            f"{HOST}/media/a.jpg?width=265",
        ]
        blocked = [
            f"{HOST}/checkout/",
            f"{HOST}/catalog/gloves?page=2",
            f"{HOST}/catalog/gloves?p=2",
            f"{HOST}/foo.php",
            f"{HOST}/catalog/product/view/id/1",
            f"{HOST}/catalog/category/view/id/1",
            f"{HOST}/catalogsearch/",
            f"{HOST}/media/a.jpg?page=2",
        ]
        for url in allowed:
            self.assertTrue(policy.allowed(url), url)
        for url in blocked:
            self.assertFalse(policy.allowed(url), url)
        fetcher = live.Fetcher(0)
        fetcher.policy = policy
        calls: list[str] = []

        def urlopen(request: object, timeout: int, context: object) -> _Response:
            url = getattr(request, "full_url", "")
            calls.append(url)
            return _Response(url)

        with mock.patch.object(live.urllib.request, "urlopen", urlopen):
            fetcher.get(allowed[0])
            fetcher.get(allowed[4])
            for url in blocked:
                with self.assertRaises(live.FetchBlocked):
                    fetcher.get(url)
        self.assertEqual(calls, [allowed[0], allowed[4]])

    def test_allow_beats_a_shorter_disallow(self) -> None:
        policy = live.RobotsPolicy.parse(
            "User-agent: *\nDisallow: /catalog/\nAllow: /catalog/gloves\n",
            live.user_agent(),
        )
        self.assertTrue(policy.allowed(f"{HOST}/catalog/gloves"))
        self.assertTrue(policy.allowed(f"{HOST}/catalog/gloves/nitrile"))
        self.assertFalse(policy.allowed(f"{HOST}/catalog/other"))

    def test_specific_group_does_not_drop_the_star_group(self) -> None:
        body = "User-agent: CatalogDesk\nDisallow: /secret\n\nUser-agent: *\nDisallow: /checkout/\n"
        self.assertEqual(live._product_token(live.user_agent()), "catalogdesk")
        ours = live.RobotsPolicy.parse(body, live.user_agent())
        other = live.RobotsPolicy.parse(body, "OtherBot/1.0")
        self.assertFalse(ours.allowed(f"{HOST}/secret"))
        self.assertFalse(ours.allowed(f"{HOST}/checkout/"))
        self.assertTrue(other.allowed(f"{HOST}/secret"))
        self.assertFalse(other.allowed(f"{HOST}/checkout/"))

    def test_shorter_product_token_does_not_match(self) -> None:
        body = "User-agent: Catalog\nDisallow: /secret\n\nUser-agent: *\nDisallow: /checkout/\n"
        policy = live.RobotsPolicy.parse(body, live.user_agent())
        self.assertTrue(policy.allowed(f"{HOST}/secret"))
        self.assertFalse(policy.allowed(f"{HOST}/checkout/"))

    def test_pagination_stays_blocked_when_the_parser_allows_it(self) -> None:
        fetcher = _loaded_fetcher(0, "User-agent: *\nDisallow:\n")
        image = f"{HOST}/media/a.jpg?width=265"
        calls: list[str] = []

        def urlopen(request: object, timeout: int, context: object) -> _Response:
            url = getattr(request, "full_url", "")
            calls.append(url)
            return _Response(url, b"img")

        with mock.patch.object(live.urllib.request, "urlopen", urlopen):
            body, _content_type = fetcher.fetch_bytes(image)
            with self.assertRaises(live.FetchBlocked):
                fetcher.fetch_bytes(f"{HOST}/media/a.jpg?page=2")
        self.assertEqual(body, b"img")
        self.assertEqual(calls, [image])

    def test_redirect_to_pagination_is_not_returned(self) -> None:
        fetcher = _loaded_fetcher(0, "User-agent: *\nDisallow:\n")
        calls: list[str] = []

        def urlopen(request: object, timeout: int, context: object) -> _Response:
            calls.append(getattr(request, "full_url", ""))
            return _Response(f"{HOST}/catalog/gloves?page=2", b"<html>page</html>")

        with mock.patch.object(live.urllib.request, "urlopen", urlopen):
            with mock.patch.object(live.time, "sleep", side_effect=AssertionError("slept")):
                with self.assertRaises(live.FetchBlocked):
                    fetcher.get(f"{HOST}/catalog/gloves")
        self.assertEqual(len(calls), 1)

    def _retry_sleeps(self, code: int, header: str | None) -> list[float]:
        fetcher = _loaded_fetcher(3, "User-agent: *\nDisallow:\n")
        url = f"{HOST}/product/widget"
        calls = {"n": 0}
        slept: list[float] = []

        def urlopen(request: object, timeout: int, context: object) -> _Response:
            calls["n"] += 1
            if calls["n"] < 2:
                raise _http_error(url, code, header)
            return _Response(url)

        with mock.patch.object(live.urllib.request, "urlopen", urlopen):
            with mock.patch.object(live.time, "monotonic", return_value=100.0):
                with mock.patch.object(live.time, "sleep", side_effect=slept.append):
                    body = fetcher.get(url)
        self.assertEqual(body, "<html>ok</html>")
        self.assertEqual(calls["n"], 2)
        return slept

    def test_retry_after_replaces_the_delay_and_is_capped(self) -> None:
        self.assertEqual(self._retry_sleeps(429, "10"), [10.0])
        self.assertEqual(self._retry_sleeps(503, "10"), [10.0])
        self.assertEqual(self._retry_sleeps(429, "0"), [3.0])
        self.assertEqual(self._retry_sleeps(429, "120"), [60.0])
        self.assertEqual(self._retry_sleeps(429, "not-a-date"), [3.0])
        self.assertEqual(self._retry_sleeps(429, "Wed, 21 Oct 1990 07:28:00 GMT"), [3.0])
        self.assertEqual(self._retry_sleeps(500, "10"), [3.0])


class AskTests(unittest.TestCase):
    def test_keyword_match_does_not_call_the_model(self) -> None:
        import chat

        with mock.patch.object(chat, "configured", return_value=False):
            with mock.patch.object(chat, "get_settings", return_value={"user_level": "viewer"}):
                with mock.patch.object(chat, "run_select", return_value=(["sku"], [])):
                    with mock.patch.object(chat, "choose", side_effect=AssertionError("model")):
                        reply = chat.ask("how many are in stock")
        self.assertEqual(reply.query_id, "in_stock_count")

    def test_claude_reply_is_shown_when_no_query_fits(self) -> None:
        import chat
        from claude import Choice

        choice = Choice(None, {}, False, "Viewer can run Child SKUs in stock.")
        with mock.patch.object(chat, "configured", return_value=True):
            with mock.patch.object(chat, "get_settings", return_value={"user_level": "viewer"}):
                with mock.patch.object(chat, "choose", return_value=choice):
                    reply = chat.ask("what queries do you support?")
        self.assertEqual(reply.answer, "Viewer can run Child SKUs in stock.")
        self.assertEqual(reply.sql, "")

    def test_ssl_context_loads_a_ca_bundle(self) -> None:
        import claude

        context = claude._ssl_context()
        self.assertGreater(context.cert_store_stats()["x509_ca"], 0)


def _cards(*rows: tuple[str, str, bool]) -> server.Category:
    return server.Category(
        key="gloves",
        title="Dental Exam Gloves",
        stats="",
        cards=[
            server.ListingCard(slug=slug, name=name, manufacturer="", price="", saved=saved)
            for slug, name, saved in rows
        ],
    )


def _product_list(slug: str, collection_url: str) -> str:
    elements: list[dict[str, object]] = []
    if collection_url:
        elements.append(
            {
                "@type": "ListItem",
                "item": {
                    "@type": "CollectionPage",
                    "name": "Not a product",
                    "url": collection_url,
                },
            }
        )
    elements.append(
        {
            "@type": "ListItem",
            "item": {
                "@type": "Product",
                "name": slug,
                "url": f"https://www.safcodental.com/product/{slug}",
                "brand": "Acme",
                "offers": {"price": "1.00"},
            },
        }
    )
    payload = {"@type": "ItemList", "name": "Products", "itemListElement": elements}
    return '<script type="application/ld+json">' + json.dumps(payload) + "</script>"


def _batch(
    categories: dict[str, server.Category],
    products: dict[str, server.Product],
    checkpoints: list[dict[str, str]] | None = None,
) -> server.Catalog:
    return server.Catalog(
        categories=categories,
        products=products,
        robots="",
        steps=[],
        checkpoints=checkpoints or [],
    )


if __name__ == "__main__":
    unittest.main()
