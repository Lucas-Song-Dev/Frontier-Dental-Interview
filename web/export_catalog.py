"""Write the catalog as CSV, JSON, and xlsx. One row per child SKU."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample"

COLUMNS: tuple[tuple[str, str], ...] = (
    ("product_name", "Parent product name."),
    ("brand", "Brand or manufacturer."),
    ("parent_sku", "Parent SKU from JSON-LD."),
    ("sku", "Child SKU from masterData. Parent SKU when a page had no child rows."),
    ("variant_name", "Child product name."),
    ("mfr_number", "Manufacturer part number."),
    ("item_group", "Variant group, such as needle type."),
    ("category_key", "Seed key: sutures or gloves."),
    ("category_title", "Listing title."),
    ("category_path", "Breadcrumb under Home, excluding the product name."),
    ("url", "Public product URL."),
    ("variant_price", "Child price in USD. Blank when the page has no price."),
    ("parent_price", "Parent JSON-LD price in USD."),
    ("pack_size", "Pack size parsed from the child specification or name."),
    ("stock", "Child stock label from masterData."),
    ("availability", "Parent availability from JSON-LD."),
    ("description", "Parent description."),
    ("specification", "Child specification text."),
    ("attributes", "JSON attributes for the child row."),
    ("image_urls", "JSON list of parent image URLs."),
    ("alternatives", "JSON list of recommendation cards with name and url."),
    ("product_slug", "Product URL slug."),
)

HEADERS = [key for key, _note in COLUMNS]


def rows_in_scope(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Drop a row whose breadcrumb is outside the two seeds or whose key disagrees."""
    from scope import scope_path

    kept: list[dict[str, object]] = []
    for row in rows:
        decision = scope_path(str(row.get("category_path") or ""))
        if decision.status != "in_scope":
            continue
        if str(row.get("category_key") or "") != decision.category:
            continue
        kept.append(row)
    return kept


def rows_from_catalog(catalog: object) -> list[dict[str, object]]:
    products = catalog.products  # type: ignore[attr-defined]
    categories = catalog.categories  # type: ignore[attr-defined]
    rows: list[dict[str, object]] = []
    for product in products.values():
        category = categories.get(product.category)
        title = category.title if category is not None else product.category
        children = product.variants or [None]
        for variant in children:
            rows.append(_row(product, variant, title))
    rows.sort(key=lambda row: (str(row["category_title"]), str(row["product_name"]), str(row["sku"])))
    return rows


def csv_bytes(rows: list[dict[str, object]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=HEADERS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def json_bytes(rows: list[dict[str, object]]) -> bytes:
    payload = {
        "grain": "one row per child SKU",
        "columns": [{"name": key, "description": note} for key, note in COLUMNS],
        "rows": rows,
    }
    return json.dumps(payload, indent=2).encode("utf-8")


def xlsx_bytes(rows: list[dict[str, object]]) -> bytes:
    sheet = _sheet(rows)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as book:
        book.writestr(
            "[Content_Types].xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
  <Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>""",
        )
        book.writestr(
            "_rels/.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>""",
        )
        book.writestr(
            "xl/workbook.xml",
            """<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="Catalog" sheetId="1" r:id="rId1"/></sheets>
</workbook>""",
        )
        book.writestr(
            "xl/_rels/workbook.xml.rels",
            """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>""",
        )
        book.writestr("xl/worksheets/sheet1.xml", sheet)
    return buffer.getvalue()


def publish(catalog: object, steps: list[dict[str, str]] | None = None) -> None:
    """Write the sample files the assignment asks to submit."""
    SAMPLE.mkdir(parents=True, exist_ok=True)
    rows = rows_in_scope(rows_from_catalog(catalog))
    (SAMPLE / "catalog.csv").write_bytes(csv_bytes(rows))
    (SAMPLE / "catalog.json").write_bytes(json_bytes(rows))
    (SAMPLE / "catalog.xlsx").write_bytes(xlsx_bytes(rows))
    if steps is not None:
        lines = [json.dumps(step, ensure_ascii=False) for step in steps]
        (SAMPLE / "pipeline-log.jsonl").write_text(
            "\n".join(lines) + ("\n" if lines else ""),
            encoding="utf-8",
        )
        legacy = SAMPLE / "agent-log.jsonl"
        if legacy.exists():
            legacy.unlink()


def _row(product: object, variant: object | None, title: str) -> dict[str, object]:
    if variant is None:
        sku = product.sku  # type: ignore[attr-defined]
        name = product.name  # type: ignore[attr-defined]
        group = ""
        mfr = ""
        stock = ""
        price = ""
        pack = ""
        specification = ""
        attributes = "{}"
    else:
        sku = variant.sku  # type: ignore[attr-defined]
        name = variant.name  # type: ignore[attr-defined]
        group = variant.group  # type: ignore[attr-defined]
        mfr = variant.mfr  # type: ignore[attr-defined]
        stock = variant.stock  # type: ignore[attr-defined]
        price = _amount(variant.price)  # type: ignore[attr-defined]
        pack = variant.pack_size  # type: ignore[attr-defined]
        specification = variant.specification  # type: ignore[attr-defined]
        attributes = variant.attributes  # type: ignore[attr-defined]
    return {
        "product_name": product.name,  # type: ignore[attr-defined]
        "brand": product.brand,  # type: ignore[attr-defined]
        "parent_sku": product.sku,  # type: ignore[attr-defined]
        "sku": sku,
        "mfr_number": mfr,
        "item_group": group,
        "category_key": product.category,  # type: ignore[attr-defined]
        "category_title": title,
        "category_path": product.category_path,  # type: ignore[attr-defined]
        "url": product.url,  # type: ignore[attr-defined]
        "variant_price": price,
        "parent_price": _amount(product.price),  # type: ignore[attr-defined]
        "pack_size": pack,
        "stock": stock,
        "availability": product.availability,  # type: ignore[attr-defined]
        "description": product.description,  # type: ignore[attr-defined]
        "specification": specification,
        "attributes": attributes,
        "image_urls": product.image_urls,  # type: ignore[attr-defined]
        "alternatives": product.alternatives,  # type: ignore[attr-defined]
        "product_slug": product.slug,  # type: ignore[attr-defined]
        "variant_name": name,
    }


def _amount(value: str) -> str:
    if not value:
        return ""
    return value.replace("$", "").replace(",", "")


def _sheet(rows: list[dict[str, object]]) -> str:
    header = "".join(f'<c t="inlineStr"><is><t>{escape(name)}</t></is></c>' for name in HEADERS)
    body = [_xml_row(1, header)]
    fields = list(HEADERS)
    for index, row in enumerate(rows, start=2):
        cells = []
        for key in fields:
            text = escape(_plain(row.get(key, "")))
            cells.append(f"<c t=\"inlineStr\"><is><t>{text}</t></is></c>")
        body.append(_xml_row(index, "".join(cells)))
    last = len(rows) + 1
    return (
        """<?xml version="1.0" encoding="UTF-8"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheetData>
"""
        + "\n".join(body)
        + f"""
  </sheetData>
  <dimension ref="A1:V{last}"/>
</worksheet>
"""
    )


def _xml_row(index: int, cells: str) -> str:
    return f'<row r="{index}">{cells}</row>'


def _plain(value: object) -> str:
    text = "" if value is None else str(value)
    return "".join(character for character in text if character >= " " or character in "\t")
