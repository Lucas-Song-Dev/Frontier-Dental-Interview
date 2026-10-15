"""Navigator, classifier, extractor, validator, and retry. Each step is recorded."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from extract import (
    ParsedListing,
    ParsedProduct,
    ParsedVariant,
    classify_page,
    extract_listing,
    extract_product,
    pagination_links,
)
from live import _product_slugs, child_category_urls
from scope import Scope, scope_page

Decide = Callable[[tuple[str, ...], str], str | None]

REPAIR = (
    "Selector repair: parent fields are in script[type=application/ld+json] "
    "@type Product. Child SKUs, pack size, stock, and price are in window.masterData. "
    "Listing pagination uses ?page= and must stay unfetched."
)


@dataclass
class Run:
    listings: list[ParsedListing] = field(default_factory=list)
    products: list[ParsedProduct] = field(default_factory=list)
    steps: list[dict[str, str]] = field(default_factory=list)
    checkpoints: list[dict[str, str]] = field(default_factory=list)


def run(
    listings: list[tuple[str, str, str]],
    products: list[tuple[str, str, str, str]],
    decide: Decide | None = None,
) -> Run:
    """Classify and extract pages that were already fetched. Pagination URLs are recorded only."""
    from claude import decide_scope

    result = Run()
    seen_sku: set[str] = set()
    seen_slug: set[str] = set()
    chooser = decide if decide is not None else decide_scope
    for key, url, html_text in listings:
        result.steps.extend(_navigator(url, html_text))
        kind = classify_page(url, html_text)
        result.steps.append(_step("classifier", url, kind, "Labeled from the URL and page markers."))
        parsed = extract_listing(html_text, key)
        result.listings.append(parsed)
        result.checkpoints.append(
            {"url": url, "status": "fetched", "stage": "navigator", "detail": parsed.stats or parsed.title}
        )
        result.steps.append(
            _step(
                "extractor",
                url,
                "extracted",
                f"{parsed.title}: {len(parsed.cards)} cards on this listing page.",
            )
        )
    for slug, category, url, html_text in products:
        kind = classify_page(url, html_text)
        result.steps.append(_step("classifier", url, kind, "Labeled from the URL and page markers."))
        if kind != "product":
            result.steps.append(_step("retry", url, "skipped", REPAIR))
            result.checkpoints.append(
                {"url": url, "status": "skipped", "stage": "retry", "detail": "Not a product page."}
            )
            continue
        try:
            parsed = extract_product(html_text, str(slug), str(category), str(url))
        except ValueError as exc:
            result.steps.append(_step("retry", url, "skipped", f"{exc}. {REPAIR}"))
            result.checkpoints.append(
                {"url": url, "status": "skipped", "stage": "retry", "detail": str(exc)}
            )
            continue
        decision = scope_page(html_text, parsed.name)
        if decision.status == "conflict":
            decision = _resolve_conflict(result, url, parsed.name or str(slug), decision, chooser)
            if decision is None:
                continue
        if decision.status != "in_scope":
            result.steps.append(
                _step(
                    "validator",
                    url,
                    decision.status,
                    f"{parsed.name or slug} is {decision.status.replace('_', ' ')}.",
                )
            )
            result.checkpoints.append(
                {
                    "url": url,
                    "status": decision.status,
                    "stage": "validator",
                    "detail": decision.path or parsed.name or str(slug),
                }
            )
            continue
        parsed.category = decision.category
        if decision.path:
            parsed.category_path = decision.path
        if parsed.recovered:
            result.steps.append(
                _step(
                    "retry",
                    url,
                    "recovered",
                    f"masterData missing. Kept JSON-LD parent {parsed.name}. {REPAIR}",
                )
            )
        else:
            result.steps.append(
                _step(
                    "extractor",
                    url,
                    "extracted",
                    f"{parsed.name}: {len(parsed.variants)} child SKUs, brand {parsed.brand or 'unknown'}.",
                )
            )
        if parsed.slug in seen_slug:
            result.steps.append(
                _step("validator", url, "deduped", f"{parsed.slug} was already stored.")
            )
            result.checkpoints.append(
                {"url": url, "status": "deduped", "stage": "validator", "detail": parsed.slug}
            )
            continue
        seen_slug.add(parsed.slug)
        kept, notes = _dedupe(parsed, seen_sku)
        result.steps.extend(notes)
        if kept is None:
            result.checkpoints.append(
                {"url": url, "status": "rejected", "stage": "validator", "detail": parsed.name}
            )
            continue
        result.products.append(kept)
        result.checkpoints.append(
            {
                "url": url,
                "status": "extracted",
                "stage": "validator",
                "detail": f"{len(kept.variants)} child SKUs",
            }
        )
        result.steps.append(
            _step(
                "validator",
                url,
                "accepted",
                f"{kept.name} stored with {len(kept.variants)} child SKUs.",
            )
        )
    return result


def _resolve_conflict(
    result: Run,
    url: str,
    name: str,
    decision: Scope,
    chooser: Decide,
) -> Scope | None:
    """Ask the decider only for this conflict. None means the page was checkpointed."""
    result.steps.append(
        _step(
            "validator",
            url,
            "conflict",
            f"Body class ({', '.join(decision.class_keys)}) and breadcrumb disagree. {decision.path}",
        )
    )
    choice = chooser(decision.class_keys, decision.path)
    if choice not in {"sutures", "gloves", "out_of_scope"}:
        result.steps.append(
            _step("validator", url, "unresolved", f"{name} stayed unresolved. The scope model did not choose.")
        )
        result.checkpoints.append(
            {"url": url, "status": "unresolved", "stage": "validator", "detail": decision.path or name}
        )
        return None
    if choice == "out_of_scope":
        result.steps.append(_step("validator", url, "out_of_scope", f"{name} is out of scope."))
        result.checkpoints.append(
            {"url": url, "status": "out_of_scope", "stage": "validator", "detail": decision.path or name}
        )
        return None
    return Scope("in_scope", choice, decision.path, decision.class_keys)


def _navigator(url: str, html_text: str) -> list[dict[str, str]]:
    blocked = pagination_links(html_text)
    product_count = len(_product_slugs(html_text))
    category_count = len(child_category_urls(html_text, url))
    steps = [
        _step(
            "navigator",
            url,
            "allowed",
            f"{product_count} product URLs and {category_count} category URLs on this page have no disallowed query.",
        )
    ]
    if blocked:
        preview = ", ".join(blocked[:4])
        extra = f" (+{len(blocked) - 4} more)" if len(blocked) > 4 else ""
        steps.append(
            _step(
                "navigator",
                url,
                "disallowed",
                f"{len(blocked)} pagination URLs match ?page= or ?p= and were not fetched. {preview}{extra}",
            )
        )
    return steps


def _dedupe(
    product: ParsedProduct, seen_sku: set[str]
) -> tuple[ParsedProduct | None, list[dict[str, str]]]:
    if not product.name:
        return None, [_step("validator", product.url, "rejected", "Missing product name.")]
    if not product.sku and not product.variants:
        return None, [_step("validator", product.url, "rejected", "Missing parent SKU and child SKUs.")]
    notes: list[dict[str, str]] = []
    unique: list[ParsedVariant] = []
    for variant in product.variants:
        if not variant.sku:
            notes.append(_step("validator", product.url, "rejected", "Dropped a child row with no SKU."))
            continue
        if variant.sku in seen_sku:
            notes.append(
                _step("validator", product.url, "deduped", f"SKU {variant.sku} was already stored.")
            )
            continue
        seen_sku.add(variant.sku)
        unique.append(variant)
    product.variants = unique
    return product, notes


def _step(stage: str, subject: str, outcome: str, detail: str) -> dict[str, str]:
    return {
        "stage": stage,
        "subject": subject,
        "outcome": outcome,
        "detail": detail[:2000],
    }
