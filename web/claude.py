"""Ask Claude to choose one listed query. The API key stays in .env or the environment."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
API_URL = "https://api.anthropic.com/v1/messages"
_PLACEHOLDER = {"", "paste-your-key-here", "your-key-here"}


def _file_values() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    values: dict[str, str] = {}
    for raw in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _setting(name: str) -> str:
    from_env = os.environ.get(name, "").strip()
    if from_env:
        return from_env
    return _file_values().get(name, "").strip()


def configured() -> bool:
    return _setting("CLAUDE_API_KEY") not in _PLACEHOLDER


def choose(question: str, catalog: list[dict[str, object]]) -> tuple[str | None, dict[str, str], bool]:
    """Return (query id, params, failed).

    failed is true when the key is set but Claude did not return a listed id.
    The caller must still reject ids the user level cannot run.
    """
    key = _setting("CLAUDE_API_KEY")
    if key in _PLACEHOLDER:
        return (None, {}, False)
    allowed = {str(item["id"]) for item in catalog}
    prompt = (
        "Choose one query id from the catalog for the user's question. "
        'Reply with JSON only: {"id": "<id or empty>", "sku": ""}. '
        "Put a SKU in sku only when the question names a product code. "
        "Do not invent SQL.\n"
        f"Question: {question}\n"
        f"Catalog: {json.dumps(catalog)}"
    )
    payload = {
        "model": _setting("CLAUDE_MODEL") or "claude-haiku-5-5",
        "max_tokens": 200,
        "thinking": {"type": "disabled"},
        "output_config": {"effort": "low"},
        "messages": [{"role": "user", "content": prompt}],
    }
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return (None, {}, True)
    text = _message_text(body)
    parsed = _parse_choice(text)
    if parsed is None:
        return (None, {}, True)
    query_id, sku = parsed
    if query_id not in allowed:
        return (None, {}, True)
    return (query_id, {"sku": sku}, False)


def _message_text(body: object) -> str:
    if not isinstance(body, dict):
        return ""
    blocks = body.get("content")
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "\n".join(parts)


def _parse_choice(text: str) -> tuple[str, str] | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        cleaned = cleaned.removeprefix("json").strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    return (str(payload.get("id") or ""), str(payload.get("sku") or ""))
