"""Decide whether a product page belongs to the two seed categories.

The decision uses the page, not the URL slug. One signal is enough. When the
body class and the breadcrumb each name a seed and they differ, the status is
conflict and the caller may ask a model. A breadcrumb outside both seeds is
out of scope even if the body class matches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from extract import category_path

# Config seed order is sutures, then gloves. The first match becomes category_key.
ORDER = ("sutures", "gloves")
TITLES = {
    "sutures": "Sutures & surgical products",
    "gloves": "Dental Exam Gloves",
}
_CLASS_PREFIX = {
    "sutures": "categorypath-catalog-sutures-surgical-products",
    "gloves": "categorypath-catalog-gloves",
}
_BLOCKED_ROOTS = ("Clearance", "Equipment")


@dataclass(frozen=True)
class Scope:
    status: str
    category: str
    path: str
    class_keys: tuple[str, ...] = ()


def scope_page(page: str, product_name: str = "") -> Scope:
    """Return in_scope, out_of_scope, conflict, or unresolved for one product page."""
    path = category_path(page, product_name)
    class_keys = _class_keys(page)
    crumb = scope_path(path) if path else None
    if class_keys is None:
        if crumb is None:
            return Scope("unresolved", "", "")
        return crumb
    found = tuple(class_keys)
    if not found:
        return Scope("out_of_scope", "", path)
    if crumb is not None and crumb.status == "out_of_scope":
        return Scope("out_of_scope", "", path, found)
    if crumb is not None and crumb.status == "in_scope" and crumb.category not in found:
        return Scope("conflict", "", path, found)
    if crumb is not None and crumb.status == "in_scope":
        return Scope("in_scope", crumb.category, path, found)
    return Scope("in_scope", found[0], _record(path, list(found)), found)


def scope_path(category_path_text: str) -> Scope:
    """Same decision from a stored breadcrumb. A blank path is unresolved.

    A path that starts at Clearance or Equipment stays out of scope even when a
    seed title was appended after those segments.
    """
    path = category_path_text.strip()
    if not path:
        return Scope("unresolved", "", "")
    if path.startswith(_BLOCKED_ROOTS):
        return Scope("out_of_scope", "", path)
    segments = [part.strip() for part in path.split(" / ") if part.strip()]
    keys = [key for key in ORDER if TITLES[key] in segments]
    if not keys:
        return Scope("out_of_scope", "", path)
    return Scope("in_scope", keys[0], path)


def _class_keys(page: str) -> list[str] | None:
    """Matching seed keys, or None when the page has no categorypath class."""
    tokens = [
        token
        for token in _body_tokens(page)
        if token.startswith("categorypath-catalog-")
    ]
    if not tokens:
        return None
    found: list[str] = []
    for key in ORDER:
        prefix = _CLASS_PREFIX[key]
        if any(token == prefix or token.startswith(prefix + "-") for token in tokens):
            found.append(key)
    return found


def _body_tokens(page: str) -> list[str]:
    match = re.search(r"<body\b([^>]*)>", page, re.I)
    if not match:
        return []
    classes = re.search(r'class="([^"]*)"', match.group(1))
    if not classes:
        return []
    return classes.group(1).split()


def _record(path: str, keys: list[str]) -> str:
    """Name the matched seeds when the page has no breadcrumb to keep."""
    if path:
        return path
    return " / ".join(TITLES[key] for key in keys)
