"""Local catalog desk. Refresh fetches allowed Safco URLs and skips disallowed ones."""

from __future__ import annotations

import html
import json
import re
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from chat import Reply, ask
from claude import configured
from crawl import crawl
from db import (
    SCHEMA_DOC,
    SETTING_BOUNDS,
    connect,
    ensure_database,
    ensure_settings,
    finish_job,
    get_settings,
    list_catalog_rows,
    list_checkpoints,
    list_jobs,
    list_steps,
    reload_catalog,
    save_settings,
    start_job,
    update_job,
    upsert_crawl,
)
from export_catalog import (
    COLUMNS,
    csv_bytes,
    json_bytes,
    publish,
    rows_from_catalog,
    rows_in_scope,
    xlsx_bytes,
)
from extract import extract_variants
from live import FetchBlocked
from pipeline import run as run_pipeline
from queries import LEVELS, describe, normalize_level

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT.parent / "tests" / "fixtures"
ROBOTS = ROOT.parent / "docs" / "recon" / "robots_2026-10-15.txt"
CSS = ROOT / "static" / "site.css"
DIST = ROOT / "ui" / "dist"
HOST = "127.0.0.1"
PORT = 8765

SITEMAP_PRODUCTS = 3939
SITEMAP_CATEGORIES = 282


@dataclass
class Variant:
    sku: str
    name: str
    group: str
    mfr: str
    stock: str
    price: str
    pack_size: str = ""
    specification: str = ""
    attributes: str = "{}"


@dataclass
class Product:
    slug: str
    name: str
    sku: str
    price: str
    availability: str
    category: str
    variants: list[Variant] = field(default_factory=list)
    brand: str = ""
    url: str = ""
    description: str = ""
    category_path: str = ""
    image_urls: str = "[]"
    alternatives: str = "[]"
    attributes: str = "{}"


@dataclass
class ListingCard:
    slug: str
    name: str
    manufacturer: str
    price: str
    saved: bool


@dataclass
class Category:
    key: str
    title: str
    stats: str
    cards: list[ListingCard]


@dataclass
class Catalog:
    categories: dict[str, Category]
    products: dict[str, Product]
    robots: str
    steps: list[dict[str, str]] = field(default_factory=list)
    checkpoints: list[dict[str, str]] = field(default_factory=list)


def unescape(value: str) -> str:
    return html.unescape(re.sub(r"\s+", " ", value)).strip()


def money(amount: str) -> str:
    return f"${amount}"


def parse_master(page: str) -> list[Variant]:
    return [
        Variant(
            sku=row.sku,
            name=row.name,
            group=row.group,
            mfr=row.mfr,
            stock=row.stock,
            price=row.price,
            pack_size=row.pack_size,
            specification=row.specification,
            attributes=row.attributes,
        )
        for row in extract_variants(page)
    ]


def parse_product(path: Path, slug: str, category: str) -> Product:
    return parse_product_html(path.read_text(encoding="utf-8"), slug, category)


def parse_product_html(page: str, slug: str, category: str, url: str = "") -> Product:
    from extract import extract_product

    parsed = extract_product(page, slug, category, url)
    return _product_from_parsed(parsed)


def parse_listing(path: Path, key: str) -> Category:
    return parse_listing_html(path.read_text(encoding="utf-8"), key)


def parse_listing_html(page: str, key: str) -> Category:
    heading = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.S)
    title = unescape(re.sub(r"<[^>]+>", "", heading.group(1))) if heading else key
    stats_at = page.find('class="ais-Stats"')
    stats = ""
    if stats_at >= 0:
        block = unescape(re.sub(r"<[^>]+>", " ", page[stats_at : stats_at + 700]))
        found = re.search(r"Items\s+\d+\s*-\s*\d+\s+of\s+\d+", block)
        stats = found.group(0) if found else ""
    cards: list[ListingCard] = []
    seen: set[str] = set()
    for chunk in page.split('<li class="ais-Hits-item')[1:]:
        href = re.search(
            r'href="https://www\.safcodental\.com/product/([^"]+)"', chunk
        )
        name = re.search(r'itemprop="name"[^>]*>\s*([^<]+)', chunk)
        if not href or not name:
            continue
        slug = unquote(href.group(1)).strip("/")
        if slug in seen:
            continue
        seen.add(slug)
        maker = re.search(r'manufacturer-name[^>]*>\s*([^<]+)', chunk)
        amount = re.search(r"\$\s*([0-9,]+\.[0-9]{2})", chunk)
        cards.append(
            ListingCard(
                slug=slug,
                name=unescape(name.group(1)),
                manufacturer=unescape(maker.group(1)) if maker else "",
                price=money(amount.group(1).replace(",", "")) if amount else "",
                saved=False,
            )
        )
    return Category(key=key, title=title, stats=stats, cards=cards)


def _product_from_parsed(parsed: object) -> Product:
    return Product(
        slug=parsed.slug,  # type: ignore[attr-defined]
        name=parsed.name,  # type: ignore[attr-defined]
        sku=parsed.sku,  # type: ignore[attr-defined]
        price=parsed.price,  # type: ignore[attr-defined]
        availability=parsed.availability,  # type: ignore[attr-defined]
        category=parsed.category,  # type: ignore[attr-defined]
        variants=[
            Variant(
                sku=row.sku,
                name=row.name,
                group=row.group,
                mfr=row.mfr,
                stock=row.stock,
                price=row.price,
                pack_size=row.pack_size,
                specification=row.specification,
                attributes=row.attributes,
            )
            for row in parsed.variants  # type: ignore[attr-defined]
        ],
        brand=parsed.brand,  # type: ignore[attr-defined]
        url=parsed.url,  # type: ignore[attr-defined]
        description=parsed.description,  # type: ignore[attr-defined]
        category_path=parsed.category_path,  # type: ignore[attr-defined]
        image_urls=parsed.image_urls,  # type: ignore[attr-defined]
        alternatives=parsed.alternatives,  # type: ignore[attr-defined]
        attributes=parsed.attributes,  # type: ignore[attr-defined]
    )


