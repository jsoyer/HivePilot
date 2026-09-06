"""Mission progress narrative via Hindsight ``reflect()`` (HP-54).

HP-51 retains experience facts in ``{project}:{task}:{role}``. HP-52 writes
role identity (``reflect_mission`` + directives) into ``role:{name}``. This
module asks Hindsight to *reason* over the experience bank of a mission's
first spawned task, with the engine's own numeric status as context.

``reflect`` is an LLM call. We:

- stay dormant unless ``HIVEPILOT_HINDSIGHT_ENABLED``
- cache the answer on the mission row keyed by a status fingerprint
- never raise into ``check_mission``
- never invent a story when the client is missing or the call fails
  (``narrative`` stays ``None``; the numeric status is still the source of truth)

The Missions board (HP-29) can render ``narrative`` once it exists. This
slice only fills the field on ``GET /v1/orchestrator/missions/{id}``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_REFLECT_QUERY = (
    "Où en est cette mission ? Réponds en 2 à 4 phrases, factuel, "
    "sans inventer ce que les souvenirs ne montrent pas."
)
_FACT_TYPES = ["experience"]
_BUDGET = "low"
_MAX_TOKENS = 512


class ReflectClient(Protocol):
    def reflect(self, **kwargs: Any) -> Any: ...


@dataclass(frozen=True)
class MissionNarrative:
    """Cached or freshly reflected prose. ``text`` is None when we have none."""

    text: str | None
    bank_id: str | None
    fingerprint: str
    cached: bool


def status_fingerprint(status: Mapping[str, Any]) -> str:
    """Stable hash of the numeric status — a cache key, not a secret."""
    payload = {
        "total": status.get("total"),
        "succeeded": status.get("succeeded"),
        "failed": status.get("failed"),
        "pending": status.get("pending"),
        "done": status.get("done"),
        "tasks": status.get("tasks") or {},
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def extract_reflect_text(result: Any) -> str:
    """Tolerate the shapes Hindsight has shipped (object / dict / bare string)."""
    if result is None:
        return ""
    if isinstance(result, str):
        return result.strip()
    for attr in ("answer", "text", "content", "reflection"):
        if isinstance(result, dict):
            value = result.get(attr)
        else:
            value = getattr(result, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _enabled() -> bool:
    from hivepilot.config import settings

    return bool(getattr(settings, "hindsight_enabled", False))


def _sdk_client() -> Any | None:
    from hivepilot.services.hindsight_role_sync import hindsight_sdk_client

    return hindsight_sdk_client()


def _role_for_run(run_id: int) -> str | None:
    from hivepilot.services import state_service

    try:
        steps = state_service.get_steps_for_run(run_id)
    except Exception:  # noqa: BLE001 — a missing steps table must not break status
        return None
    for step in reversed(steps):
        role = step.get("role")
        if isinstance(role, str) and role.strip():
            return role.strip()
    return None


def experience_banks_for_mission(mission: Mapping[str, Any]) -> list[str]:
    """Episodic banks HP-51 would have retained into, in spawn order."""
    from hivepilot.services import state_service

    project = str(mission.get("project") or "unknown").strip() or "unknown"
    banks: list[str] = []
    seen: set[str] = set()
    for task_id, run_id in (mission.get("runs") or {}).items():
        row = None
        if run_id is not None:
            try:
                row = state_service.get_run(int(run_id))
            except Exception:  # noqa: BLE001
                row = None
        task = str((row or {}).get("task") or task_id or "unknown").strip() or "unknown"
        role = _role_for_run(int(run_id)) if run_id is not None else None
        bank = f"{project}:{task}:{role}" if role else f"{project}:{task}"
        if bank not in seen:
            seen.add(bank)
            banks.append(bank)
    return banks


def _status_context(mission: Mapping[str, Any], status: Mapping[str, Any]) -> str:
    lines = [
        f"Objectif : {mission.get('goal') or ''}".rstrip(),
        (
            f"Avancement moteur : {status.get('succeeded', 0)}/"
            f"{status.get('total', 0)} réussie(s), "
            f"{status.get('failed', 0)} en échec, "
            f"{status.get('pending', 0)} en cours."
        ),
    ]
    for task_id, info in (status.get("tasks") or {}).items():
        raw = (info or {}).get("status") or "unknown"
        lines.append(f"- {task_id}: {raw}")
    return "\n".join(lines)


def _call_reflect(client: ReflectClient, bank_id: str, context: str) -> str:
    kwargs: dict[str, Any] = {
        "bank_id": bank_id,
        "query": _REFLECT_QUERY,
        "budget": _BUDGET,
        "context": context,
        "max_tokens": _MAX_TOKENS,
        "fact_types": _FACT_TYPES,
    }
    try:
        return extract_reflect_text(client.reflect(**kwargs))
    except TypeError:
        kwargs.pop("fact_types", None)
        return extract_reflect_text(client.reflect(**kwargs))


def reflect_mission_progress(
    mission: Mapping[str, Any],
    status: Mapping[str, Any],
    *,
    client: ReflectClient | None = None,
    enabled: bool | None = None,
) -> MissionNarrative:
    """Return a narrative for this status snapshot. Never raises."""
    fingerprint = status_fingerprint(status)
    stored = (mission.get("narrative") or "") if isinstance(mission.get("narrative"), str) else ""
    stored_fp = mission.get("narrative_fingerprint") or ""
    if stored and stored_fp == fingerprint:
        return MissionNarrative(
            text=stored,
            bank_id=None,
            fingerprint=fingerprint,
            cached=True,
        )
    if not (enabled if enabled is not None else _enabled()):
        return MissionNarrative(text=None, bank_id=None, fingerprint=fingerprint, cached=False)

    adapter = client if client is not None else _sdk_client()
    if adapter is None:
        return MissionNarrative(text=None, bank_id=None, fingerprint=fingerprint, cached=False)

    banks = experience_banks_for_mission(mission)
    if not banks:
        return MissionNarrative(text=None, bank_id=None, fingerprint=fingerprint, cached=False)

    bank = banks[0]
    try:
        text = _call_reflect(adapter, bank, _status_context(mission, status)) or None
    except Exception as exc:  # noqa: BLE001 — status API must stay up
        logger.warning("hindsight_reflect.failed", bank=bank, error=str(exc))
        return MissionNarrative(text=None, bank_id=bank, fingerprint=fingerprint, cached=False)

    if text:
        logger.info("hindsight_reflect.ok", bank=bank, chars=len(text))
    return MissionNarrative(text=text, bank_id=bank, fingerprint=fingerprint, cached=False)
