# Safco catalog desk

A local prototype that extracts Safco Dental products from two categories, stores one row per child SKU, and can be queried or exported. Listing pagination is left unfetched because `robots.txt` disallows `?page=` and `?p=`.

- [Architecture overview](#architecture-overview)
- [Why you chose your approach](#why-you-chose-your-approach)
- [Robots and pagination](#robots-and-pagination)
- [Agent responsibilities](#agent-responsibilities)
- [Setup and execution](#setup-and-execution)
- [Sample output schema](#sample-output-schema)
- [Limitations](#limitations)
- [Failure handling](#failure-handling)
- [How you would scale to full-site crawling](#how-you-would-scale-to-full-site-crawling)
- [How you would monitor data quality](#how-you-would-monitor-data-quality)

## Architecture overview

```
config.yaml + settings
        │
        ▼
   navigator ── child category URLs under each seed, then products.xml
        │         records ?page= / ?p= and does not fetch them
        ▼
   classifier ── listing | product | other
        │
        ▼
   extractor ── JSON-LD, breadcrumbs, window.masterData
        │
        ▼
   validator ── name, SKU, scope gate, first row wins
        │         model only when class and breadcrumb name different seeds
        ▼
   retry ── skip a broken page, or keep a JSON-LD parent when masterData is missing
        │
        ▼
   SQLite (data/catalog.db)  +  data/sample/catalog.csv|json|xlsx
```

Fetch I/O is separate from the pipeline. Every live request goes through the robots check and a delay. The stages only see HTML that was already fetched, or the saved fixtures in `tests/fixtures/`. The scope model receives the body-class tokens and the breadcrumb text. It does not receive the page.

## Why you chose your approach

The product pages are regular. Parent name, brand, SKU, price, availability, description, and images are in JSON-LD. Every child SKU is already in `window.masterData`, including rows the on-page pager does not draw. A model is not required to read that.

The crawl calls a model in one place. When the body class and the breadcrumb each name a seed and they name different seeds, the validator asks Claude to choose `sutures`, `gloves`, or `out_of_scope`. A breadcrumb outside both seeds is rejected by the rule, including when the body class matches. No API key, or a reply that is not one of those three values, leaves the page unresolved. Page HTML is not sent. Ask can still send the question text to Claude to pick a listed SQL id. That call does not see page HTML either.

## Product extraction

**How.** `extract_product` in `web/extract.py` reads one saved or fetched page and returns a parent plus its child rows. JSON-LD `Product` supplies the parent name, brand, SKU, price, availability, description, and image URLs. The breadcrumb supplies the category hierarchy, with Home and the product name left off. `window.masterData` supplies each child SKU, manufacturer part number, pack size, stock label, specification, and attributes. Recommendation cards already in the HTML supply alternative name and URL from the rendered `alt` or `title`, not from the Alpine binding.

**Why.** Those markers are in the HTML before any extra request. Perma Sharp draws 20 rows and stores all 48 children in `masterData`. Parsing that blob is the variant grid, so the extractor does not click the on-page pager.

**Tradeoff.** Pack size is a regex such as `12/box`, not a separate field. A child with no count stays blank. Recommendation cards are filled in the browser, so `alternatives` is `[]` on every checked-in row. Algolia is not called. Listing page 2 and later use `?page=`, which `robots.txt` disallows, so those URLs are recorded and not fetched.

## Structured output

**How.** `web/export_catalog.py` writes one row per child SKU to CSV, JSON, and xlsx. The column list is `COLUMNS` in that module. The SQLite view `catalog_rows` in `web/db.py` uses the same grain. A parent with no child rows still emits one row, and `sku` is the parent SKU. The files are `data/sample/catalog.csv`, `catalog.json`, and `catalog.xlsx`. The Pipeline page lists the columns.

**Why.** A flat row exports and answers a query without a join. One schema serves the database, the three files, and the Data page.

**Tradeoff.** Parent fields repeat on every child. The xlsx file is written with the stdlib as one sheet of inline strings, so the desk has no spreadsheet dependency and the file is not a formatted workbook.

## Pipeline

The prompt lists a navigator, a page classifier, an extractor, a validator, and retry. Those are the five stages in `web/pipeline.py`.

**How.** The navigator selects the next allowed URLs: product links on the two seed pages, then product links on one level of child category pages under those seeds, then the rest of `products.xml`. It records `?page=` and `?p=` links and does not fetch them. The classifier labels a page `listing`, `product`, or `other` from the URL and from `masterData`, JSON-LD `Product`, or the Algolia hit list. The extractor fills the fields above. The validator requires a name and a SKU, keeps the first copy of a product slug or child SKU, and keeps the page only when `web/scope.py` says it is sutures or gloves. When the body class and the breadcrumb each name a seed and they differ, the validator calls the model. The retry stage skips a page that has neither marker, and keeps a parent row when JSON-LD is present, `masterData` is not, and the page is in scope. A scope reply that does not choose leaves the page unresolved. The trace is on `/pipeline` and in `data/sample/pipeline-log.jsonl`.

**Why.** Page type, child SKUs, and an out-of-scope breadcrumb are already in the HTML. A model that picked URLs could request a disallowed link, so navigation stays a rule. The model is used where the two scope signals disagree.

**Tradeoff.** The classifier is rules over URLs and markers. A new template needs a code change. The retry note names the repair, JSON-LD for the parent and `masterData` for the children, instead of asking a model to invent a selector. A crawl with no class-versus-breadcrumb disagreement, or with no `CLAUDE_API_KEY`, does not call the model.

## Production design

**How.** `web/live.py` fetches through a `Fetcher`. A catalog URL is not requested until `robots.txt` has loaded. The policy and a final redirect are both checked, and `?page=` / `?p=` are refused even when the policy allows them. A 429 or 503 waits for a parsed `Retry-After`, capped at 60 seconds and never shorter than the configured delay. Other 5xx responses and timeouts retry up to three times on that delay. Any other HTTP error is not retried. A robots block is not requested. `web/crawl.py` fetches the two seeds, then one level of `/catalog/` URLs under each seed, and queues product links from those pages ahead of `products.xml`. `max_products` counts newly kept parents. `max_pages_per_category` counts product-page attempts in one run. Child category fetches sit outside that cap. URLs checkpointed `extracted` or `out_of_scope` are skipped. `unresolved` and `failed` are fetched again. A crawl upserts the parents it kept. A URL checkpointed `out_of_scope` deletes that parent and its child SKUs. Other parents stay. Publishing drops a row whose breadcrumb fails `scope_path`, or whose `category_key` does not match that breadcrumb. Starting the desk does not rewrite `data/sample/`. `config.yaml` supplies the origin, user agent, seeds, and default delay. SQLite settings override the delay and the caps for a desk run. `CLAUDE_API_KEY` stays in `.env`. The pipeline log does not contain it.

**Why.** Listing pages do not include the product grid until JavaScript runs, and `?page=` is disallowed. `products.xml` is the allowed list of product URLs. Membership is known only after the product page is read. Remembering out-of-scope URLs is what lets the next run move past them.

**Tradeoff.** The first Refresh can keep zero new parents and only checkpoint rejects. The job message reports fetched, kept, out of scope, unresolved, and failed. Concurrency stays at 1. `max_concurrency` and `crawl_interval_minutes` are stored for a later worker and do not start extra fetches or a timer. A second worker would need the same limiter, or the delay would no longer be one gap between requests.

## Robots and pagination

`https://www.safcodental.com/robots.txt` is one group, `User-agent: *`. The copy from 2026-10-15 is `docs/recon/robots_2026-10-15.txt`. The two rules that block the listing pager are:

```
Disallow: /*?p=
Disallow: /*?page=
```

Seed paths (`/catalog/sutures-surgical-products`, `/catalog/gloves`), one level of child category under each seed, and `/product/<slug>` are allowed. `products.xml` lists 3,939 product URLs, none with a query string. `catalog.xml` lists 282 category URLs and is not the crawl queue.

The pager on the rendered listing is the disallowed form. Sutures page 1 says "Items 1-12 of 55" and links `?page=1` through `?page=5`. Gloves page 1 says "Items 1-12 of 93" and links `?page=1` through `?page=7`. The navigator records those hrefs and does not fetch them. `Fetcher` refuses `?page=` and `?p=` even when the parsed policy would allow the URL, and it refuses a redirect that lands on one. There is no load-more control. `?product_list_limit=` does not appear on the pages that were opened.

Page 1 has a second gap. The fixtures in `tests/fixtures/listing_sutures.html` and `listing_gloves.html` are the DOM after JavaScript, and each has 12 product cards. A live fetch of the same seed URL does not include that grid. `data/sample/pipeline-log.jsonl` records 0 cards on both seeds: sutures returned 1 product URL and 8 child category URLs, gloves returned 1 product URL and 6 child category URLs. The five product pages fetched after that (Lucitone, Lucitone Liquid, Ultra silver alloy, Valiant Ph.D., Valiant) were out of scope. Child category HTML from a raw fetch can be just as empty of product links, so the queue falls through to `products.xml`.

Algolia is what fills the grid in the browser. Calling it, or requesting `?page=`, would get past the disallow. This desk does neither. Parents that never appear as an allowed href are reached by walking the sitemap and keeping a page when the breadcrumb is sutures or gloves. At 3 seconds per URL that walk is multi-hour, and a run capped by `max_pages_per_category` can finish with zero newly kept parents. The checkpoint table is what lets the next run skip the rejects.

## Agent responsibilities

| Stage | What it decides |
| --- | --- |
| Navigator | Chooses the next allowed URLs: products on the seed pages, products on one level of child category pages, then the rest of the sitemap. Records pagination URLs as disallowed. |
| Classifier | Labels a page from its URL and from `masterData`, JSON-LD `Product`, or the Algolia hit list. |
| Extractor | Fills the catalog fields from those markers. |
| Validator | Requires a product name and a SKU. Drops a repeated product slug or child SKU. Keeps the page only when the scope gate returns sutures or gloves. Calls the model when the body class and the breadcrumb name different seeds. |
| Retry | Skips a page with neither marker and continues the run. Keeps a parent row when JSON-LD is present, `masterData` is not, and the page is in scope. A scope model that does not choose leaves the page unresolved. |

The latest trace is on the Pipeline page (`/pipeline`) and in `data/sample/pipeline-log.jsonl`.

## Setup and execution

Python 3.12. The sample files are already in `data/sample/`. From the repo root, the offline check is:

```bash
python3 -m unittest discover -s tests -v
```

The desk needs a built UI. `web/ui/dist` is not in git. Without the build, `GET /` returns 503.

```bash
cd web/ui && npm ci && npm run build
cd ../.. && python3 web/server.py
```

Open http://127.0.0.1:8765/

A fresh database loads the saved HTML for both category pages and three product sheets (Surgifoam, Perma Sharp sutures, Lavender Nitrile). That seed does not overwrite `data/sample/`. `python3 web/sheets.py` fetches listing-page-1 product URLs that are in the saved listing HTML and saves each photo under `data/sample/images/`. Data, Pipeline, and the product pages read the database. CSV, JSON, and Excel download from the Data page.

Refresh is a live crawl. It fetches the seed URLs, one level of child category pages under those seeds, then the next slice of `products.xml`. It skips URLs already extracted or out of scope. It does not request `?page=` or `?p=`. A run that keeps nothing still finishes, and the job line says how far the cursor moved. This process does not schedule crawls.

Optional: put `CLAUDE_API_KEY=` in `.env` so a scope disagreement, and Ask, can call Claude. The key stays in `.env` and is not logged.

## Sample output schema

Grain is one row per child SKU. The same columns are in `catalog_rows`, `data/sample/catalog.csv`, `catalog.json`, and `catalog.xlsx`.

| Column | Meaning |
| --- | --- |
| product_name | Parent product name |
| brand | Brand or manufacturer |
| parent_sku | Parent SKU from JSON-LD |
| sku | Child SKU. Parent SKU when a page had no child rows |
| variant_name | Child name |
| mfr_number | Manufacturer part number |
| item_group | Variant group, such as needle type |
| category_key | `sutures` or `gloves` |
| category_title | Listing title |
| category_path | Breadcrumb under Home, excluding the product name |
| url | Public product URL |
| variant_price | Child price in USD, blank if absent |
| parent_price | Parent JSON-LD price |
| pack_size | Parsed from the child specification or name, such as `12/box` |
| stock | Child stock label |
| availability | Parent availability |
| description | Parent description |
| specification | Child specification text |
| attributes | JSON for the child row |
| image_urls | JSON list of parent image URLs |
| alternatives | JSON list of recommendation cards (`name`, `url`). `[]` in the checked-in sample |
| product_slug | URL slug |

The checked-in files hold 285 child rows across 40 parent products: 28 labeled sutures and 12 labeled gloves. Of those, 160 rows and 25 parents have a breadcrumb `scope_path` accepts. That slice is 13 parents under Sutures & surgical products (96 rows, including Surgifoam and Perma Sharp sutures) and 12 parents under Dental Exam Gloves (64 rows, including Lavender Nitrile). On those rows `category_key` matches the breadcrumb.

The other 125 rows are still in the files. 67 rows are one parent, Clearance Item, path `Clearance / Safco Clearance`. 58 rows are 14 parents under Acrylics and reline (13 reline materials, including Coe-Soft, GC Reline, and Kooliner, plus Jet Denture Repair Acrylic) stored with `category_key` sutures. Publish would drop both groups. These files were not rewritten after that gate, so `test_checked_in_sample_stays_inside_the_two_categories` fails on the Clearance path.

`alternatives` is `[]` on all 285 rows. `pack_size` is blank on 155 rows. `item_group` is blank on 94 rows. 25 photos are under `data/sample/images/` and served at `/images/<slug>.jpg`. Safco returns JPEG for these catalog photos, so the files are `.jpg`. Listing page 2 is not in the sample.

## Limitations

- Listing page 2 and later use `?page=`, which `robots.txt` disallows (`/*?page=` and `/*?p=`). Sutures links pages 1–5 (55 parents on the rendered page). Gloves links pages 1–7 (93 parents). Those URLs are recorded and not fetched. Algolia is not called. See [Robots and pagination](#robots-and-pagination).
- A live fetch of either seed returns the category shell without the product grid (0 cards in `data/sample/pipeline-log.jsonl`). The 12-card fixtures are post-JavaScript DOM. Child category pages from a raw fetch can be empty of product links too, so the rest of each category is reached only through `products.xml` (3,939 `/product/` URLs) and the scope gate. The in-scope slice of this sample is 13 of about 55 suture parents and 12 of about 93 glove parents.
- A page with neither a category class nor a breadcrumb is unresolved. It is not stored, and it is fetched again on a later run.
- The scope model runs only when the body class and the breadcrumb each name a seed and they differ. A crawl that never hits that case, or that has no `CLAUDE_API_KEY`, does not call Claude for scope.
- The checked-in files still hold 125 rows the scope gate would drop: Clearance Item (67 rows) and 14 acrylic and reline parents (58 rows) labeled sutures. `test_checked_in_sample_stays_inside_the_two_categories` fails on `Clearance / Safco Clearance`.
- `alternatives` is `[]` on all 285 rows. Those cards are filled in the browser. Algolia is not called.
- Pack size is parsed from text such as `12/box`. 155 rows have no count in the name or specification, so `pack_size` is blank. `item_group` is blank on 94 rows, where `masterData` has no group.

## Failure handling

- A product page missing both `masterData` and JSON-LD is skipped. The job continues.
- A page with JSON-LD and no `masterData` is kept as a parent row when it is in scope, and marked recovered.
- A breadcrumb outside both seeds is checkpointed `out_of_scope` even when the body class matches. That parent and its child SKUs are removed if they were stored earlier. The checkpoint stays, so the next run skips the URL. A path that starts with Clearance or Equipment stays out of scope.
- When the body class and the breadcrumb name different seeds, the model chooses. No key, or an unusable reply, checkpoints the page `unresolved` and does not store it. The request does not include the page HTML or the API key in the log.
- 429 and 503 honor `Retry-After`, capped at 60 seconds. Other 5xx responses and timeouts use the configured delay. A robots mismatch is not retried and is not requested. One failed URL does not discard the others.
- The validator rejects a row with no name, and a second copy of a slug or child SKU.
- Publish drops a row that fails `scope_path`. A later export leaves Clearance and Acrylics and reline out of `data/sample/`. The files checked in now still contain both, because they were written before that gate.

## How you would scale to full-site crawling

1. Keep the robots check and the delay on every request. Do not add a switch that turns robots off.
2. Fetch the two seed URLs from `config.yaml`, then one level of child category URLs under each seed. `catalog.xml` is not the crawler's category list.
3. Queue product links from those pages, then the remaining `products.xml` slugs. Skip URLs already `extracted` or `out_of_scope`.
4. Run the classifier, extractor, and scope gate. Fetch `unresolved` and `failed` again.
5. Upsert by slug. Replace child SKUs for that parent only. Delete a parent when this run checkpoints its URL `out_of_scope`.
6. Keep concurrency at 1 until the limiter is shared. The delay default is 3 seconds, and settings cannot put it under 1. One run stops at `max_products` newly kept parents and at `max_pages_per_category` product fetches. Child category fetches are extra.
7. Publish CSV, JSON, and Excel from the rows that pass `scope_path`.

Raising `max_pages_per_category` continues that queue, so discovered links are not stuck behind sitemap order. A live category fetch currently contributes almost no product hrefs, and `?page=` stays unfetched, so most parents are still only reached by walking `products.xml`. At 3 seconds per URL, the 3,939 product sitemap is a multi-hour run. The checkpoint table is what makes that resumable. The in-scope slice here is 13 suture parents and 12 glove parents, short of the 55 and 93 on the rendered listing pages.

## How you would monitor data quality

This prototype checks:

- An accepted row has a product name and a SKU.
- Child SKU and product slug are unique inside one run.
- Publish keeps a row only when `scope_path` is in scope and `category_key` matches.

A later worker would alert when `brand`, `variant_price`, or `stock` is blank on a newly extracted row, or when a re-crawl changes a SKU's price or stock against the stored row. Those alerts do not run in this process. The Pipeline page is the run log. `data/sample/pipeline-log.jsonl` is the same trace on disk.

## Config, secrets, deployment

- `config.yaml` holds the origin, user agent, default delay, and the two seed URLs. `MIN_DELAY_SECONDS` in the environment overrides the delay in that file. A desk run then uses the SQLite delay and caps.
- Crawl interval and concurrency are stored in Settings for a later worker. This process does not schedule crawls and does not fetch two pages at once.
- `CLAUDE_API_KEY` is read from `.env` and is never written to the log or the database.
- The desk is `python3 web/server.py` after `npm ci && npm run build` in `web/ui`. It uses `data/catalog.db`, which is not committed. The submitted sample is `data/sample/`.
- A production deploy is the same pipeline in a worker, with the checkpoint table as the queue. The desk stays a reader of that database.
