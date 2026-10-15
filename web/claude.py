"""Ask Claude to choose one listed query. The API key stays in .env or the environment."""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
API_URL = "https://api.anthropic.com/v1/messages"
_PLACEHOLDER = {"", "paste-your-key-here", "your-key-here"}
_CA_FILES = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/opt/homebrew/etc/openssl@3/cert.pem",
    "/opt/homebrew/etc/ca-certificates/cert.pem",
)


@dataclass(frozen=True)
class Choice:
    query_id: str | None
    params: dict[str, str]
    failed: bool
    answer: str = ""


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


def scope_prompt(class_keys: tuple[str, ...] | list[str], breadcrumb: str) -> str:
    """Text sent when the body class and the breadcrumb name different seeds.

    The page HTML and the API key are not part of this text.
    """
    from scope import TITLES

    seeds = "; ".join(f"{key} = {TITLES[key]}" for key in ("sutures", "gloves"))
    keys = ", ".join(class_keys)
    return (
        "The body class and the breadcrumb name different seed categories. "
        "Choose sutures, gloves, or out_of_scope. "
        'Reply with JSON only: {"category": "sutures" or "gloves" or "out_of_scope"}. '
        "Do not invent a category.\n"
        f"Class keys: {keys}\n"
        f"Breadcrumb: {breadcrumb}\n"
        f"Seeds: {seeds}"
    )


def decide_scope(class_keys: tuple[str, ...] | list[str], breadcrumb: str) -> str | None:
    """Return sutures, gloves, or out_of_scope.

    None when the key is missing or the reply is unusable. The caller leaves
    that page unresolved. This does not fetch a page.
    """
    key = _setting("CLAUDE_API_KEY")
    if key in _PLACEHOLDER:
        return None
    prompt = scope_prompt(class_keys, breadcrumb)
    payload = {
        "model": _setting("CLAUDE_MODEL") or "claude-haiku-5-5",
        "max_tokens": 80,
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
        with urllib.request.urlopen(request, timeout=20, context=_ssl_context()) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    return _parse_scope(_message_text(body))


def choose(question: str, catalog: list[dict[str, object]]) -> Choice:
    """Pick one listed query, or a short reply when none fits.

    failed is true when the key is set but Claude did not return a usable choice.
    The caller must still reject ids the user level cannot run.
    """
    key = _setting("CLAUDE_API_KEY")
    if key in _PLACEHOLDER:
        return Choice(None, {}, False)
    allowed = {str(item["id"]) for item in catalog}
    prompt = (
        "Choose one query id from the catalog for the user's question. "
        'Reply with JSON only: {"id": "<id or empty>", "sku": "", "answer": ""}. '
        "Put a SKU in sku only when the question names a product code. "
        "When a listed query answers the question, set id and leave answer empty. "
        "When none does, including a greeting or a question about which queries exist, "
        "leave id empty and put a short answer in answer. "
        "That answer may name the allowed titles. Do not invent SQL.\n"
        f"Question: {question}\n"
        f"Catalog: {json.dumps(catalog)}"
    )
    payload = {
        "model": _setting("CLAUDE_MODEL") or "claude-haiku-5-5",
        "max_tokens": 400,
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
        with urllib.request.urlopen(request, timeout=20, context=_ssl_context()) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return Choice(None, {}, True)
    parsed = _parse_choice(_message_text(body))
    if parsed is None:
        return Choice(None, {}, True)
    query_id, sku, answer = parsed
    if not query_id:
        return Choice(None, {}, False, _clip(answer) or _catalog_answer(catalog))
    if query_id not in allowed:
        return Choice(None, {}, True)
    return Choice(query_id, {"sku": sku}, False)


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


def _ssl_context() -> ssl.SSLContext:
    """Use a CA bundle Python can actually read.

    The python.org macOS build looks for a cert.pem that is not installed,
    so the default context cannot verify api.anthropic.com.
    """
    candidates: list[str] = []
    env_file = os.environ.get("SSL_CERT_FILE", "").strip()
    if env_file:
        candidates.append(env_file)
    try:
        import certifi
    except ImportError:
        pass
    else:
        candidates.append(certifi.where())
    candidates.extend(_CA_FILES)
    for path in candidates:
        if Path(path).is_file():
            return ssl.create_default_context(cafile=path)
    return ssl.create_default_context()


def _catalog_answer(catalog: list[dict[str, object]]) -> str:
    titles = [str(item["title"]) for item in catalog if item.get("title")]
    if not titles:
        return "No listed query matches."
    return "I can run these: " + ", ".join(titles) + "."


def _clip(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    cleaned = "\n".join(line for line in lines if line)
    if len(cleaned) <= 800:
        return cleaned
    return cleaned[:797].rstrip() + "..."


def _parse_scope(text: str) -> str | None:
    cleaned = text.strip()
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
    category = str(payload.get("category") or "")
    if category not in {"sutures", "gloves", "out_of_scope"}:
        return None
    return category


def _parse_choice(text: str) -> tuple[str, str, str] | None:
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
    return (
        str(payload.get("id") or ""),
        str(payload.get("sku") or ""),
        str(payload.get("answer") or ""),
    )
