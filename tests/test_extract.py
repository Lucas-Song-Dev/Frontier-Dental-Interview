"""Extraction of the assignment fields from saved product HTML."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

from extract import extract_product, image_urls, pack_size, pagination_links  # noqa: E402
from pipeline import run  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class FixtureExtractionTests(unittest.TestCase):
    def test_surgifoam_has_the_catalog_fields(self) -> None:
        page = (FIXTURES / "product_a.html").read_text(encoding="utf-8")
        product = extract_product(
            page,
            "surgifoam-reg",
            "sutures",
            "https://www.safcodental.com/product/surgifoam-reg",
        )
        self.assertEqual(product.name, "Surgifoam")
        self.assertEqual(product.brand, "Ethicon")
        self.assertEqual(product.sku, "PFPJK")
        self.assertIn("Sutures & surgical products", product.category_path)
        self.assertIn("Surgical medicaments", product.category_path)
        self.assertTrue(product.description.startswith("Sterile"))
        self.assertTrue(product.image_urls.startswith("["))
        images = json.loads(product.image_urls)
        self.assertTrue(images[0].startswith("https://www.safcodental.com/"))
        packs = {row.pack_size for row in product.variants}
        self.assertIn("12/box", packs)
        self.assertTrue(all(row.stock for row in product.variants))
        self.assertTrue(any("2cm x 6cm" in row.specification for row in product.variants))
        alternatives = json.loads(product.alternatives)
        self.assertGreaterEqual(len(alternatives), 1)
        self.assertTrue(alternatives[0]["url"].startswith("https://www.safcodental.com/product/"))
        self.assertNotIn("surgifoam-reg", alternatives[0]["url"])
        self.assertTrue(all("<" not in item["name"] and "&" not in item["name"] for item in alternatives))

    def test_glove_pack_size_and_breadcrumb(self) -> None:
        page = (FIXTURES / "product_b.html").read_text(encoding="utf-8")
        product = extract_product(page, "lavender-nitrile", "gloves")
        self.assertIn("Dental Exam Gloves", product.category_path)
        self.assertIn("Nitrile gloves", product.category_path)
        self.assertEqual(product.variants[0].pack_size, "250/box")
        self.assertTrue(product.brand)

    def test_listing_pagination_is_recorded_and_not_a_fetch(self) -> None:
        page = (FIXTURES / "listing_sutures.html").read_text(encoding="utf-8")
        blocked = pagination_links(page)
        self.assertTrue(blocked)
        self.assertTrue(all("page=" in url or "p=" in url for url in blocked))
        trace = run(
            [("sutures", "https://www.safcodental.com/catalog/sutures-surgical-products", page)],
            [],
        )
        outcomes = [step["outcome"] for step in trace.steps if step["stage"] == "navigator"]
        self.assertIn("disallowed", outcomes)
        self.assertFalse(any(item["status"] == "fetched" and "page=" in item["url"] for item in trace.checkpoints))

    def test_validator_drops_a_repeated_sku(self) -> None:
        page = (FIXTURES / "product_a.html").read_text(encoding="utf-8")
        url = "https://www.safcodental.com/product/surgifoam-reg"
        trace = run(
            [],
            [
                ("surgifoam-reg", "sutures", url, page),
                ("surgifoam-reg", "sutures", url, page),
            ],
        )
        self.assertEqual(len(trace.products), 1)
        self.assertTrue(any(step["outcome"] == "deduped" for step in trace.steps))

    def test_retry_skips_a_page_with_no_product_markers(self) -> None:
        trace = run(
            [],
            [("broken", "sutures", "https://www.safcodental.com/product/broken", "<html>no masterData</html>")],
        )
        self.assertEqual(trace.products, [])
        self.assertTrue(any(step["stage"] == "retry" and step["outcome"] == "skipped" for step in trace.steps))
        self.assertIn("masterData", trace.steps[-1]["detail"])

    def test_perma_sharp_keeps_every_child_and_its_attributes(self) -> None:
        page = (FIXTURES / "product_variants.html").read_text(encoding="utf-8")
        product = extract_product(page, "perma-sharp-reg-sutures", "sutures")
        self.assertEqual(len(product.variants), 48)
        grouped = [row for row in product.variants if row.group]
        self.assertTrue(grouped)
        attributes = json.loads(grouped[0].attributes)
        self.assertEqual(attributes["item_group"], grouped[0].group)
        parent = json.loads(product.attributes)
        self.assertIn(grouped[0].group, parent["item_groups"])
        images = json.loads(product.image_urls)
        self.assertTrue(images[0].startswith("https://"))

    def test_brand_falls_back_to_manufacturer_name(self) -> None:
        page = (
            "window.masterData = "
            '{"ABC":{"sku":"ABC","name":"Child","manufacturer_name":"Acme",'
            '"manufacturer_part_number":"M-1","stock_availability_label":"In stock",'
            '"description":"plain","price":"<span data-price-amount=\\"1.00\\"></span>"}};'
        )
        product = extract_product(page, "child", "sutures")
        self.assertEqual(product.brand, "Acme")
        self.assertEqual(product.variants[0].pack_size, "")

    def test_pack_size_is_blank_without_a_count(self) -> None:
        self.assertEqual(pack_size("Plain gut suture", "plain gut"), "")

    def test_image_urls_collapse_to_one_path(self) -> None:
        linked = {
            "image": [
                "https://www.safcodental.com/media/catalog/product/p/f/pfpjk.jpg?width=265",
                "https://www.safcodental.com/media/catalog/product/p/f/pfpjk.jpg?width=700",
            ]
        }
        self.assertEqual(len(image_urls("", linked)), 1)

    def test_json_ld_without_master_data_keeps_the_parent(self) -> None:
        page = (
            '<script type="application/ld+json">'
            '{"@type":"Product","name":"Widget","sku":"PARENT",'
            '"offers":{"price":"1.00","availability":"https://schema.org/InStock"}}'
            "</script>"
        )
        product = extract_product(page, "widget", "sutures")
        self.assertTrue(product.recovered)
        self.assertEqual(product.variants, [])
        self.assertEqual(product.name, "Widget")
        self.assertEqual(product.sku, "PARENT")


if __name__ == "__main__":
    unittest.main()