def build_catalog() -> Catalog:
    robots = ROBOTS.read_text(encoding="utf-8")

    def read(name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    return catalog_from_live(
        robots,
        {
            "listings": [
                (
                    "sutures",
                    "https://www.safcodental.com/catalog/sutures-surgical-products",
                    read("listing_sutures.html"),
                ),
                ("gloves", "https://www.safcodental.com/catalog/gloves", read("listing_gloves.html")),
            ],
            "products": [
                (
                    "surgifoam-reg",
                    "sutures",
                    "https://www.safcodental.com/product/surgifoam-reg",
                    read("product_a.html"),
                ),
                (
                    "perma-sharp-reg-sutures",
                    "sutures",
                    "https://www.safcodental.com/product/perma-sharp-reg-sutures",
                    read("product_variants.html"),
                ),
                (
                    "lavender-nitrile",
                    "gloves",
                    "https://www.safcodental.com/product/lavender-nitrile",
                    read("product_b.html"),
                ),
            ],
            "note": "Saved HTML fixtures.",
        },
    )


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _nav_item(href: str, label: str, key: str, current: str) -> str:
    current_attr = ' aria-current="page"' if current == key else ""
    return f'<li><a href="{href}"{current_attr}>{label}</a></li>'


def _level_name() -> str:
    return normalize_level(get_settings()["user_level"]).capitalize()


def _cadence() -> str:
    minutes = int(get_settings()["crawl_interval_minutes"])
    if minutes % 60 == 0:
        hours = minutes // 60
        label = "Every hour" if hours == 1 else f"Every {hours} hours"
    else:
        label = f"Every {minutes} minutes"
    return label


def layout(title: str, current: str, body: str) -> str:
    here = {
        "desk": "/",
        "sutures": "/category/sutures",
        "gloves": "/category/gloves",
        "gate": "/gate",
        "ask": "/chat",
        "jobs": "/jobs",
        "settings": "/settings",
    }.get(current, "/")
    catalog = "".join(
        [
            _nav_item("/", "Overview", "desk", current),
            _nav_item("/category/sutures", "Sutures", "sutures", current),
            _nav_item("/category/gloves", "Gloves", "gloves", current),
        ]
    )
    operate = "".join(
        [
            _nav_item("/jobs", "Jobs", "jobs", current),
            _nav_item("/chat", "Ask", "ask", current),
            _nav_item("/gate", "Gate", "gate", current),
            _nav_item("/settings", "Settings", "settings", current),
        ]
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{esc(title)}</title>
  <link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&amp;display=swap">
  <link rel="stylesheet" href="/static/site.css">
</head>
<body>
  <a class="skip" href="#content">Skip to content</a>
  <div class="app" id="app">
    <aside class="rail" id="rail">
      <div>
        <a class="brand" href="/">Catalog desk</a>
        <p class="rail-note">Safco catalog desk</p>
      </div>
      <nav aria-label="Catalog">
        <p class="rail-label">Catalog</p>
        <ul>{catalog}</ul>
      </nav>
      <nav aria-label="Operate">
        <p class="rail-label">Operate</p>
        <ul>{operate}</ul>
      </nav>
      <div class="cadence">
        <p>Time between crawls</p>
        <strong>{esc(_cadence())}</strong>
        <p class="cadence-next">User level</p>
        <strong>{esc(_level_name())}</strong>
      </div>
    </aside>
    <div class="workspace">
      <header class="bar">
        <button class="icon-btn" id="rail-toggle" type="button" aria-expanded="true" aria-controls="rail">
          <span class="bars" aria-hidden="true"></span>
          <span class="sr" id="rail-toggle-label">Hide sidebar</span>
        </button>
        <p class="bar-title">Snapshot · 15 Oct 2026</p>
        <div class="bar-actions">
          <form method="post" action="/refresh">
            <input type="hidden" name="next" value="{esc(here)}">
            <button class="refresh" type="submit">Refresh snapshot</button>
          </form>
          <div class="auto">
            <span id="auto-label">Auto</span>
            <button class="switch" id="auto-refresh" type="button" role="switch" aria-checked="false" aria-labelledby="auto-label">
              <span class="switch-knob"></span>
            </button>
          </div>
        </div>
      </header>
      <main id="content">
        {body}
      </main>
      <p class="foot">Saved pages only. Refresh reloads them into SQLite. Auto repeats that every 30 seconds.</p>
    </div>
  </div>
  <script>
  const app = document.getElementById("app");
  const railToggle = document.getElementById("rail-toggle");
  const railLabel = document.getElementById("rail-toggle-label");
  const auto = document.getElementById("auto-refresh");
  let timer = null;

  function setCollapsed(collapsed) {{
    app.classList.toggle("is-collapsed", collapsed);
    railToggle.setAttribute("aria-expanded", String(!collapsed));
    railLabel.textContent = collapsed ? "Show sidebar" : "Hide sidebar";
    localStorage.setItem("desk-rail", collapsed ? "collapsed" : "open");
  }}

  function setAuto(on) {{
    auto.setAttribute("aria-checked", on ? "true" : "false");
    localStorage.setItem("desk-auto", on ? "on" : "off");
    if (timer) clearInterval(timer);
    timer = null;
    if (!on) return;
    timer = setInterval(async () => {{
      const body = new URLSearchParams({{next: location.pathname}});
      await fetch("/refresh", {{
        method: "POST",
        headers: {{"Content-Type": "application/x-www-form-urlencoded"}},
        body,
        redirect: "manual"
      }});
      location.reload();
    }}, 30000);
  }}

  if (localStorage.getItem("desk-rail") === "collapsed") setCollapsed(true);
  railToggle.addEventListener("click", () => setCollapsed(!app.classList.contains("is-collapsed")));
  if (localStorage.getItem("desk-auto") === "on") setAuto(true);
  auto.addEventListener("click", () => setAuto(auto.getAttribute("aria-checked") !== "true"));
  </script>
</body>
</html>
"""


def decision_block() -> str:
    return """
<section class="decision" aria-labelledby="gate-heading">
  <div>
    <h2 id="gate-heading">Robots gate</h2>
    <p class="recommend">Recommendation <strong>yes</strong>, for the seed paths and /product/ URLs.</p>
    <p class="sign-label" id="sign-label">crawl allowed</p>
    <p class="sign-hint">Waiting for a person to write yes or no.</p>
  </div>
  <ul class="facts">
    <li>Seed paths are allowed.</li>
    <li class="blocked">Listing ?page= and ?p= are disallowed.</li>
    <li>Sitemap lists 3,939 product URLs and 282 category URLs.</li>
    <li>No Crawl-delay. One group: User-agent: *.</li>
  </ul>
</section>
"""


def card_table(category: Category) -> str:
    rows = []
    for card in category.cards:
        if card.saved:
            name = f'<a href="/product/{esc(card.slug)}">{esc(card.name)}</a>'
            detail = "Full sheet saved"
        else:
            name = esc(card.name)
            detail = "Listing card only"
        rows.append(
            "<tr>"
            f"<td>{esc(card.manufacturer)}</td>"
            f"<td>{name}</td>"
            f'<td class="num">{esc(card.price)}</td>'
            f"<td>{detail}</td>"
            "</tr>"
        )
    count = sum(1 for card in category.cards if card.saved)
    caption = f"{len(category.cards)} cards on page 1. {count} have a saved product sheet."
    return f"""
<div class="scroll">
  <table class="sheet">
    <caption>{esc(caption)}</caption>
    <thead>
      <tr>
        <th scope="col">Maker</th>
        <th scope="col">Product</th>
        <th scope="col" class="num">As low as</th>
        <th scope="col">Saved page</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
</div>
"""


def sheets_for(catalog: Catalog, key: str) -> list[Product]:
    return [product for product in catalog.products.values() if product.category == key]


def sheet_list(products: list[Product]) -> str:
    items = []
    for product in products:
        items.append(
            "<li>"
            f'<a href="/product/{esc(product.slug)}">{esc(product.name)}</a>'
            f" ({len(product.variants)} child SKUs)"
            "</li>"
        )
    return f"<ul>{''.join(items)}</ul>"


def home(catalog: Catalog) -> str:
    blocks = []
    for category in catalog.categories.values():
        blocks.append(
            f"""
<section class="block">
  <h2><a href="/category/{esc(category.key)}">{esc(category.title)}</a></h2>
  <p class="meta">{esc(category.stats)} on the saved listing page.</p>
  {sheet_list(sheets_for(catalog, category.key))}
</section>
"""
        )
    body = f"""
<h1>Saved Safco pages</h1>
<p class="lede">Two category listings and three grouped products from the 15 October recon. Prices and child SKUs come from those HTML files.</p>
{decision_block()}
<div class="split">
  {''.join(blocks)}
</div>
"""
    return layout("Catalog desk", "desk", body)


def category_page(catalog: Catalog, key: str) -> str | None:
    category = catalog.categories.get(key)
    if category is None:
        return None
    extra = [
        product
        for product in sheets_for(catalog, key)
        if product.slug not in {card.slug for card in category.cards}
    ]
    extra_html = ""
    if extra:
        extra_html = (
            "<h2>Also saved from this category</h2>"
            + sheet_list(extra)
        )
    body = f"""
<h1>{esc(category.title)}</h1>
<p class="lede">{esc(category.stats)} on the saved listing. Further listing pages use ?page=, which robots.txt disallows. This desk stops at page 1.</p>
{card_table(category)}
{extra_html}
"""
    return layout(category.title, key, body)


def product_page(catalog: Catalog, slug: str) -> str | None:
    product = catalog.products.get(slug)
    if product is None:
        return None
    category = catalog.categories[product.category]
    show_group = any(row.group for row in product.variants)
    group_head = '<th scope="col">Group</th>' if show_group else ""
    rows = []
    for row in product.variants:
        group_cell = f"<td>{esc(row.group)}</td>" if show_group else ""
        rows.append(
            "<tr>"
            f"<td>{esc(row.sku)}</td>"
            f"<td>{esc(row.name)}</td>"
            f"{group_cell}"
            f"<td>{esc(row.mfr)}</td>"
            f"<td>{esc(row.pack_size)}</td>"
            f"<td>{esc(row.stock)}</td>"
            f'<td class="num">{esc(row.price)}</td>'
            "</tr>"
        )
    body = f"""
<nav aria-label="Breadcrumb">
  <ol class="crumbs">
    <li><a href="/">Desk</a></li>
    <li><a href="/category/{esc(category.key)}">{esc(category.title)}</a></li>
    <li>{esc(product.name)}</li>
  </ol>
</nav>
<h1>{esc(product.name)}</h1>
<dl class="identity">
  <div><dt>Brand</dt><dd>{esc(product.brand)}</dd></div>
  <div><dt>Parent SKU</dt><dd>{esc(product.sku)}</dd></div>
  <div><dt>Category</dt><dd>{esc(product.category_path)}</dd></div>
  <div><dt>URL</dt><dd><a href="{esc(product.url)}">{esc(product.url)}</a></dd></div>
  <div><dt>Price</dt><dd>{esc(product.price)}</dd></div>
  <div><dt>Availability</dt><dd>{esc(product.availability)}</dd></div>
  <div><dt>Child SKUs</dt><dd>{len(product.variants)}</dd></div>
</dl>
<p>{esc(product.description)}</p>
<div class="scroll">
  <table class="sheet">
    <caption>Child rows from window.masterData in the saved product HTML.</caption>
    <thead>
      <tr>
        <th scope="col">SKU</th>
        <th scope="col">Name</th>
        {group_head}
        <th scope="col">Mfr #</th>
        <th scope="col">Pack</th>
        <th scope="col">Stock</th>
        <th scope="col" class="num">Price</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
</div>
"""
    return layout(product.name, product.category, body)


def gate_page(catalog: Catalog) -> str:
    body = f"""
<h1>What the gate allows</h1>
<p class="lede">Read on 15 October 2026 from https://www.safcodental.com/robots.txt. Terms of Sale do not mention bots. They do say unauthorized use of site material is prohibited. /terms-of-use is a 404.</p>
{decision_block()}
<h2>Sitemap</h2>
<p class="meta">products.xml has {SITEMAP_PRODUCTS:,} /product/ URLs. catalog.xml has {SITEMAP_CATEGORIES} category URLs, including both seeds. Neither file uses a query string.</p>
<h2>robots.txt</h2>
<pre class="rules">{esc(catalog.robots)}</pre>
"""
    return layout("Gate", "gate", body)


def _result_table(reply: Reply) -> str:
    if not reply.columns or (reply.columns == ["matches"] and len(reply.rows) == 1):
        return ""
    head = "".join(f"<th scope=\"col\">{esc(column)}</th>" for column in reply.columns)
    body = []
    for row in reply.rows:
        cells = "".join(f"<td>{esc(cell or '')}</td>" for cell in row)
        body.append(f"<tr>{cells}</tr>")
    return f"""
<div class="scroll">
  <table class="sheet">
    <caption>Rows returned by the query.</caption>
    <thead><tr>{head}</tr></thead>
    <tbody>{''.join(body)}</tbody>
  </table>
</div>
"""


def _exchange(question: str, reply: Reply) -> str:
    answer = "<br>".join(esc(line) for line in reply.answer.splitlines())
    label = f"Query · {reply.query_id}" if reply.query_id else "Query"
    sql = f"<details class=\"query\"><summary>{esc(label)}</summary><pre class=\"rules\">{esc(reply.sql)}</pre></details>" if reply.sql else ""
    return f"""
<article class="exchange">
  <p class="who">You</p>
  <p>{esc(question)}</p>
  <p class="who">Desk</p>
  <p>{answer}</p>
  {_result_table(reply)}
  {sql}
</article>
"""


def _query_section(items: list[dict[str, object]], *, allowed: bool) -> str:
    cards: list[str] = []
    for item in items:
        if bool(item["allowed"]) != allowed:
            continue
        run = ""
        if allowed:
            sku = ""
            if item["needs_sku"]:
                sku = (
                    '<label class="sku-label" for="sku-'
                    + esc(item["id"])
                    + '">SKU</label>'
                    + f'<input id="sku-{esc(item["id"])}" name="sku" value="PFPJK" autocomplete="off">'
                )
            run = (
                '<form class="query-run" method="post" action="/chat">'
                f'<input type="hidden" name="query_id" value="{esc(item["id"])}">'
                f"{sku}<button type=\"submit\">Run</button></form>"
            )
        pill_class = "pill" if allowed else "pill pill-held"
        state = "" if allowed else " is-held"
        cards.append(
            f'<li class="query-card{state}">'
            f'<div class="query-head"><strong>{esc(item["title"])}</strong>'
            f'<span class="{pill_class}">{esc(item["level"])}</span></div>'
            f'<p class="hint">{esc(item["phrase"])}</p>'
            f'<pre class="rules">{esc(item["sql"])}</pre>'
            f"{run}</li>"
        )
    if not cards:
        return "<p class=\"hint\">None.</p>"
    return f'<ul class="query-list">{"".join(cards)}</ul>'


def chat_page(question: str = "", reply: Reply | None = None) -> str:
    level = normalize_level(get_settings()["user_level"])
    listed = describe(level)
    label = question
    if reply and reply.query_id and not label:
        match = next((item for item in listed if item["id"] == reply.query_id), None)
        label = str(match["title"]) if match else reply.query_id
    exchange = _exchange(label, reply) if reply and (question or reply.query_id) else ""
    suggestions = [str(item["phrase"]) for item in listed if item["allowed"]][:5]
    buttons = "".join(
        f'<button type="button" class="suggestion" data-question="{esc(text)}">{esc(text)}</button>'
        for text in suggestions
    )
    claude = (
        "Claude is on and may choose among the queries this level allows."
        if configured()
        else "Add your key to .env as CLAUDE_API_KEY to let Claude choose a listed query."
    )
    body = f"""
<h1>Ask the snapshot</h1>
<p class="lede">The server keeps the valid SQL. {_level_name()} can run the first list. {claude}</p>
<div id="thread" role="log" aria-live="polite">{exchange}</div>
<section class="composer">
  <form id="ask-form" method="post" action="/chat">
    <label for="question">Question</label>
    <textarea id="question" name="question" rows="3" required></textarea>
    <button type="submit">Ask</button>
  </form>
  <div class="suggestions" aria-label="Example questions">{buttons}</div>
</section>
<section class="query-block" aria-labelledby="allowed-heading">
  <h2 id="allowed-heading">Allowed for {esc(level)}</h2>
  <p class="hint">Run sends the query id. The server checks the user level before SQLite sees it.</p>
  {_query_section(listed, allowed=True)}
</section>
<section class="query-block" aria-labelledby="held-heading">
  <h2 id="held-heading">Valid, not allowed at this level</h2>
  {_query_section(listed, allowed=False)}
</section>
<script>
const form = document.getElementById("ask-form");
const thread = document.getElementById("thread");
const field = document.getElementById("question");

function table(columns, rows) {{
  if (!columns.length) return "";
  if (columns.length === 1 && columns[0] === "matches" && rows.length === 1) return "";
  const head = columns.map((column) => `<th scope="col">${{column}}</th>`).join("");
  const body = rows.map((row) => `<tr>${{row.map((cell) => `<td>${{cell ?? ""}}</td>`).join("")}}</tr>`).join("");
  return `<div class="scroll"><table class="sheet"><caption>Rows returned by the query.</caption><thead><tr>${{head}}</tr></thead><tbody>${{body}}</tbody></table></div>`;
}}

function escapeHtml(value) {{
  return String(value).replace(/[&<>"]/g, (char) => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}}[char]));
}}

function addExchange(question, data) {{
  const article = document.createElement("article");
  article.className = "exchange";
  const answer = escapeHtml(data.answer).replace(/\\n/g, "<br>");
  const summary = data.query_id ? `Query · ${{escapeHtml(data.query_id)}}` : "Query";
  const sql = data.sql ? `<details class="query"><summary>${{summary}}</summary><pre class="rules">${{escapeHtml(data.sql)}}</pre></details>` : "";
  article.innerHTML = `<p class="who">You</p><p>${{escapeHtml(question)}}</p><p class="who">Desk</p><p>${{answer}}</p>${{table(data.columns.map(escapeHtml), data.rows.map((row) => row.map((cell) => cell == null ? "" : escapeHtml(cell))))}}${{sql}}`;
  thread.append(article);
  article.scrollIntoView({{block: "nearest"}});
}}

field.addEventListener("keydown", (event) => {{
  if (event.key !== "Enter" || event.isComposing) return;
  event.preventDefault();
  if (event.shiftKey) {{
    const start = field.selectionStart ?? field.value.length;
    const end = field.selectionEnd ?? start;
    field.value = field.value.slice(0, start) + "\\n" + field.value.slice(end);
    field.selectionStart = field.selectionEnd = start + 1;
    return;
  }}
  form.requestSubmit();
}});

form.addEventListener("submit", async (event) => {{
  event.preventDefault();
  const question = field.value.trim();
  if (!question) return;
  field.value = "";
  const response = await fetch("/api/chat", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{question}})
  }});
  const data = await response.json();
  addExchange(question, data);
}});

