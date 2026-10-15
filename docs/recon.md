# Safco recon — 2026-10-15

Manual browser session against `https://www.safcodental.com`. The verbatim robots body is [`docs/recon/robots_2026-10-15.txt`](recon/robots_2026-10-15.txt) (705 bytes, `Content-Type: text/plain;charset=UTF-8`, HTTP 200). A second fetch with an identifiable client reproduced those bytes. Catalog URLs whose query string matches a `Disallow` rule were not requested.

Wildcard rules below are read the way protego will read them in Phase 3: `*` matches any characters, `$` anchors the end. There is no `Allow` line in the file.

## robots.txt

One group only: `User-agent: *`. A later crawler user-agent falls through to this group. No other `User-agent` block is present.

`Crawl-delay` is absent.

```
User-agent: *
Disallow: /index.php/
Disallow: /checkout/
Disallow: /app/
Disallow: /lib/
Disallow: /*.php$
Disallow: /pkginfo/
Disallow: /report/
Disallow: /var/
Disallow: /customer/
Disallow: /sendfriend/
Disallow: /productalert/
Disallow: /review/
Disallow: /*SID=
Disallow: /*?price*
Disallow: /*?sortBy*
Disallow: /*search?*
Disallow: /*?pageType=*
Disallow: /catalogsearch/
Disallow: /*?manufacturer_name*
Disallow: /customer/account/forgotpassword/
Disallow: /reset-password-confirmation/
Disallow: /catalog/product/view/id/*
Disallow: /catalog/category/view/id/*
Disallow: /*q=__empty
Disallow: /*?p=
Disallow: /*?page=

Sitemap: https://www.safcodental.com/sitemap.xml
```

### Paths we care about

| URL | Result |
| --- | --- |
| `/catalog/sutures-surgical-products` | Allowed. No rule matches. |
| `/catalog/gloves` | Allowed. No rule matches. |
| Subcategory paths such as `/catalog/gloves/nitrile-gloves` and `/catalog/sutures-surgical-products/sutures` | Allowed. No rule matches. |
| Public product URLs `/product/<slug>` | Allowed. No rule matches. |
| `/catalog/product/view/id/*` | Disallowed. This is the Magento id form. Live product links do not use it. |
| `/catalog/category/view/id/*` | Disallowed. Live category links do not use it. |
| Listing pagination `?page=` | Disallowed by `/*?page=`. This is the parameter the live listing uses. |
| `?p=` | Disallowed by `/*?p=`. Not used by the pages opened in this session. |
| `?product_list_limit=` | No matching rule. Not present on the pages opened in this session. |
| `?price`, `?sortBy`, `?manufacturer_name`, `?pageType` | Disallowed. Sort and filter links that use these must not be fetched. |

Seed paths and typical product paths are allowed. Paginated listing URLs are not.

## Terms

The footer has no "Terms of Use" link. `https://www.safcodental.com/terms-of-use` returns 404.

The legal pages that are linked:

