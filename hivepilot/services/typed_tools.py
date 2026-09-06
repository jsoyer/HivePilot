"""HP-58: namespaced typed-tool catalog (MCP + OpenAPI).

Qualified names follow Claude's ``mcp__<server>__<tool>`` shape so a later
runner hook can pass them as ``allowed_tools`` without a second mapping.
Collisions across sources get a numeric suffix instead of overwriting.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from hivepilot.services import db, state_service

_SAFE = re.compile(r"[^a-z0-9._-]+")


@dataclass
class TypedTool:
    qualified_name: str
    local_name: str
    source_kind: str  # mcp | openapi
    source_id: str
    description: str
    input_schema: dict[str, Any]
    id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TypedToolError(ValueError):
    """Invalid tool descriptor."""


def safe_token(raw: str, fallback: str = "tool") -> str:
    cleaned = _SAFE.sub("-", (raw or "").strip().lower()).strip("-._")
    return (cleaned or fallback)[:64]


def qualify_tool_name(kind: str, source: str, local: str) -> str:
    return f"{safe_token(kind)}__{safe_token(source)}__{safe_token(local)}"


def _unique_qualified(conn: Any, desired: str, source_id: str) -> str:
    row = conn.execute(
        db.ph("SELECT source_id FROM typed_tools WHERE qualified_name=?"), (desired,)
    ).fetchone()
    if row is None or str(row["source_id"]) == source_id:
        return desired
    for index in range(2, 100):
        candidate = f"{desired}__{index}"
        other = conn.execute(
            db.ph("SELECT source_id FROM typed_tools WHERE qualified_name=?"), (candidate,)
        ).fetchone()
        if other is None or str(other["source_id"]) == source_id:
            return candidate
    raise TypedToolError(f"could not de-collide {desired!r}")


def replace_source_tools(
    source_kind: str, source_id: str, tools: list[TypedTool]
) -> list[TypedTool]:
    """Replace every tool for one source. Names are re-qualified + de-collided."""
    state_service.init_db()
    stored: list[TypedTool] = []
    with db.connect() as conn:
        conn.execute(
            db.ph("DELETE FROM typed_tools WHERE source_kind=? AND source_id=?"),
            (source_kind, source_id),
        )
        for tool in tools:
            local = safe_token(tool.local_name)
            qualified = _unique_qualified(
                conn, qualify_tool_name(source_kind, tool.source_id, local), source_id
            )
            tid = uuid.uuid4().hex
            conn.execute(
                db.ph(
                    """
                    INSERT INTO typed_tools (
                        id, qualified_name, local_name, source_kind, source_id,
                        description, input_schema
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """
                ),
                (
                    tid,
                    qualified,
                    local,
                    source_kind,
                    source_id,
                    tool.description or "",
                    json.dumps(tool.input_schema or {}),
                ),
            )
            stored.append(
                TypedTool(
                    id=tid,
                    qualified_name=qualified,
                    local_name=local,
                    source_kind=source_kind,
                    source_id=source_id,
                    description=tool.description or "",
                    input_schema=dict(tool.input_schema or {}),
                )
            )
    return stored


def list_typed_tools(
    *, source_kind: str | None = None, source_id: str | None = None
) -> list[TypedTool]:
    state_service.init_db()
    clauses = ["1=1"]
    params: list[Any] = []
    if source_kind:
        clauses.append("source_kind=?")
        params.append(source_kind)
    if source_id:
        clauses.append("source_id=?")
        params.append(source_id)
    sql = f"SELECT * FROM typed_tools WHERE {' AND '.join(clauses)} ORDER BY qualified_name"
    with db.connect() as conn:
        rows = conn.execute(db.ph(sql), tuple(params)).fetchall()
    out: list[TypedTool] = []
    for row in rows:
        mapping = dict(row)
        schema_raw = mapping.get("input_schema") or "{}"
        try:
            schema = json.loads(schema_raw)
        except (ValueError, TypeError):
            schema = {}
        out.append(
            TypedTool(
                id=str(mapping["id"]),
                qualified_name=str(mapping["qualified_name"]),
                local_name=str(mapping["local_name"]),
                source_kind=str(mapping["source_kind"]),
                source_id=str(mapping["source_id"]),
                description=str(mapping.get("description") or ""),
                input_schema=schema if isinstance(schema, dict) else {},
            )
        )
    return out