document.querySelectorAll(".suggestion").forEach((button) => {{
  button.addEventListener("click", () => {{
    field.value = button.dataset.question || "";
    form.requestSubmit();
  }});
}});
</script>
"""
    return layout("Ask the snapshot", "ask", body)


def jobs_page() -> str:
    jobs = list_jobs()
    if not jobs:
        table = """
<div class="empty">
  <h2>No jobs yet</h2>
  <p>Refresh the snapshot to parse the saved listing and product pages into SQLite. That run is recorded here.</p>
</div>
"""
    else:
        rows = []
        for job in jobs:
            when = (job["finished_at"] or job["started_at"]).replace("T", " ").replace("+00:00", " UTC")
            status = esc(job["status"])
            rows.append(
                "<tr>"
                f"<td>{esc(when)}</td>"
                f"<td>Snapshot refresh</td>"
                f'<td class="status-{status}">{status}</td>'
                f'<td class="num">{job["products"]}</td>'
                f'<td class="num">{job["variants"]}</td>'
                f'<td class="num">{job["cards"]}</td>'
                f"<td>{esc(job['message'])}</td>"
                "</tr>"
            )
        table = f"""
<div class="scroll">
  <table class="sheet">
    <caption>{len(jobs)} most recent jobs.</caption>
    <thead>
      <tr>
        <th scope="col">Finished</th>
        <th scope="col">Kind</th>
        <th scope="col">Status</th>
        <th scope="col" class="num">Products</th>
        <th scope="col" class="num">Variants</th>
        <th scope="col" class="num">Cards</th>
        <th scope="col">Note</th>
      </tr>
    </thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</div>
