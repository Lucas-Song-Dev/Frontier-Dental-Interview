"""Extraction of the assignment fields from saved product HTML."""

from __future__ import annotations

import html
import json
import re
import sys
import unittest
from pathlib import Path
from urllib.parse import unquote

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "web"))

from extract import (  # noqa: E402
    extract_listing,
    extract_product,
    image_urls,
    pack_size,
    pagination_links,
)
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


class ListingCardTests(unittest.TestCase):
    def test_json_ld_products_skip_subcategory_pages(self) -> None:
        page = _ld(
            _item_list(
                "Subcategories",
                [
                    {
                        "@type": "ListItem",
                        "item": {
                            "@type": "CollectionPage",
                            "name": "Nitrile gloves",
                            "url": "https://www.safcodental.com/catalog/gloves/nitrile-gloves",
                        },
                    }
                ],
            ),
            _item_list(
                "Products",
                [
                    _list_item("lavender-nitrile", "Lavender Nitrile", "Halyard", "38.99"),
                    _list_item(
                        "aquasoft",
                        "Aquasoft",
                        "Halyard",
                        "9.50",
                        url_on_item=False,
                        brand_as_string=True,
                    ),
                ],
            ),
        )
        cards = extract_listing(page, "gloves").cards
        self.assertEqual(
            [(card.slug, card.name, card.manufacturer, card.price) for card in cards],
            [
                ("lavender-nitrile", "Lavender Nitrile", "Halyard", "$38.99"),
                ("aquasoft", "Aquasoft", "Halyard", "$9.50"),
            ],
        )

    def test_hit_grid_is_used_when_json_ld_has_no_products(self) -> None:
        page = (
            '<li class="ais-Hits-item">'
            '<a href="https://www.safcodental.com/product/from-hit">x</a>'
            '<h3 itemprop="name">From hit</h3>'
            '<h3 class="manufacturer-name">Maker</h3>'
            "<span>$4.00</span>"
            "</li>"
        )
        cards = extract_listing(page, "gloves").cards
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].slug, "from-hit")
        self.assertEqual(cards[0].name, "From hit")
        self.assertEqual(cards[0].manufacturer, "Maker")
        self.assertEqual(cards[0].price, "$4.00")

    def test_listing_photo_comes_from_json_ld_and_the_hit_grid(self) -> None:
        photo = "https://www.safcodental.com/media/catalog/product/d/r/drcas.jpg?width=265"
        page = _ld(
            _item_list(
                "Products",
                [_list_item("lavender-nitrile", "Lavender Nitrile", "Halyard", "38.99", image=photo)],
            )
        )
        cards = extract_listing(page, "gloves").cards
        self.assertEqual(cards[0].image, photo)
        hit = (
            '<li class="ais-Hits-item">'
            '<a href="https://www.safcodental.com/product/from-hit">x</a>'
            '<h3 itemprop="name">From hit</h3>'
            f'<img src="{photo}&amp;height=265" alt="From hit">'
            "</li>"
        )
        hit_cards = extract_listing(hit, "gloves").cards
        self.assertEqual(hit_cards[0].image, photo + "&height=265")

    def test_saved_listings_keep_a_photo_for_every_card(self) -> None:
        for name, key in (
            ("listing_gloves.html", "gloves"),
            ("listing_sutures.html", "sutures"),
        ):
            page = (FIXTURES / name).read_text(encoding="utf-8")
            cards = extract_listing(page, key).cards
            self.assertTrue(cards, name)
            self.assertTrue(
                all(card.image.startswith("https://www.safcodental.com/media/catalog/product/") for card in cards),
                name,
            )

    def test_json_ld_wins_when_both_lists_differ(self) -> None:
        page = _ld(
            _item_list("Products", [_list_item("from-json", "From JSON", "Brand", "1.00")])
        )
        page += (
            '<li class="ais-Hits-item">'
            '<a href="https://www.safcodental.com/product/from-hit">x</a>'
            '<h3 itemprop="name">From hit</h3>'
            "</li>"
        )
        cards = extract_listing(page, "gloves").cards
        self.assertEqual([card.slug for card in cards], ["from-json"])

    def test_a_broken_json_ld_script_does_not_drop_the_next_list(self) -> None:
        page = (
            '<script type="application/ld+json">{not json</script>'
            + _ld(_item_list("Products", [_list_item("kept", "Kept", "Brand", "2.00")]))
        )
        cards = extract_listing(page, "gloves").cards
        self.assertEqual([card.slug for card in cards], ["kept"])

    def test_fixtures_keep_the_hit_grid_slugs(self) -> None:
        for name, key in (
            ("listing_gloves.html", "gloves"),
            ("listing_sutures.html", "sutures"),
        ):
            page = (FIXTURES / name).read_text(encoding="utf-8")
            cards = extract_listing(page, key).cards
            self.assertEqual(
                [(card.slug, card.name, card.manufacturer, card.price) for card in cards],
                _hit_rows(page),
                name,
            )


def _ld(*lists: dict[str, object]) -> str:
    return '<script type="application/ld+json">' + json.dumps(list(lists)) + "</script>"


def _item_list(name: str, elements: list[dict[str, object]]) -> dict[str, object]:
    return {"@type": "ItemList", "name": name, "itemListElement": elements}


def _list_item(
    slug: str,
    name: str,
    brand: str,
    price: str,
    *,
    url_on_item: bool = True,
    brand_as_string: bool = False,
    image: str = "",
) -> dict[str, object]:
    url = f"https://www.safcodental.com/product/{slug}"
    brand_value: object = brand if brand_as_string else {"@type": "Brand", "name": brand}
    item: dict[str, object] = {
        "@type": "Product",
        "name": name,
        "brand": brand_value,
        "offers": {"@type": "Offer", "price": price},
    }
    if image:
        item["image"] = [image]
    element: dict[str, object] = {"@type": "ListItem", "item": item}
    if url_on_item:
        item["url"] = url
    else:
        element["url"] = url
    return element


def _hit_rows(page: str) -> list[tuple[str, str, str, str]]:
    rows: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for chunk in page.split('<li class="ais-Hits-item')[1:]:
        href = re.search(r'href="https://www\.safcodental\.com/product/([^"]+)"', chunk)
        name = re.search(r'itemprop="name"[^>]*>\s*([^<]+)', chunk)
        if not href or not name:
            continue
        slug = unquote(href.group(1)).strip("/")
        if slug in seen:
            continue
        seen.add(slug)
        maker = re.search(r'manufacturer-name[^>]*>\s*([^<]+)', chunk)
        amount = re.search(r"\$\s*([0-9,]+\.[0-9]{2})", chunk)
        text = re.sub(r"\s+", " ", name.group(1)).strip()
        brand = re.sub(r"\s+", " ", maker.group(1)).strip() if maker else ""
        rows.append(
            (
                slug,
                html.unescape(html.unescape(text)),
                html.unescape(html.unescape(brand)),
                f"${amount.group(1).replace(',', '')}" if amount else "",
            )
        )
    return rows


if __name__ == "__main__":
    unittest.main()
