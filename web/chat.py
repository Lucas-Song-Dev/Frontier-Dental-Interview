"""Answer a question by running one query from the server catalog."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from claude import choose, configured
from db import get_settings, run_select
from queries import (
    BY_ID,
    allowed_ids,
    bind_sku,
    describe,
    level_allows,
    match_question,
    normalize_level,
)


@dataclass
class Reply:
    answer: str
    sql: str
    columns: list[str]
    rows: list[list[str | None]]
    query_id: str = ""


def ask(question: str, *, query_id: str = "", sku: str = "") -> Reply:
    level = normalize_level(get_settings()["user_level"])
    if query_id.strip():
        return _gated(query_id.strip(), {"sku": sku}, level, via="direct")
    text = question.strip()
    if not text:
        return Reply("Pick a listed query, or ask in those words.", "", [], [])
    if re.search(r"(?i)\b(drop|delete|insert|update|alter|attach)\b", text):
        return Reply("This chat only runs listed read-only queries.", "", [], [])
    if re.match(r"(?is)^(select|with)\b", text):
        return Reply("Pasted SQL is turned off. Run a query from the server list.", "", [], [])

    chosen_id = ""
    params: dict[str, str] = {}
    via = "list"
    if configured():
        catalog = [item for item in describe(level) if item["allowed"]]
        choice = choose(text, catalog)
        via = "claude"
        if choice.query_id:
            chosen_id = choice.query_id
            params = choice.params
        else:
            local = match_question(text)
            if local is None:
                if choice.answer and not choice.failed:
                    return Reply(choice.answer, "", [], [])
                note = "Claude could not choose a listed query." if choice.failed else "No listed query matches."
                return Reply(note, "", [], [])
            chosen_id, params = local
            via = "fallback"
    else:
        local = match_question(text)
        if local is None:
            return Reply("No listed query matches. Use one from the server list.", "", [], [])
        chosen_id, params = local
    if sku and "sku" not in params:
        params = {**params, "sku": sku}
    return _gated(chosen_id, params, level, via=via)


def _gated(query_id: str, params: dict[str, str], level: str, *, via: str) -> Reply:
    spec = BY_ID.get(query_id)
    if spec is None or query_id not in {item.id for item in BY_ID.values()}:
        return Reply("That query is not in the server list.", "", [], [])
    if not level_allows(spec, level):
        return Reply(
            f"Not run. {level.capitalize()} cannot use {spec.title}. "
            f"{spec.level.capitalize()} or above can. Change the user level in Settings.",
            spec.sql,
            [],
            [],
            spec.id,
        )
    if query_id not in set(allowed_ids(level)):
        return Reply(
            f"Not run. {level.capitalize()} cannot use {spec.title}.",
            "",
            [],
            [],
            spec.id,
        )
    try:
        bound = bind_sku(params.get("sku", "")) if spec.needs_sku else ()
    except ValueError as exc:
        return Reply(str(exc), spec.sql, [], [], spec.id)
    reply = _execute(spec.sql, bound, _show(spec.sql, bound), spec.id)
    if via == "claude":
        reply.answer = f"Claude chose {spec.title}. {reply.answer}"
    elif via == "fallback":
        reply.answer = f"Claude did not choose a listed query. {reply.answer}"
    return reply


def _execute(sql: str, params: tuple, shown: str, query_id: str) -> Reply:
    try:
        columns, raw_rows = run_select(sql, params)
    except (sqlite3.Error, ValueError) as exc:
        return Reply(str(exc), shown, [], [], query_id)
    rows = [[_cell(row[column]) for column in columns] for row in raw_rows]
    return Reply(_narrate(columns, rows, shown), shown, columns, rows, query_id)


def _narrate(columns: list[str], rows: list[list[str | None]], shown: str) -> str:
    if not rows:
        return "No rows matched. This database has the two saved listings and three product sheets."
    if "ORDER BY variant_price ASC" in shown and "variant_price" in columns:
        price = rows[0][columns.index("variant_price")]
        name = rows[0][columns.index("variant_name")]
        return f"Lowest price is {price} USD, for {name}. {len(rows)} rows, cheapest first."
    if columns == ["key", "value"]:
        lines = [f"{row[0]}: {row[1] or '(blank)'}" for row in rows]
        return "Gate records:\n" + "\n".join(lines)
    if columns == ["matches"] and len(rows) == 1:
        return f"{rows[0][0]} matching rows."
    if len(rows) == 1 and "value" in columns and "key" in columns:
        return f"{rows[0][columns.index('key')]}: {rows[0][columns.index('value')] or '(blank)'}."
    if len(rows) == 1:
        pairs = [
            f"{column.replace('_', ' ')} {value}"
            for column, value in zip(columns, rows[0], strict=True)
            if value
        ]
        return "One row. " + ", ".join(pairs) + "."
    return f"{len(rows)} rows. The table is the result of the query."


def _show(sql: str, params: tuple) -> str:
    shown = sql
    for value in params:
        replacement = "'" + str(value).replace("'", "''") + "'"
        shown = shown.replace("?", replacement, 1)
    return shown


def _cell(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)