"""
    body = f"""
<h1>Jobs</h1>
<p class="lede">Each refresh parses the saved HTML and rewrites the catalog tables. Crawl timing for a later worker is set on Settings.</p>
{table}
"""
    return layout("Jobs", "jobs", body)


def _setting_field(key: str, label: str, hint: str, settings: dict[str, str], low: int, high: int) -> str:
    return f"""
<div class="field">
  <label for="{key}">{label}</label>
  <p class="hint">{hint}</p>
  <input id="{key}" name="{key}" type="number" min="{low}" max="{high}" required value="{esc(settings[key])}">
</div>
"""


def settings_page(error: str = "", saved: bool = False) -> str:
    settings = get_settings()
    notice = ""
    if error:
        notice = f'<p class="form-error" role="alert">{esc(error)}</p>'
    elif saved:
        notice = '<p class="form-ok" role="status">Settings saved.</p>'
    body = f"""
<h1>Settings</h1>
<p class="lede">These values are stored in SQLite. Put the Claude key in the <code>.env</code> file next to this project, as <code>CLAUDE_API_KEY=</code>. Refresh does not call Safco.</p>
{notice}
<form class="settings" method="post" action="/settings">
  <div class="field">
    <label for="user_level">User level</label>
    <p class="hint">Viewer runs catalog queries. Operator also runs the robots gate. Admin also reads jobs and crawl settings. The server enforces this on every query.</p>
    <select id="user_level" name="user_level">
      {''.join(f'<option value="{name}"{" selected" if settings["user_level"] == name else ""}>{name.capitalize()}</option>' for name in LEVELS)}
    </select>
  </div>
  {_setting_field("crawl_interval_minutes", "Time between crawls", "Stored for a later worker. This desk does not start a crawl on a timer. 15 to 10080.", settings, 15, 10080)}
  {_setting_field("min_delay_seconds", "Minimum delay", "Seconds between requests. Kept at or above 1. SQLite overrides config.yaml for a desk run.", settings, 1, 60)}
  {_setting_field("max_concurrency", "Max concurrency", "Stored for a later worker. This desk fetches one page at a time so the delay stays a single clock.", settings, 1, 2)}
  {_setting_field("max_products", "Max products per run", "Stop after this many newly kept parent products. 1 to 5000.", settings, 1, 5000)}
  {_setting_field("max_pages_per_category", "Product fetches per run", "How many product pages one run may request. Extracted and out-of-scope URLs are skipped. ?page= stays disallowed.", settings, 1, 20)}
  <div class="field">
    <p class="hint">Seeds stay fixed for this assignment.</p>
    <ul class="seeds">
      <li>https://www.safcodental.com/catalog/sutures-surgical-products</li>
      <li>https://www.safcodental.com/catalog/gloves</li>
    </ul>
  </div>
  <button type="submit">Save settings</button>
</form>
"""
    return layout("Settings", "settings", body)


def catalog_from_live(robots: str, pages: dict[str, object]) -> Catalog:
    listings = pages["listings"]
    fetched_products = pages["products"]
    assert isinstance(listings, list)
    assert isinstance(fetched_products, list)
    trace = run_pipeline(listings, fetched_products)
    categories: dict[str, Category] = {}
    for parsed in trace.listings:
        category = Category(
            key=parsed.key,
            title=parsed.title,
            stats=parsed.stats,
            cards=[
                ListingCard(
                    slug=card.slug,
                    name=card.name,
                    manufacturer=card.manufacturer,
                    price=card.price,
                    saved=False,
                )
                for card in parsed.cards
            ],
        )
        categories[parsed.key] = category
    products = {parsed.slug: _product_from_parsed(parsed) for parsed in trace.products}
    for category in categories.values():
        for card in category.cards:
            card.saved = card.slug in products
    return Catalog(
        categories=categories,
        products=products,
        robots=robots,
        steps=trace.steps,
        checkpoints=trace.checkpoints,
    )


def _stored_price(value: object) -> str:
    if value is None or value == "":
        return ""
    return f"${float(value):.2f}"


def load_catalog() -> Catalog | None:
    """Read the saved snapshot. None when the database has not been filled in."""
    connection = connect()
    try:
        category_rows = connection.execute(
            "SELECT key, title, stats FROM categories ORDER BY key"
        ).fetchall()
        if not category_rows:
            return None
        card_rows = connection.execute(
            "SELECT category_key, slug, name, manufacturer, price_amount, saved FROM listing_cards"
        ).fetchall()
        product_rows = connection.execute(
            "SELECT slug, category_key, name, parent_sku, price_amount, availability, "
            "brand, url, description, category_path, image_urls, alternatives, attributes FROM products"
        ).fetchall()
        variant_rows = connection.execute(
            "SELECT sku, product_slug, name, item_group, mfr_number, stock, price_amount, "
            "pack_size, specification, attributes FROM variants"
        ).fetchall()
        step_rows = connection.execute(
            "SELECT stage, subject, outcome, detail FROM agent_steps ORDER BY position"
        ).fetchall()
        checkpoint_rows = connection.execute(
            "SELECT url, status, stage, detail FROM checkpoints ORDER BY url"
        ).fetchall()
    finally:
        connection.close()
    cards_by_category: dict[str, list[ListingCard]] = {}
    for row in card_rows:
        cards_by_category.setdefault(str(row["category_key"]), []).append(
            ListingCard(
                slug=str(row["slug"]),
                name=str(row["name"]),
                manufacturer=str(row["manufacturer"]),
                price=_stored_price(row["price_amount"]),
                saved=bool(row["saved"]),
            )
        )
    categories = {
        str(row["key"]): Category(
            key=str(row["key"]),
            title=str(row["title"]),
            stats=str(row["stats"]),
            cards=cards_by_category.get(str(row["key"]), []),
        )
        for row in category_rows
    }
    variants_by_product: dict[str, list[Variant]] = {}
    for row in variant_rows:
        variants_by_product.setdefault(str(row["product_slug"]), []).append(
            Variant(
                sku=str(row["sku"]),
                name=str(row["name"]),
                group=str(row["item_group"]),
                mfr=str(row["mfr_number"]),
                stock=str(row["stock"]),
                price=_stored_price(row["price_amount"]),
                pack_size=str(row["pack_size"] or ""),
                specification=str(row["specification"] or ""),
                attributes=str(row["attributes"] or "{}"),
            )
        )
    products = {
        str(row["slug"]): Product(
            slug=str(row["slug"]),
            name=str(row["name"]),
            sku=str(row["parent_sku"]),
            price=_stored_price(row["price_amount"]),
            availability=str(row["availability"]),
            category=str(row["category_key"]),
            variants=variants_by_product.get(str(row["slug"]), []),
            brand=str(row["brand"] or ""),
            url=str(row["url"] or ""),
            description=str(row["description"] or ""),
            category_path=str(row["category_path"] or ""),
            image_urls=str(row["image_urls"] or "[]"),
            alternatives=str(row["alternatives"] or "[]"),
            attributes=str(row["attributes"] or "{}"),
        )
        for row in product_rows
    }
    return Catalog(
        categories=categories,
        products=products,
        robots=ROBOTS.read_text(encoding="utf-8"),
        steps=[{key: str(row[key]) for key in row.keys()} for row in step_rows],
        checkpoints=[{key: str(row[key]) for key in row.keys()} for row in checkpoint_rows],
    )


_refresh_guard = threading.Lock()
_running_job_id: int | None = None


def begin_refresh() -> int:
    """Record a running job and fetch in the background. A second call returns that same job."""
    global _running_job_id
    with _refresh_guard:
        if _running_job_id is not None:
            return _running_job_id
        job_id = start_job("live_fetch")
        _running_job_id = job_id
        threading.Thread(
            target=_run_refresh,
            args=(job_id,),
            name="catalog-refresh",
            daemon=True,
        ).start()
        return job_id


def _run_refresh(job_id: int) -> None:
    global CATALOG, _running_job_id
    try:
        robots, pages = crawl(on_progress=lambda message: update_job(job_id, message))
        catalog = catalog_from_live(robots, pages)
        failed = pages["failed"]
        assert isinstance(failed, list)
        for item in failed:
            assert isinstance(item, dict)
            catalog.checkpoints.append(
                {
                    "url": str(item["url"]),
                    "status": "failed",
                    "stage": "retry",
                    "detail": str(item["detail"]),
                }
            )
        counts = upsert_crawl(catalog)
        loaded = load_catalog()
        if loaded is not None:
            CATALOG = loaded
        publish(CATALOG, catalog.steps)
        note = _crawl_note(catalog, int(pages["attempts"]), len(failed))
        finish_job(job_id, "completed", message=note, **counts)
    except FetchBlocked as exc:
        finish_job(job_id, "failed", message=str(exc))
    except Exception as exc:
        finish_job(job_id, "failed", message=str(exc))
    finally:
        with _refresh_guard:
            if _running_job_id == job_id:
                _running_job_id = None


def _crawl_note(catalog: Catalog, attempts: int, failed: int) -> str:
    out_of_scope = sum(1 for item in catalog.checkpoints if item["status"] == "out_of_scope")
    unresolved = sum(1 for item in catalog.checkpoints if item["status"] == "unresolved")
    return (
        f"Fetched {attempts} product pages. Kept {len(catalog.products)}. "
        f"Out of scope {out_of_scope}. Unresolved {unresolved}. Failed {failed}."
    )


def safe_next(value: str) -> str:
    if value in {"/", "/jobs", "/chat", "/settings", "/links", "/gate", "/data"}:
        return value
    if re.fullmatch(r"/jobs/\d+", value):
        return value
    if re.fullmatch(r"/category/[a-z]+", value) or re.fullmatch(r"/product/[a-z0-9-]+", value):
        return value
    return "/jobs"


def not_found() -> str:
    body = """