- [Terms of Sale](https://www.safcodental.com/terms-of-sale), effective August 15, 2024. Page title "Term of Sale". It covers returns, shipping, payment, and professional-account limits. It does not mention bots, crawling, or scraping. The copyright paragraph says Safco is the owner of the intellectual property on the site and that "any unauthorized use of any material on this website or the appearance of this website is strictly prohibited."
- [Privacy Policy](https://www.safcodental.com/privacy-policy). It describes personal information, cookies, and Google Analytics. It does not mention bots, crawling, or scraping. The closing line says use of the site binds the reader to "the terms of use applicable to you." No separate terms-of-use document was found.

## Sitemap

`https://www.safcodental.com/sitemap.xml` is a sitemap index, `lastmod` `2026-10-15T13:00:06+00:00`. Child sitemaps:

- `https://www.safcodental.com/products.xml`
- `https://www.safcodental.com/landing.xml`
- `https://www.safcodental.com/index.xml`
- `https://www.safcodental.com/blog.xml`
- `https://www.safcodental.com/catalog.xml`
- `https://www.safcodental.com/endodontics.xml`
- `https://www.safcodental.com/equipment.xml`
- `https://www.safcodental.com/laboratory.xml`
- `https://www.safcodental.com/orthodontics.xml`
- `https://www.safcodental.com/patient-giveaways-gifts.xml`
- `https://www.safcodental.com/custom-personalized.xml`

`products.xml` contains 3,939 URLs, all `https://www.safcodental.com/product/<slug>`, none with a query string, none under `/catalog/product/view/id/`.

`catalog.xml` contains 282 URLs, including both seeds and their subcategories, none with a query string.

The homepage marketing copy says "Over 20,000+ Products Available." The product sitemap count is parent `/product/` URLs. Variant SKUs live on those pages (see below), so the two numbers can both be true. This session did not prove the sitemap lists every parent product.

Sitemap discovery is the allowed way to reach products past listing page 1, because `?page=` is disallowed.

## Listing pages

Both seeds are Hyvä/Magento category pages. The product grid is Algolia InstantSearch (`Blackbird_HyvaAlgoliaSearch`), already rendered in the HTML of page 1. Stats read "Items 1-12 of 55" (sutures) and "Items 1-12 of 93" (gloves).

JSON-LD on the listing is `ItemList`, not `Product`. Each card also carries microdata `http://schema.org/Product` plus `AggregateOffer` (`lowPrice`, `highPrice`, `priceCurrency`).

Selectors, from the rendered DOM:

- Card: `li.ais-Hits-item`
- Product link: `li.ais-Hits-item a.h-full.result` → `/product/<slug>`
- Name: `h3[itemprop="name"]`
- Manufacturer: `.manufacturer-name`
- Price: `.price-product-label`. Visible text is "As low as" plus a dollar amount. `meta[itemprop="lowPrice"]` and `meta[itemprop="highPrice"]` are in the card.
- Pagination: `.ais-Pagination a.ais-Pagination-link`, real `href`s. Sutures uses `?page=1` through `?page=5` and a Next link to `?page=2`. Gloves uses `?page=1` through `?page=7`. These URLs are disallowed. They were not opened.
- Subcategories: plain anchors under the seed path, for example `/catalog/sutures-surgical-products/sutures` and `/catalog/gloves/nitrile-gloves`. No query string.

`?p=` and `?product_list_limit=` do not appear. There is no load-more button. Further listing pages are ordinary links that robots.txt disallows.

Listing price and the first 12 products are in the server-rendered HTML. Algolia assets load from the Safco origin and from `algolia.net`. Do not call Algolia, and do not follow `?page=`, to get around the disallow.

## Product pages

All three saved products are grouped products (`body` class `page-product-grouped`).

JSON-LD `Product` is present, with `sku`, `name`, `brand`, and a single `Offer` (price, `priceCurrency`, `availability`). That offer is the parent price, not one offer per child SKU. A `BreadcrumbList` is also present.

Variants are not a `<select>`, swatch, or HTML `<table>`. They are a CSS grid (`.table-grid-pdp`) filled by Alpine from embedded JSON:

- `window.masterData` is a JSON string keyed by child SKU.
- Each child includes `name`, `itemgroup`, `manufacturer_part_number`, `stock_availability`, `stock_availability_label`, and `price` HTML that contains `data-price-amount`. Tier prices are in the same blob.
- `initGroupedOptions()` renders the grid. Columns: Item #, Product Name, Mfr #, Description, Stock Availability, Price, Qty.
- The first page of rows is in the DOM (20 on Perma Sharp, via Toast UI pager `a.tui-page-btn` with `href="#"`). The remaining children are already in `masterData`. `window.productConfig` holds `page_limit`, `paginate`, and `total`. No extra navigation is required to read every variant.

Price and a stock label are in that embedded JSON. After load, the page also `fetch`es `https://www.safcodental.com/safcoinventory/index/GetInventorySource/`. That path is not disallowed. The request method and body were not recorded. The embedded `stock_availability_label` is enough to read stock from the saved HTML.

Observed products:

- Surgifoam (`PFPJK`): 2 child SKUs, both "In stock". JSON-LD price `214.49`. Category path class includes `categorypath-catalog-sutures-surgical-products`.
- Lavender Nitrile (`DRCAS`): 5 child SKUs (sizes, for example "x-small 250/box"). JSON-LD price `38.99`, availability InStock. Breadcrumb: Home / Dental Supplies / Dental Exam Gloves / Nitrile gloves / Lavender Nitrile. `itemgroup` is empty; the size is in the child name.
- Perma Sharp sutures (`PFEDB`): 48 child SKUs in `masterData`, grouped by needle `itemgroup` (for example "C-6 needle (3/8 circle, premium reverse cut)"). JSON-LD price `73.99`. 20 rows rendered. Category path class includes `categorypath-catalog-sutures-surgical-products`.

## Fixtures

Rendered HTML, saved from the DOM after load:

| File | URL | Role |
| --- | --- | --- |
| `tests/fixtures/listing_sutures.html` | `https://www.safcodental.com/catalog/sutures-surgical-products` | Listing page 1, 12 cards, subcategory links, disallowed `?page=` links present in the HTML |
| `tests/fixtures/listing_gloves.html` | `https://www.safcodental.com/catalog/gloves` | Listing page 1, 12 cards, subcategory links |
| `tests/fixtures/product_a.html` | `https://www.safcodental.com/product/surgifoam-reg` | Grouped product, 2 variants, sutures |
| `tests/fixtures/product_b.html` | `https://www.safcodental.com/product/lavender-nitrile` | Grouped product, 5 variants, gloves |
| `tests/fixtures/product_variants.html` | `https://www.safcodental.com/product/perma-sharp-reg-sutures` | Grouped product, 48 variants in `masterData` |

## Recommendation

yes

Seed category paths and `/product/<slug>` URLs are allowed, and both are listed in the sitemap. Listing pagination (`?page=`, and `?p=` if it appears) stays disallowed and must not be fetched. The Terms of Sale copyright sentence does not name automated access; it is quoted above so it can be weighed separately.

crawl allowed:
