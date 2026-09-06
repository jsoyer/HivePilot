"""Migrate mem0 memories into Hindsight episodic banks (HP-53 slice 1).

mem0 and Hindsight already share the same key: ``{project}:{task}:{role}``.
This module lists mem0 memories per user_id and ``retain``s each one into
the matching Hindsight bank. It does **not** delete mem0, honcho, or
obsidian, and it never writes into HP-52 ``role:{name}`` banks.

Idempotent: each ``(mem0_id, user_id)`` is logged in ``state.db`` so a
second run skips already-migrated items. ``--force`` re-retains.

Dormant clients / missing extras raise ``MigrationUnavailable`` — the CLI
turns that into a clear exit, not a stack trace.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_MAX_PAGES = 200


class MigrationUnavailable(Exception):
    """A precondition failed (flag off, extra missing, client unbuildable)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class MigrationReport:
    keys_scanned: int = 0
    memories_found: int = 0
    migrated: int = 0
    skipped: int = 0
    failed: int = 0
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)


def extract_memories(response: Any) -> list[dict[str, Any]]:
    """Normalise mem0 ``get_all`` shapes into ``{id, text, metadata}`` dicts."""
    if response is None:
        return []
    if isinstance(response, list):
        raw = response
    elif isinstance(response, dict):
        raw = response.get("results") or response.get("memories") or response.get("items") or []
    else:
        raw = (
            getattr(response, "results", None)
            or getattr(response, "memories", None)
            or getattr(response, "items", None)
            or []
        )
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            text = item.strip()
            items.append({"id": None, "text": text, "metadata": {}})
            continue
        if isinstance(item, dict):
            text = str(item.get("memory") or item.get("text") or item.get("content") or "").strip()
            mid = item.get("id") or item.get("memory_id")
            meta = item.get("metadata") or {}
        else:
            text = str(getattr(item, "memory", None) or getattr(item, "text", None) or "").strip()
            mid = getattr(item, "id", None) or getattr(item, "memory_id", None)
            meta = getattr(item, "metadata", None) or {}
        if not text:
            continue
        items.append({"id": str(mid) if mid else None, "text": text, "metadata": meta})
    return items


def memory_id(item: dict[str, Any], user_id: str) -> str:
    """Stable id: mem0's own id, or a hash of the bank + text."""
    if item.get("id"):
        return str(item["id"])
    digest = hashlib.sha256(f"{user_id}\0{item.get('text', '')}".encode()).hexdigest()[:16]
    return f"hash:{digest}"


def discover_user_ids(extra: Sequence[str] | None = None) -> list[str]:
    """Config-derived ``{project}:{task}`` and ``{project}:{task}:{role}`` keys."""
    keys: set[str] = {item.strip() for item in (extra or []) if item and item.strip()}
    try:
        from hivepilot.roles import ROLES
        from hivepilot.services.project_service import load_projects, load_tasks

        projects = list(load_projects().projects)
        tasks = list(load_tasks().tasks)
        roles = list(ROLES)
        for project in projects:
            for task in tasks:
                keys.add(f"{project}:{task}")
                for role in roles:
                    keys.add(f"{project}:{task}:{role}")
    except Exception as exc:  # noqa: BLE001 — a missing yaml must not abort --user-id
        logger.info("mem0_migrate.discover_skipped", error=str(exc))
    return sorted(keys)


def list_memories_for_key(client: Any, user_id: str, page_size: int) -> list[dict[str, Any]]:
    """Version-tolerant ``get_all``: 2.x filters, then 1.x user_id, paginated."""
    collected: list[dict[str, Any]] = []
    for page in range(1, _MAX_PAGES + 1):
        batch = _get_all_page(client, user_id, page=page, page_size=page_size)
        if not batch:
            break
        collected.extend(batch)
        if len(batch) < page_size:
            break
    return collected


