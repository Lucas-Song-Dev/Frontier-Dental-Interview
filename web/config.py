"""Load config.yaml. Environment variables override secrets and are never logged."""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"

DEFAULTS = {
    "origin": "https://www.safcodental.com",
    "user_agent": "CatalogDesk/0.1 (local catalog desk; respects robots.txt)",
    "min_delay_seconds": 3,
    "max_products": 40,
    "seeds": {
        "sutures": "https://www.safcodental.com/catalog/sutures-surgical-products",
        "gloves": "https://www.safcodental.com/catalog/gloves",
    },
}


def _coerce(value: str) -> str | int:
    if value.isdigit():
        return int(value)
    return value


def load_config() -> dict[str, object]:
    """Read the flat config file. Missing file falls back to the assignment seeds."""
    data: dict[str, object] = {
        "origin": DEFAULTS["origin"],
        "user_agent": DEFAULTS["user_agent"],
        "min_delay_seconds": DEFAULTS["min_delay_seconds"],
        "max_products": DEFAULTS["max_products"],
        "seeds": dict(DEFAULTS["seeds"]),
    }
    if not CONFIG_PATH.is_file():
        return data
    seeds = data["seeds"]
    assert isinstance(seeds, dict)
    for raw in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip() or ":" not in line:
            continue
        if line.startswith(" "):
            key, value = line.strip().split(":", 1)
            seeds[key.strip()] = _coerce(value.strip())
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key == "seeds" or value == "":
            continue
        data[key] = _coerce(value)
    delay = os.environ.get("MIN_DELAY_SECONDS", "").strip()
    if delay.isdigit():
        data["min_delay_seconds"] = int(delay)
    return data


def claude_key_is_set() -> bool:
    """True when a key is present. The key itself is not returned."""
    return bool(os.environ.get("CLAUDE_API_KEY", "").strip())