<h1>That page is not in the snapshot</h1>
<p class="lede">The desk only has the two listings and the three saved products.</p>
<p><a href="/">Back to the desk</a></p>
"""
    return layout("Not in the snapshot", "", body)


CATALOG = build_catalog()


class Handler(BaseHTTPRequestHandler):
    server_version = "CatalogDesk/0.1"

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/schema":
            self._bytes(200, "text/plain; charset=utf-8", SCHEMA_DOC.encode("utf-8"))
            return
        if path == "/api/pipeline":
            self._json(200, _pipeline_payload())
            return
        export = {"/api/export.csv": "csv", "/api/export.json": "json", "/api/export.xlsx": "xlsx"}.get(path)
        if export:
            self._export(export)
            return
        if path == "/api/queries":
            level = normalize_level(get_settings()["user_level"])
            self._json(200, {"level": level, "claude": configured(), "queries": describe(level)})
            return
        if path == "/api/catalog":
            self._json(200, _catalog_payload())
            return
        if path == "/api/rows":
            self._json(200, {"rows": list_catalog_rows()})
            return
        if path == "/api/jobs":
            self._json(200, _jobs_payload())
            return
        if path == "/api/settings":
            self._json(200, _settings_payload())
            return
        product = re.fullmatch(r"/api/product/([a-z0-9-]+)", path)
        if product:
            payload = _product_payload(product.group(1))
            if payload is None:
                self._json(404, {"error": "Unknown product."})
                return
            self._json(200, payload)
            return
        if path.startswith("/api/"):
            self._json(404, {"error": "Unknown API."})
            return
        image = re.fullmatch(r"/images/([a-z0-9-]+\.(?:jpg|jpeg|png|webp))", path)
        if image:
            self._image(image.group(1))
            return
        self._spa(path)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        if length > 8000:
            self._json(413, {"answer": "That question is too long.", "sql": "", "columns": [], "rows": []})
            return
        raw = self.rfile.read(length)
        if path == "/api/refresh":
            try:
                job_id = begin_refresh()
            except Exception as exc:
                self._json(500, {"ok": False, "error": str(exc)})
                return
            self._json(200, {"ok": True, "job_id": job_id, "status": "running"})
            return
        if path == "/api/settings":
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json(400, {"ok": False, "error": "Send JSON settings."})
                return
            if not isinstance(payload, dict):
                self._json(400, {"ok": False, "error": "Send JSON settings."})
                return
            incoming = {key: str(payload.get(key) or "") for key in (*SETTING_BOUNDS, "user_level")}
            try:
                save_settings(incoming)
            except ValueError as exc:
                self._json(400, {"ok": False, "error": str(exc)})
                return
            self._json(200, {"ok": True, **_settings_payload()})
            return
        if path == "/refresh":
            form = parse_qs(raw.decode("utf-8"))
            target = safe_next((form.get("next") or ["/jobs"])[0])
            try:
                begin_refresh()
            except Exception:
                target = "/jobs"
            self._redirect(target)
            return
        if path == "/settings":
            form = parse_qs(raw.decode("utf-8"))
            incoming = {
                key: (form.get(key) or [""])[0]
                for key in (
                    "crawl_interval_minutes",
                    "min_delay_seconds",
                    "max_concurrency",
                    "max_products",
                    "max_pages_per_category",
                    "user_level",
                )
            }
            try:
                save_settings(incoming)
            except ValueError as exc:
                self._html(400, settings_page(error=str(exc)))
                return
            self._redirect("/settings?saved=1")
            return
        if path == "/api/chat":
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json(400, {"answer": "Send JSON with a question field.", "sql": "", "columns": [], "rows": []})
                return
            question = str(payload.get("question") or "")
            query_id = str(payload.get("query_id") or "")
            sku = str(payload.get("sku") or "")
            reply = ask(question, query_id=query_id, sku=sku)
            self._json(200, _reply_payload(reply))
            return
        if path == "/chat":
            form = parse_qs(raw.decode("utf-8"))
            question = (form.get("question") or [""])[0]
            query_id = (form.get("query_id") or [""])[0]
            sku = (form.get("sku") or [""])[0]
            reply = ask(question, query_id=query_id, sku=sku)
            self._html(200, chat_page(question, reply))
            return
        self._html(404, not_found())

    def _spa(self, path: str) -> None:
        index = DIST / "index.html"
        if not index.is_file():
            self._html(503, "<p>React UI is not built. From web/ui run npm install and npm run build.</p>")
            return
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        candidate = (DIST / relative).resolve()
        root = DIST.resolve()
        if candidate.is_file() and (candidate == root or root in candidate.parents):
            kind = _ASSET_TYPES.get(candidate.suffix, "application/octet-stream")
            self._bytes(200, kind, candidate.read_bytes())
            return
        self._bytes(200, "text/html; charset=utf-8", index.read_bytes())

    def _image(self, name: str) -> None:
        path = (ROOT.parent / "data" / "sample" / "images" / name).resolve()
        root = (ROOT.parent / "data" / "sample" / "images").resolve()
        if path.parent != root or not path.is_file():
            self._json(404, {"error": "Image not saved."})
            return
        kind = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
            ".webp": "image/webp",
        }.get(path.suffix, "application/octet-stream")
        self._bytes(200, kind, path.read_bytes())

    def _export(self, kind: str) -> None:
        rows = rows_in_scope(rows_from_catalog(CATALOG))
        if kind == "csv":
            payload, mime, name = csv_bytes(rows), "text/csv; charset=utf-8", "safco-catalog.csv"
        elif kind == "json":
            payload, mime, name = json_bytes(rows), "application/json; charset=utf-8", "safco-catalog.json"
        else:
            payload = xlsx_bytes(rows)
            mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            name = "safco-catalog.xlsx"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def _redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.end_headers()

    def _json(self, status: int, payload: dict) -> None:
        self._bytes(status, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"))

    def _html(self, status: int, page: str) -> None:
        self._bytes(status, "text/html; charset=utf-8", page.encode("utf-8"))

    def _bytes(self, status: int, content_type: str, payload: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")


def _catalog_payload() -> dict:
    settings = get_settings()
    level = normalize_level(settings["user_level"])
    return {
        "snapshot": "15 Oct 2026",
        "level": level,
        "cadence": _cadence(),
        "claude": configured(),
        "categories": [
            {
                "key": category.key,
                "title": category.title,
                "stats": category.stats,
                "cards": [
                    {
                        "slug": card.slug,
                        "name": card.name,
                        "manufacturer": card.manufacturer,
                        "price": card.price,
                        "saved": card.saved,
                    }
                    for card in category.cards
                ],
            }
            for category in CATALOG.categories.values()
        ],
        "products": [
            {
                "slug": product.slug,
                "name": product.name,
                "sku": product.sku,
                "price": product.price,
                "availability": product.availability,
                "category": product.category,
                "variants": len(product.variants),
                "image": next(iter(_json_list(product.image_urls)), ""),
            }
            for product in CATALOG.products.values()
        ],
        "gate": {
            "recommendation": "yes",
            "crawl_allowed": "",
            "facts": [
                "Seed paths are allowed.",
                "Listing ?page= and ?p= are disallowed.",
                "Sitemap lists 3,939 product URLs and 282 category URLs.",
                "No Crawl-delay. One group: User-agent: *.",
            ],
            "robots": CATALOG.robots,
            "sitemap_products": SITEMAP_PRODUCTS,
            "sitemap_categories": SITEMAP_CATEGORIES,
            "terms": "Terms of Sale do not mention bots. Unauthorized use of site material is prohibited. /terms-of-use is a 404.",
        },
    }


def _product_payload(slug: str) -> dict | None:
    product = CATALOG.products.get(slug)
    if product is None:
        return None
    category = CATALOG.categories[product.category]
    return {
        "slug": product.slug,
        "name": product.name,
        "sku": product.sku,
        "price": product.price,
        "availability": product.availability,
        "category": product.category,
        "category_title": category.title,
        "brand": product.brand,
        "url": product.url,
        "description": product.description,
        "category_path": product.category_path,
        "images": _json_list(product.image_urls),
        "alternatives": _json_list(product.alternatives),
        "variants": [
            {
                "sku": row.sku,
                "name": row.name,
                "group": row.group,
                "mfr": row.mfr,
                "stock": row.stock,
                "price": row.price,
                "pack_size": row.pack_size,
                "specification": row.specification,
            }
            for row in product.variants
        ],
    }


def _json_list(value: str) -> list[object]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    if isinstance(parsed, list):
        return parsed
    return []


def _pipeline_payload() -> dict[str, object]:
    from config import load_config

    config = load_config()
    steps = CATALOG.steps or list_steps()
    checkpoints = CATALOG.checkpoints or list_checkpoints()
    return {
        "stages": [
            {"id": "navigator", "role": "Chooses the next allowed URLs: product links on the seed and one level of child category pages, then the rest of the sitemap. Records ?page= and ?p= links without fetching them."},
            {"id": "classifier", "role": "Labels each fetched page as listing, product, or other from the URL and from JSON-LD, masterData, or the Algolia hit list."},
            {"id": "extractor", "role": "Reads parent fields from JSON-LD and breadcrumbs, and child SKUs, pack size, stock, and price from masterData."},
            {"id": "validator", "role": "Requires a name and a SKU, drops a repeated child SKU, and keeps a product only when the page is in sutures or gloves. When the body class and the breadcrumb each name a seed and they differ, asks the model. An out-of-scope breadcrumb is rejected without a model."},
            {"id": "retry", "role": "Skips a product page that has neither masterData nor JSON-LD. A page with only JSON-LD is kept when it is in scope. A scope model that does not choose leaves the page unresolved."},
        ],
        "steps": steps,
        "checkpoints": checkpoints,
        "schema": [{"name": key, "description": note} for key, note in COLUMNS],
        "exports": ["/api/export.csv", "/api/export.json", "/api/export.xlsx"],
        "production": [
            {"title": "Rate limiting", "body": f"Live fetches wait at least {config['min_delay_seconds']} seconds. Settings can raise that delay and cannot set it below 1."},
            {"title": "Retries", "body": "429, 5xx, and timeouts are retried a few times. Each attempt waits on the same delay and passes the robots check. Other 4xx responses are not retried."},
            {"title": "Checkpointing", "body": "Extracted and out-of-scope URLs are skipped on the next run. Unresolved and failed URLs are fetched again."},
            {"title": "Logging", "body": "Pipeline stages are stored in agent_steps and written to data/sample/pipeline-log.jsonl. Secrets are not logged."},
            {"title": "Deduplication", "body": "The validator keeps the first row for each child SKU."},
            {"title": "Idempotency", "body": "A crawl upserts the parents it kept. A URL checkpointed out of scope drops that parent and its child SKUs. Other parents stay. Re-extracting a parent replaces only that parent's child SKUs."},
            {"title": "Config", "body": "Seeds, delay, and the user agent live in config.yaml. Crawl caps live in SQLite settings."},
            {"title": "Secrets", "body": "CLAUDE_API_KEY stays in .env. A scope conflict sends the class tokens and the breadcrumb, not the page HTML. The key is not written to the pipeline log."},
            {"title": "Deployment", "body": "This desk is one process and a SQLite file. A full-site run fetches child category pages under the seeds, then resumes through products.xml. The checkpoint table is the queue."},
        ],
    }


def _jobs_payload() -> dict:
    jobs = []
    for job in list_jobs():
        jobs.append({key: job[key] for key in job.keys()})
    return {"jobs": jobs}


def _settings_payload() -> dict:
    settings = get_settings()
    return {
        "settings": settings,
        "bounds": {key: {"min": low, "max": high} for key, (low, high) in SETTING_BOUNDS.items()},
        "levels": list(LEVELS),
        "claude": configured(),
        "cadence": _cadence(),
    }


_ASSET_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json",
    ".woff2": "font/woff2",
}


def _reply_payload(reply: Reply) -> dict:
    return {
        "answer": reply.answer,
        "sql": reply.sql,
        "columns": reply.columns,
        "rows": reply.rows,
        "query_id": reply.query_id,
    }


def open_catalog() -> Catalog:
    """Use the saved snapshot when one exists. Seed the fixtures only on an empty database."""
    global CATALOG
    rebuild = ensure_database()
    ensure_settings()
    loaded = load_catalog()
    if loaded is None or rebuild:
        reload_catalog(CATALOG)
        return CATALOG
    CATALOG = loaded
    return CATALOG


def main() -> None:
    open_catalog()
    sutures = CATALOG.categories["sutures"]
    gloves = CATALOG.categories["gloves"]
    print(
        f"Loaded {len(sutures.cards)} suture cards, {len(gloves.cards)} glove cards, "
        f"{sum(len(p.variants) for p in CATALOG.products.values())} child SKUs.",
        flush=True,
    )
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Catalog desk at http://{HOST}:{PORT}/", flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
