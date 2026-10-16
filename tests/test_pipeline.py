"""Navigator, classifier, extractor, validator, and retry decisions."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

from extract import classify_page  # noqa: E402
from pipeline import run  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CHILD = (
    'window.masterData = {"ABC":{"sku":"ABC","name":"Child",'
    '"manufacturer_part_number":"M","stock_availability_label":"In stock",'
    '"price":"<span data-price-amount=\\"4.00\\"></span>"}};'
)


def _product_page(name: str, sku: str) -> str:
    return (
        '<nav class="breadcrumbs"><a>Home</a><a>Sutures &amp; surgical products</a>'
        f'<span class="breadcrumb-last-node">{name}</span></nav>'
        '<script type="application/ld+json">'
        f'{{"@type":"Product","name":"{name}","sku":"{sku}",'
        '"offers":{"price":"1.00","availability":"https://schema.org/InStock"}}'
        "</script>"
        + CHILD
    )


class PipelineTests(unittest.TestCase):
    def test_classifier_labels_the_three_page_kinds(self) -> None:
        self.assertEqual(classify_page("https://www.safcodental.com/catalog/gloves", 'class="ais-Hits-item"'), "listing")
        self.assertEqual(classify_page("https://www.safcodental.com/product/widget", "window.masterData = {}"), "product")
        self.assertEqual(classify_page("https://www.safcodental.com/about", "<html></html>"), "other")

    def test_json_ld_only_page_is_recovered(self) -> None:
        page = (
            '<nav class="breadcrumbs"><a>Home</a><a>Sutures &amp; surgical products</a>'
            '<span class="breadcrumb-last-node">Widget</span></nav>'
            '<script type="application/ld+json">'
            '{"@type":"Product","name":"Widget","sku":"PARENT",'
            '"offers":{"price":"1.00","availability":"https://schema.org/InStock"}}'
            "</script>"
        )
        trace = run([], [("widget", "sutures", "https://www.safcodental.com/product/widget", page)])
        self.assertEqual(len(trace.products), 1)
        self.assertEqual(trace.products[0].variants, [])
        self.assertEqual(trace.products[0].category, "sutures")
        self.assertTrue(any(step["stage"] == "retry" and step["outcome"] == "recovered" for step in trace.steps))

    def test_missing_name_is_rejected(self) -> None:
        page = (
            '<nav class="breadcrumbs"><a>Home</a><a>Sutures &amp; surgical products</a></nav>'
            + CHILD
        )
        trace = run([], [("", "sutures", "https://www.safcodental.com/product/x", page)])
        self.assertEqual(trace.products, [])
        self.assertTrue(any(step["stage"] == "validator" and step["outcome"] == "rejected" for step in trace.steps))

    def test_navigator_keeps_seed_subcategories_and_blocks_pagination(self) -> None:
        page = (FIXTURES / "listing_sutures.html").read_text(encoding="utf-8")
        url = "https://www.safcodental.com/catalog/sutures-surgical-products"
        trace = run([("sutures", url, page)], [])
        allowed = next(step for step in trace.steps if step["stage"] == "navigator" and step["outcome"] == "allowed")
        self.assertIn("8 category URLs", allowed["detail"])
        self.assertTrue(any(step["outcome"] == "disallowed" for step in trace.steps))
        self.assertFalse(any(item["status"] == "fetched" and "page=" in item["url"] for item in trace.checkpoints))

    def test_same_child_sku_on_two_parents_is_stored_once(self) -> None:
        trace = run(
            [],
            [
                ("one", "sutures", "https://www.safcodental.com/product/one", _product_page("One", "ONE")),
                ("two", "sutures", "https://www.safcodental.com/product/two", _product_page("Two", "TWO")),
            ],
        )
        stored = [row.sku for product in trace.products for row in product.variants]
        self.assertEqual(stored, ["ABC"])
        self.assertTrue(any(step["outcome"] == "deduped" for step in trace.steps))

    def test_shared_sku_set_dedupes_across_separate_runs(self) -> None:
        seen: set[str] = set()
        first = run(
            [],
            [("one", "sutures", "https://www.safcodental.com/product/one", _product_page("One", "ONE"))],
            seen_sku=seen,
        )
        second = run(
            [],
            [("two", "sutures", "https://www.safcodental.com/product/two", _product_page("Two", "TWO"))],
            seen_sku=seen,
        )
        self.assertEqual([row.sku for product in first.products for row in product.variants], ["ABC"])
        self.assertEqual([row.sku for product in second.products for row in product.variants], [])
        self.assertTrue(any(step["outcome"] == "deduped" for step in second.steps))


if __name__ == "__main__":
    unittest.main()