def _get_all_page(client: Any, user_id: str, *, page: int, page_size: int) -> list[dict[str, Any]]:
    get_all = getattr(client, "get_all", None)
    if get_all is None:
        raise MigrationUnavailable("mem0 client has no get_all()")
    try:
        response = get_all(filters={"user_id": user_id}, page=page, page_size=page_size)
    except TypeError:
        try:
            response = get_all(user_id=user_id, page=page, page_size=page_size)
        except TypeError:
            if page > 1:
                return []
            response = get_all(user_id=user_id)
    return extract_memories(response)


def _retain_content(user_id: str, text: str) -> str:
    from hivepilot.services.config_provenance import redact_text

    return redact_text(f"[migrated-from-mem0 {user_id}]\n{text}")


def build_mem0_client() -> Any | None:
    """Hosted MemoryClient or self-host Memory — same rules as the plugin."""
    try:
        from mem0 import Memory, MemoryClient  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return None
    from hivepilot.config import settings

    try:
        if getattr(settings, "mem0_api_key", None):
            return MemoryClient(api_key=settings.mem0_api_key)
        config = getattr(settings, "mem0_config", None)
        return Memory.from_config(config) if config else Memory()
    except Exception as exc:  # noqa: BLE001
        logger.warning("mem0_migrate.client_failed", error=str(exc))
        return None


def migrate(
    *,
    dry_run: bool = False,
    user_id: str | None = None,
    page_size: int = 100,
    force: bool = False,
    mem0_client: Any | None = None,
    hindsight_client: Any | None = None,
) -> MigrationReport:
    """Copy mem0 memories into Hindsight. Inject clients in tests."""
    from hivepilot.config import settings
    from hivepilot.services import state_service
    from hivepilot.services.hindsight_role_sync import hindsight_sdk_client

    if mem0_client is None:
        if not getattr(settings, "mem0_enabled", False):
            raise MigrationUnavailable(
                "HIVEPILOT_MEM0_ENABLED is off — enable it to read the source corpus"
            )
        mem0_client = build_mem0_client()
    if mem0_client is None:
        raise MigrationUnavailable("mem0 client could not be built — install mem0ai")

    if not dry_run:
        if hindsight_client is None:
            if not getattr(settings, "hindsight_enabled", False):
                raise MigrationUnavailable(
                    "HIVEPILOT_HINDSIGHT_ENABLED is off — enable it as the destination"
                )
            hindsight_client = hindsight_sdk_client()
        if hindsight_client is None:
            raise MigrationUnavailable(
                "hindsight client could not be built — install hindsight-client"
            )

    keys = [user_id] if user_id else discover_user_ids()
    report = MigrationReport(keys_scanned=len(keys), dry_run=dry_run)
    retain = getattr(hindsight_client, "retain", None) if hindsight_client is not None else None

    for key in keys:
        try:
            items = list_memories_for_key(mem0_client, key, page_size)
        except Exception as exc:  # noqa: BLE001 — one bank must not abort the rest
            report.failed += 1
            report.errors.append(f"{key}: list failed: {exc}")
            continue
        report.memories_found += len(items)
        for item in items:
            mid = memory_id(item, key)
            if not force and state_service.mem0_already_migrated(mid, key):
                report.skipped += 1
                continue
            if dry_run:
                report.migrated += 1
                continue
            try:
                if retain is None:
                    raise MigrationUnavailable("hindsight client has no retain()")
                retain(bank_id=key, content=_retain_content(key, item["text"]))
                state_service.record_mem0_migrated(mid, key, key)
                report.migrated += 1
            except Exception as exc:  # noqa: BLE001
                report.failed += 1
                report.errors.append(f"{key}/{mid}: {exc}")
    return report


def iter_error_lines(errors: Iterable[str], *, limit: int = 12) -> list[str]:
    lines = list(errors)
    if len(lines) <= limit:
        return lines
    return [*lines[:limit], f"... {len(lines) - limit} more"]
