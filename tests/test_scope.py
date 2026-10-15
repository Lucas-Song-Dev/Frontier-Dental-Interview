"""Category membership comes from the product page, not the slug."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

import server  # noqa: E402
from pipeline import run  # noqa: E402
from scope import scope_page, scope_path  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
ROOT = Path(__file__).resolve().parents[1]


def _crumb(*parts: str) -> str:
    links = "".join(f"<a>{part}</a>" for part in parts)
    return f'<nav class="breadcrumbs"><a>Home</a>{links}</nav>'


class ScopeTests(unittest.TestCase):
    def test_fixture_breadcrumbs_are_in_scope(self) -> None:
        gloves = (FIXTURES / "product_b.html").read_text(encoding="utf-8")
        sutures = (FIXTURES / "product_variants.html").read_text(encoding="utf-8")
        self.assertEqual(scope_page(gloves, "Lavender Nitrile").category, "gloves")
        self.assertEqual(scope_page(sutures, "Perma Sharp sutures").category, "sutures")

    def test_body_class_counts_without_a_breadcrumb(self) -> None:
        page = '<body class="page-product categorypath-catalog-gloves-nitrile-gloves"></body>'
        decision = scope_page(page, "Widget")
        self.assertEqual(decision.status, "in_scope")
        self.assertEqual(decision.category, "gloves")

    def test_equipment_and_clearance_are_out_of_scope(self) -> None:
        equipment = _crumb("Equipment", "Mechanical Room Equipment", "Amalgam Separator")
        clearance = _crumb("Clearance", "Safco Clearance")
        self.assertEqual(scope_page(equipment, "Acadia").status, "out_of_scope")
        self.assertEqual(scope_page(clearance, "Clearance item").status, "out_of_scope")

    def test_blank_page_is_unresolved(self) -> None:
        decision = scope_page("<html></html>", "Widget")
        self.assertEqual(decision.status, "unresolved")
        self.assertEqual(scope_path("").status, "unresolved")

    def test_slug_does_not_assign_sutures(self) -> None:
        page = (
            _crumb("Equipment", "Mechanical Room Equipment")
            + '<script type="application/ld+json">'
            '{"@type":"Product","name":"Acadia","sku":"ACDAE",'
            '"offers":{"price":"1.00","availability":"https://schema.org/InStock"}}'
            "</script>"
        )
        trace = run([], [("acadia", "sutures", "https://www.safcodental.com/product/acadia", page)])
        self.assertEqual(trace.products, [])
        self.assertTrue(any(step["outcome"] == "out_of_scope" for step in trace.steps))

    def test_glove_breadcrumb_wins_over_a_sutures_hint(self) -> None:
        page = (
            _crumb("Dental Supplies", "Dental Exam Gloves", "Nitrile gloves")
            + '<script type="application/ld+json">'
            '{"@type":"Product","name":"Widget","sku":"PARENT",'
            '"offers":{"price":"1.00","availability":"https://schema.org/InStock"}}'
            "</script>"
        )
        trace = run([], [("widget", "sutures", "https://www.safcodental.com/product/widget", page)])
        self.assertEqual(trace.products[0].category, "gloves")

    def test_both_classes_use_config_order_and_record_both_titles(self) -> None:
        page = (
            '<body class="categorypath-catalog-gloves categorypath-catalog-sutures-surgical-products">'
            "</body>"
        )
        decision = scope_page(page, "Widget")
        self.assertEqual(decision.category, "sutures")
        self.assertIn("Sutures & surgical products", decision.path)
        self.assertIn("Dental Exam Gloves", decision.path)

    def test_a_class_that_only_contains_the_letters_does_not_match(self) -> None:
        page = '<body class="categorypath-catalog-glovestuff"></body>'
        self.assertEqual(scope_page(page).status, "out_of_scope")

    def test_pipeline_payload_lists_stages(self) -> None:
        payload = server._pipeline_payload()
        ids = [item["id"] for item in payload["stages"]]
        self.assertEqual(ids, ["navigator", "classifier", "extractor", "validator", "retry"])

    def test_appended_seed_title_does_not_pull_clearance_into_scope(self) -> None:
        path = "Clearance / Safco Clearance / Sutures & surgical products"
        self.assertEqual(scope_path(path).status, "out_of_scope")

    def test_class_does_not_override_an_out_of_scope_breadcrumb(self) -> None:
        page = (
            '<body class="categorypath-catalog-sutures-surgical-products">'
            + _crumb("Clearance", "Safco Clearance")
            + '<script type="application/ld+json">'
            '{"@type":"Product","name":"Clearance item","sku":"CLR",'
            '"offers":{"price":"1.00","availability":"https://schema.org/InStock"}}'
            "</script></body>"
        )

        def decide(*_args: object) -> str:
            raise AssertionError("model")

        trace = run(
            [],
            [("clearance-item", "sutures", "https://www.safcodental.com/product/clearance-item", page)],
            decide=decide,
        )
        self.assertEqual(trace.products, [])
        checkpoint = next(item for item in trace.checkpoints if item["status"] == "out_of_scope")
        self.assertNotIn("Sutures & surgical products", checkpoint["detail"])

    def test_model_runs_only_when_both_signals_name_a_seed(self) -> None:
        page = (
            '<body class="categorypath-catalog-gloves">'
            + _crumb("Dental Supplies", "Sutures & surgical products")
            + '<script type="application/ld+json">'
            '{"@type":"Product","name":"Widget","sku":"PARENT",'
            '"offers":{"price":"1.00","availability":"https://schema.org/InStock"}}'
            "</script></body>"
        )
        calls: list[tuple[object, object]] = []

        def decide(class_keys: tuple[str, ...], breadcrumb: str) -> str:
            calls.append((class_keys, breadcrumb))
            return "gloves"

        trace = run(
            [],
            [("widget", "sutures", "https://www.safcodental.com/product/widget", page)],
            decide=decide,
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(trace.products[0].category, "gloves")
        self.assertNotIn("<html", calls[0][1].lower())

    def test_model_failure_leaves_the_page_unresolved(self) -> None:
        page = (
            '<body class="categorypath-catalog-gloves">'
            + _crumb("Dental Supplies", "Sutures & surgical products")
            + '<script type="application/ld+json">'
            '{"@type":"Product","name":"Widget","sku":"PARENT",'
            '"offers":{"price":"1.00","availability":"https://schema.org/InStock"}}'
            "</script></body>"
        )
        trace = run(
            [],
            [("widget", "sutures", "https://www.safcodental.com/product/widget", page)],
            decide=lambda _keys, _path: None,
        )
        self.assertEqual(trace.products, [])
        self.assertTrue(any(item["status"] == "unresolved" for item in trace.checkpoints))

    def test_scope_prompt_omits_the_page_and_the_key(self) -> None:
        import claude

        prompt = claude.scope_prompt(("gloves",), "Dental Supplies / Sutures & surgical products")
        self.assertNotIn("<html", prompt.lower())
        self.assertNotIn("CLAUDE_API_KEY", prompt)
        with mock.patch.object(claude, "_setting", return_value=""):
            with mock.patch.object(claude.urllib.request, "urlopen", side_effect=AssertionError("network")):
                self.assertIsNone(
                    claude.decide_scope(("gloves",), "Dental Supplies / Sutures & surgical products")
                )

    def test_checked_in_sample_stays_inside_the_two_categories(self) -> None:
        sample = json.loads((ROOT / "data" / "sample" / "catalog.json").read_text(encoding="utf-8"))
        slugs = {row["product_slug"] for row in sample["rows"]}
        self.assertIn("lavender-nitrile", slugs)
        self.assertIn("perma-sharp-reg-sutures", slugs)
        self.assertIn("surgifoam-reg", slugs)
        for row in sample["rows"]:
            decision = scope_path(str(row["category_path"]))
            self.assertEqual(decision.status, "in_scope", row["category_path"])
            self.assertEqual(row["category_key"], decision.category)
            self.assertFalse(str(row["category_path"]).startswith("Equipment"))
            self.assertFalse(str(row["category_path"]).startswith("Clearance"))


if __name__ == "__main__":
    unittest.main()
