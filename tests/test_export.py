"""CSV, JSON, and xlsx share one row per child SKU."""

from __future__ import annotations

import csv
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

import export_catalog  # noqa: E402
import server  # noqa: E402
from export_catalog import COLUMNS, HEADERS, csv_bytes, json_bytes, rows_from_catalog, xlsx_bytes  # noqa: E402


class ExportTests(unittest.TestCase):
    def test_fixture_catalog_has_one_row_per_child(self) -> None:
        rows = rows_from_catalog(server.CATALOG)
        self.assertEqual(len(rows), 55)
        self.assertEqual([key for key, _note in COLUMNS], HEADERS)
        self.assertTrue(all(set(HEADERS) <= set(row) for row in rows))

    def test_csv_and_json_and_xlsx_list_the_same_skus(self) -> None:
        rows = rows_from_catalog(server.CATALOG)
        expected = [row["sku"] for row in rows]
        parsed = list(csv.DictReader(io.StringIO(csv_bytes(rows).decode("utf-8"))))
        self.assertEqual(parsed[0].keys(), dict.fromkeys(HEADERS).keys())
        self.assertEqual([row["sku"] for row in parsed], expected)
        document = json.loads(json_bytes(rows))
        self.assertEqual([column["name"] for column in document["columns"]], HEADERS)
        self.assertEqual([row["sku"] for row in document["rows"]], expected)
        with zipfile.ZipFile(io.BytesIO(xlsx_bytes(rows))) as book:
            sheet = book.read("xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertIn("product_name", sheet)
        self.assertIn("sku", sheet)
        for sku in expected:
            self.assertIn(str(sku), sheet)

    def test_parent_without_children_uses_the_parent_sku(self) -> None:
        product = server.Product(
            slug="only",
            name="Only",
            sku="PARENT",
            price="$1.00",
            availability="In stock",
            category="sutures",
            variants=[],
            url="https://www.safcodental.com/product/only",
        )
        category = server.Category(key="sutures", title="Sutures", stats="", cards=[])
        catalog = server.Catalog(
            categories={"sutures": category},
            products={"only": product},
            robots="",
        )
        rows = rows_from_catalog(catalog)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sku"], "PARENT")
        self.assertEqual(rows[0]["product_name"], "Only")

    def test_publish_drops_a_clearance_parent(self) -> None:
        glove = server.Product(
            slug="lavender-nitrile",
            name="Lavender Nitrile",
            sku="G",
            price="$1.00",
            availability="In stock",
            category="gloves",
            category_path="Dental Supplies / Dental Exam Gloves",
            url="https://www.safcodental.com/product/lavender-nitrile",
        )
        clearance = server.Product(
            slug="clearance-item",
            name="Clearance Item",
            sku="C",
            price="$1.00",
            availability="In stock",
            category="sutures",
            category_path="Clearance / Safco Clearance",
            url="https://www.safcodental.com/product/clearance-item",
        )
        catalog = server.Catalog(
            categories={
                "gloves": server.Category(key="gloves", title="Dental Exam Gloves", stats="", cards=[]),
                "sutures": server.Category(
                    key="sutures", title="Sutures & surgical products", stats="", cards=[]
                ),
            },
            products={"lavender-nitrile": glove, "clearance-item": clearance},
            robots="",
        )
        with tempfile.TemporaryDirectory() as directory:
            sample = Path(directory)
            with mock.patch.object(export_catalog, "SAMPLE", sample):
                export_catalog.publish(catalog)
            document = json.loads((sample / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual([row["product_slug"] for row in document["rows"]], ["lavender-nitrile"])


if __name__ == "__main__":
    unittest.main()
