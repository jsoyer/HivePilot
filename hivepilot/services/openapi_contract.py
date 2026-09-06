"""HP-64: curated OpenAPI contract for the Pollen typed client.

The full FastAPI schema is large and includes unversioned twins plus UI
routes. The committed spec is the `/v1` surface Pollen actually types
against: roles, concierge, and schedules (list + named trigger).
"""

from __future__ import annotations

import json
from typing import Any

CONTRACT_PATH_PREFIXES: tuple[str, ...] = (
    "/v1/roles",
    "/v1/concierge",
    "/v1/schedules",
    "/v1/webhook/trigger",
)


def is_contract_path(path: str) -> bool:
    return any(path == prefix or path.startswith(f"{prefix}/") for prefix in CONTRACT_PATH_PREFIXES)


def filter_openapi(schema: dict[str, Any]) -> dict[str, Any]:
    """Keep contract paths only. Components stay intact so $refs resolve."""
    paths = schema.get("paths") or {}
    kept = {path: ops for path, ops in paths.items() if is_contract_path(path)}
    out = dict(schema)
    out["paths"] = kept
    info = dict(out.get("info") or {})
    info["title"] = "HivePilot Pollen contract"
    info["description"] = (
        "Curated OpenAPI for roles, concierge, and schedules (HP-64). "
        "Regenerate with `python scripts/export_openapi.py`."
    )
    out["info"] = info
    return out


def export_contract() -> dict[str, Any]:
    from hivepilot.services.api_service import app

    return filter_openapi(app.openapi())


def dumps_contract(schema: dict[str, Any] | None = None) -> str:
    payload = schema if schema is not None else export_contract()
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"
