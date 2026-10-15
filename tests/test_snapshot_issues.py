"""Regressions for failed snapshot runs and for keeping a saved snapshot."""

from __future__ import annotations

import ssl
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

import db  # noqa: E402
import live  # noqa: E402
import server  # noqa: E402

ROBOTS = "https://www.safcodental.com/robots.txt"
LIST_MASTER = (
    '[{"sku":"ABC","name":"Child","itemgroup":"Needle",'
    '"manufacturer_part_number":"M-1","stock_availability_label":"In stock",'
    '"price":"<span data-price-amount=\\"4.00\\"></span>"}]'
)


def _product_page(master: str) -> str:
    return (
        '<script type="application/ld+json">'
        '{"@type":"Product","name":"Widget","sku":"PARENT",'
        '"offers":{"price":"1.00","availability":"https://schema.org/InStock"}}'
        "</script>"
        f"window.masterData = {master};"
    )


class MasterDataTests(unittest.TestCase):
    def test_list_master_data_parses_without_calling_items(self) -> None:
        quoted = '"' + LIST_MASTER.replace("\\", "\\\\").replace('"', '\\"') + '"'
        for master in (LIST_MASTER, quoted):
            variants = server.parse_master(_product_page(master))
            self.assertEqual([row.sku for row in variants], ["ABC"])
            self.assertEqual(variants[0].name, "Child")
            self.assertEqual(variants[0].group, "Needle")
            self.assertEqual(variants[0].mfr, "M-1")
            self.assertEqual(variants[0].stock, "In stock")
            self.assertEqual(variants[0].price, "$4.00")

    def test_object_master_data_still_parses(self) -> None:
        master = (
            '{"ABC":{"sku":"ABC","name":"Child","itemgroup":"",'
            '"manufacturer_part_number":"M-1","stock_availability_label":"Special order",'
            '"price":{"price":"<span data-price-amount=\\"8.50\\"></span>"}}}'
        )
        variants = server.parse_master(_product_page(master))
        self.assertEqual(variants[0].sku, "ABC")
        self.assertEqual(variants[0].stock, "Special order")
        self.assertEqual(variants[0].price, "$8.50")

    def test_one_list_page_does_not_fail_the_snapshot(self) -> None:
        catalog = server.catalog_from_live(
            "User-agent: *\n",
            {
                "listings": [
                    (
                        "sutures",
                        "https://www.safcodental.com/catalog/sutures-surgical-products",
                        "<h1>Sutures</h1>",
                    )
                ],
                "products": [
                    (
                        "widget",
                        "sutures",
                        "https://www.safcodental.com/product/widget",
                        _product_page(LIST_MASTER),
                    ),
                    (
                        "broken",
                        "sutures",
                        "https://www.safcodental.com/product/broken",
                        "<html>no masterData</html>",
                    ),
                ],
                "note": "",
            },
        )
        self.assertEqual(list(catalog.products), ["widget"])
        self.assertEqual(catalog.products["widget"].variants[0].sku, "ABC")


class FetchTests(unittest.TestCase):
    def test_robots_fetch_uses_the_system_trust_store(self) -> None:
        bundle = Path("/etc/ssl/cert.pem")
        self.assertTrue(bundle.is_file())
        captured: dict[str, object] = {}

        class _Response:
            def __enter__(self) -> _Response:
                return self

            def __exit__(self, *_args: object) -> bool:
                return False

            def geturl(self) -> str:
                return ROBOTS

            def read(self) -> bytes:
                return b"User-agent: *\nDisallow: /*?page=\n"

        def urlopen(request: object, timeout: int, context: ssl.SSLContext) -> _Response:
            captured["url"] = request.full_url  # type: ignore[attr-defined]
            captured["context"] = context
            return _Response()

        with mock.patch.object(live.urllib.request, "urlopen", urlopen):
            body = live._get(ROBOTS, live._Clock(0), [])
        self.assertIn("Disallow", body)
        self.assertEqual(captured["url"], ROBOTS)
        context = captured["context"]
        self.assertIsInstance(context, ssl.SSLContext)
        assert isinstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        expected = ssl.create_default_context(cafile=str(bundle))
        self.assertEqual(context.cert_store_stats(), expected.cert_store_stats())

    def test_unreachable_robots_reports_the_url(self) -> None:
        def urlopen(request: object, timeout: int, context: ssl.SSLContext) -> object:
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            raise urllib.error.URLError("CERTIFICATE_VERIFY_FAILED")

        with mock.patch.object(live.urllib.request, "urlopen", urlopen):
            with self.assertRaises(RuntimeError) as caught:
                live._get(ROBOTS, live._Clock(0), [])
        self.assertEqual(str(caught.exception), f"Could not reach {ROBOTS}")

    def test_listing_pagination_is_not_fetched(self) -> None:
        rules = live._disallow_rules("User-agent: *\nDisallow: /*?page=\nDisallow: /*?p=\n")
        for url in (
            "https://www.safcodental.com/catalog/gloves?page=2",
            "https://www.safcodental.com/catalog/gloves?p=2",
        ):
            with self.assertRaises(live.FetchBlocked):
                live._require_allowed(url, rules)
        live._require_allowed("https://www.safcodental.com/catalog/gloves", rules)
        live._require_allowed("https://www.safcodental.com/product/perma-sharp-reg-sutures", rules)


class StartupTests(unittest.TestCase):
    def test_startup_keeps_an_existing_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(db, "DB_PATH", Path(directory) / "catalog.db"):
                db.ensure_database()
                db.reload_catalog(server.CATALOG)
                connection = db.connect()
                try:
                    connection.execute(
                        "INSERT INTO products (slug, category_key, name, parent_sku, price_amount, availability) "
                        "VALUES ('sentinel', 'sutures', 'Sentinel', 'SEN', 1.25, 'In stock')"
                    )
                    connection.execute(
                        "INSERT INTO variants (sku, product_slug, name, item_group, mfr_number, stock, price_amount) "
                        "VALUES ('SENT-1', 'sentinel', 'Sentinel child', '', '', 'In stock', 1.25)"
                    )
                    connection.commit()
                finally:
                    connection.close()

                loaded = server.open_catalog()
                self.assertIn("sentinel", loaded.products)
                self.assertEqual(loaded.products["sentinel"].variants[0].sku, "SENT-1")
                self.assertEqual(loaded.products["sentinel"].price, "$1.25")
                still = db.connect()
                try:
                    row = still.execute("SELECT slug FROM products WHERE slug = 'sentinel'").fetchone()
                finally:
                    still.close()
                self.assertIsNotNone(row)


if __name__ == "__main__":
    unittest.main()
