"""Pollen Memory panel over Hindsight role banks (HP-55).

Vue par rôle: curated Mental Models plus traced Observations (quotes /
proof count / optional confidence), editable by a human.

Reads and writes the HP-52 identity bank ``role:{name}``. Does not touch
the HP-51 episodic namespace ``{project}:{task}:{role}``.

Observations are derived — Hindsight rejects PATCH on ``fact_type=observation``.
Human edits go to the underlying world/experience facts listed as
``evidence``. Mental models are first-class: create / update / refresh.

Dormant unless ``HIVEPILOT_HINDSIGHT_ENABLED``. A missing extra or a
raising SDK call never 500s a GET; mutations return a structured error.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from hivepilot.services.hindsight_role_sync import hindsight_sdk_client, role_bank_id
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_ROLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_DISABLED = "Hindsight is disabled (HIVEPILOT_HINDSIGHT_ENABLED)."
_NO_CLIENT = "hindsight-client is not installed or failed to construct."


class PanelClient(Protocol):
    """Narrow surface over ``hindsight_client.Hindsight``. Tests fake this."""

    def list_mental_models(self, bank_id: str, **kwargs: Any) -> Any: ...

    def create_mental_model(self, bank_id: str, **kwargs: Any) -> Any: ...

    def update_mental_model(self, bank_id: str, mental_model_id: str, **kwargs: Any) -> Any: ...

    def refresh_mental_model(self, bank_id: str, mental_model_id: str) -> Any: ...

    def list_memories(self, bank_id: str, **kwargs: Any) -> Any: ...

    def update_memory(self, bank_id: str, memory_id: str, **kwargs: Any) -> Any: ...


class UnknownRole(ValueError):
    """``role`` is not on the HivePilot roster."""


class PanelError(RuntimeError):
    """Hindsight call failed or the panel is not configured."""

    def __init__(self, detail: str, *, status_code: int = 502) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


def _enabled() -> bool:
    from hivepilot.config import settings

    return bool(getattr(settings, "hindsight_enabled", False))


def _pick(obj: Any, *names: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, Mapping):
        for name in names:
            if name in obj and obj[name] is not None:
                return obj[name]
        return None
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None and not callable(value):
                return value
    return None


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, (int, float, bool)):
        return str(value)
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _as_str_list(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        out: list[str] = []
        for item in value:
            text = _as_str(item)
            if text:
                out.append(text)
        return out
    return []


def _items(response: Any) -> list[Any]:
    if response is None:
        return []
    if isinstance(response, list):
        return response
    if isinstance(response, Mapping):
        for key in ("items", "mental_models", "memories", "observations", "data"):
            value = response.get(key)
            if isinstance(value, list):
                return value
        return []
    for attr in ("items", "mental_models", "memories", "observations", "data"):
        if hasattr(response, attr):
            value = getattr(response, attr)
            if isinstance(value, list):
                return value
    return []


def known_role_names() -> set[str]:
    """Live YAML roster plus any store-adopted names."""
    from hivepilot import roles

    names = {role.name for role in roles.list_roles()}
    try:
        for row in roles.api_roster():
            name = row.get("name") if isinstance(row, Mapping) else None
            if isinstance(name, str) and name.strip():
                names.add(name.strip())
    except Exception:  # noqa: BLE001 — roster store must not blank the panel
        logger.debug("hindsight_panel.api_roster_failed", exc_info=True)
    return names


def resolve_role(name: str) -> str:
    """Return the roster name or raise ``UnknownRole``."""
    stripped = (name or "").strip()
    if not stripped or not _ROLE_RE.match(stripped):
        raise UnknownRole(stripped)
    known = known_role_names()
    if stripped in known:
        return stripped
    lowered = {item.lower(): item for item in known}
    if stripped.lower() in lowered:
        return lowered[stripped.lower()]
    raise UnknownRole(stripped)


def roster_entries() -> list[dict[str, str | None]]:
    """Role picker rows: name, display_name, bank_id."""
    from hivepilot import roles

    by_name: dict[str, dict[str, str | None]] = {}
    for role in roles.list_roles():
        by_name[role.name] = {
            "name": role.name,
            "display_name": role.display_name,
            "bank_id": role_bank_id(role.name),
        }
    try:
        for row in roles.api_roster():
            if not isinstance(row, Mapping):
                continue
            name = row.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            name = name.strip()
            display = row.get("display_name")
            by_name.setdefault(
                name,
                {
                    "name": name,
                    "display_name": display if isinstance(display, str) else None,
                    "bank_id": role_bank_id(name),
                },
            )
    except Exception:  # noqa: BLE001
        logger.debug("hindsight_panel.roster_store_failed", exc_info=True)
    return sorted(by_name.values(), key=lambda item: item["name"] or "")


def default_client() -> Any | None:
    return hindsight_sdk_client()


def _wrap_client(raw: Any) -> PanelClient:
    if raw is None:
        raise PanelError(_NO_CLIENT, status_code=503)
    if all(
        hasattr(raw, name)
        for name in (
            "list_mental_models",
            "create_mental_model",
            "update_mental_model",
            "refresh_mental_model",
            "list_memories",
        )
    ):
        return raw
    return SdkPanelClient(raw)


class SdkPanelClient:
    """Adapter over the HTTP SDK: top-level methods plus ``client.memory``."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def list_mental_models(self, bank_id: str, **kwargs: Any) -> Any:
        return self._raw.list_mental_models(bank_id, **kwargs)

    def create_mental_model(self, bank_id: str, **kwargs: Any) -> Any:
        return self._raw.create_mental_model(bank_id=bank_id, **kwargs)

    def update_mental_model(self, bank_id: str, mental_model_id: str, **kwargs: Any) -> Any:
        return self._raw.update_mental_model(bank_id, mental_model_id, **kwargs)

    def refresh_mental_model(self, bank_id: str, mental_model_id: str) -> Any:
        return self._raw.refresh_mental_model(bank_id, mental_model_id)

    def list_memories(self, bank_id: str, **kwargs: Any) -> Any:
        if hasattr(self._raw, "list_memories"):
            return self._raw.list_memories(bank_id, **kwargs)
        memory = getattr(self._raw, "memory", None)
        if memory is not None and hasattr(memory, "list_memories"):
            return memory.list_memories(bank_id=bank_id, **kwargs)
        raise PanelError("Hindsight client has no list_memories")

    def update_memory(self, bank_id: str, memory_id: str, **kwargs: Any) -> Any:
        memory = getattr(self._raw, "memory", None)
        if memory is not None and hasattr(memory, "update_memory"):
            try:
                return memory.update_memory(bank_id=bank_id, memory_id=memory_id, **kwargs)
            except TypeError:
                return memory.update_memory(bank_id, memory_id, **kwargs)
        if hasattr(self._raw, "update_memory"):
            try:
                return self._raw.update_memory(bank_id=bank_id, memory_id=memory_id, **kwargs)
            except TypeError:
                return self._raw.update_memory(bank_id, memory_id, **kwargs)
        raise PanelError("Hindsight client has no update_memory")


def _require_client(client: Any | None, *, enabled: bool | None) -> PanelClient:
    on = _enabled() if enabled is None else enabled
    if not on:
        raise PanelError(_DISABLED, status_code=503)
    raw = client if client is not None else default_client()
    if raw is None:
        raise PanelError(_NO_CLIENT, status_code=503)
    return _wrap_client(raw)


def parse_mental_model(item: Any) -> dict[str, Any]:
    """Normalize one mental-model row for Pollen."""
    ident = _as_str(_pick(item, "id", "mental_model_id")) or ""
    return {
        "id": ident,
        "name": _as_str(_pick(item, "name", "title")) or ident,
        "source_query": _as_str(_pick(item, "source_query", "query")) or "",
        "content": _as_str(_pick(item, "content", "text")) or "",
        "last_refreshed_at": _as_str(_pick(item, "last_refreshed_at", "updated_at")),
        "is_stale": _as_bool(_pick(item, "is_stale", "stale")),
        "tags": _as_str_list(_pick(item, "tags")),
    }


def _quote_from(item: Any) -> dict[str, str] | None:
    text = _as_str(_pick(item, "text", "quote", "content", "memory"))
    if not text:
        if isinstance(item, str) and item.strip():
            return {"text": item.strip(), "source_id": ""}
        return None
    source = _as_str(_pick(item, "id", "source_id", "memory_id")) or ""
    return {"text": text, "source_id": source}


def _evidence_from(item: Any) -> dict[str, Any] | None:
    ident = _as_str(_pick(item, "id", "memory_id", "source_id"))
    text = _as_str(_pick(item, "text", "content", "memory"))
    fact_type = (_as_str(_pick(item, "fact_type", "type")) or "").lower()
    if fact_type == "observation":
        return None
    if not ident and not text:
        return None
    return {
        "id": ident or "",
        "text": text or "",
        "fact_type": fact_type or "world",
        "state": _as_str(_pick(item, "state")) or "valid",
    }


def _collect_nested(item: Any, *keys: str) -> list[Any]:
    collected: list[Any] = []
    for key in keys:
        value = _pick(item, key)
        if value is None:
            continue
        if isinstance(value, list):
            collected.extend(value)
        else:
            collected.append(value)
    based = _pick(item, "based_on", "reflect_response")
    if based is not None and based is not item:
        collected.extend(
            _collect_nested(based, "facts", "memories", "evidence", "proofs", "sources")
        )
    return collected


def parse_observation(item: Any) -> dict[str, Any]:
    """Normalize one observation (or a source fact listed beside them)."""
    ident = _as_str(_pick(item, "id", "memory_id")) or ""
    fact_type = (_as_str(_pick(item, "fact_type", "type")) or "observation").lower()
    nested = _collect_nested(item, "proofs", "quotes", "evidence", "sources", "facts", "memories")
    quotes: list[dict[str, str]] = []
    evidence: list[dict[str, Any]] = []
    seen_quotes: set[str] = set()
    seen_evidence: set[str] = set()
    for raw in nested:
        nested_type = (_as_str(_pick(raw, "fact_type", "type")) or "").lower()
        quote = _quote_from(raw)
        if quote and quote["text"] not in seen_quotes and nested_type != "observation":
            seen_quotes.add(quote["text"])
            quotes.append(quote)
        ev = _evidence_from(raw)
        if ev:
            key = ev["id"] or ev["text"]
            if key and key not in seen_evidence:
                seen_evidence.add(key)
                evidence.append(ev)
    proof_count = _as_int(_pick(item, "proof_count", "n_proofs", "proofs_count"))
    if proof_count is None:
        proof_count = len(quotes)
    confidence = _as_float(_pick(item, "confidence"))
    if confidence is None:
        scores = _pick(item, "scores")
        confidence = _as_float(_pick(scores, "final", "confidence")) if scores is not None else None
        if confidence is None:
            confidence = _as_float(_pick(item, "score"))
    return {
        "id": ident,
        "text": _as_str(_pick(item, "text", "content", "memory")) or "",
        "fact_type": fact_type,
        "state": _as_str(_pick(item, "state")) or "valid",
        "proof_count": proof_count,
        "confidence": confidence,
        "quotes": quotes,
        "evidence": evidence,
        "edited_at": _as_str(_pick(item, "edited_at")),
    }


def panel_status(*, enabled: bool | None = None, client: Any | None = None) -> dict[str, Any]:
    """Role picker + whether the Hindsight extra/server is usable."""
    on = _enabled() if enabled is None else enabled
    raw = client if client is not None else (default_client() if on else None)
    if not on:
        detail = _DISABLED
        configured = False
    elif raw is None:
        detail = _NO_CLIENT
        configured = False
    else:
        detail = None
        configured = True
    payload: dict[str, Any] = {
        "configured": configured,
        "roles": roster_entries(),
    }
    if detail:
        payload["detail"] = detail
    return payload


def role_panel(
    role: str,
    *,
    client: Any | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Mental models + observations for ``role:{name}``."""
    resolved = resolve_role(role)
    bank = role_bank_id(resolved)
    on = _enabled() if enabled is None else enabled
    raw = client if client is not None else (default_client() if on else None)
    if not on:
        return {
            "configured": False,
            "role": resolved,
            "bank_id": bank,
            "mental_models": [],
            "observations": [],
            "detail": _DISABLED,
        }
    if raw is None:
        return {
            "configured": False,
            "role": resolved,
            "bank_id": bank,
            "mental_models": [],
            "observations": [],
            "detail": _NO_CLIENT,
        }
    adapter = _wrap_client(raw)
    models: list[dict[str, Any]] = []
    observations: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        models = [
            parse_mental_model(item)
            for item in _items(adapter.list_mental_models(bank, detail="content"))
        ]
    except Exception as exc:  # noqa: BLE001 — GET must degrade
        logger.warning("hindsight_panel.list_models_failed", role=resolved, error=str(exc))
        errors.append(f"mental_models: {exc}")
    try:
        observations = [
            parse_observation(item)
            for item in _items(adapter.list_memories(bank, type="observation"))
        ]
    except Exception as exc:  # noqa: BLE001
        logger.warning("hindsight_panel.list_observations_failed", role=resolved, error=str(exc))
        errors.append(f"observations: {exc}")
    payload: dict[str, Any] = {
        "configured": True,
        "role": resolved,
        "bank_id": bank,
        "mental_models": models,
        "observations": observations,
    }
    if errors:
        payload["detail"] = "; ".join(errors)
    return payload


def create_mental_model(
    role: str,
    *,
    name: str,
    source_query: str,
    tags: Sequence[str] | None = None,
    client: Any | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    resolved = resolve_role(role)
    adapter = _require_client(client, enabled=enabled)
    bank = role_bank_id(resolved)
    try:
        result = adapter.create_mental_model(
            bank,
            name=name.strip(),
            source_query=source_query.strip(),
            **({"tags": list(tags)} if tags else {}),
        )
    except PanelError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PanelError(str(exc)) from exc
    parsed = parse_mental_model(result)
    operation_id = _as_str(_pick(result, "operation_id"))
    return {
        "ok": True,
        "role": resolved,
        "bank_id": bank,
        "mental_model": parsed,
        "operation_id": operation_id,
    }


def update_mental_model(
    role: str,
    mental_model_id: str,
    *,
    name: str | None = None,
    source_query: str | None = None,
    client: Any | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    resolved = resolve_role(role)
    adapter = _require_client(client, enabled=enabled)
    bank = role_bank_id(resolved)
    kwargs: dict[str, Any] = {}
    if name is not None:
        kwargs["name"] = name.strip()
    if source_query is not None:
        kwargs["source_query"] = source_query.strip()
    if not kwargs:
        raise PanelError("nothing to update", status_code=400)
    try:
        result = adapter.update_mental_model(bank, mental_model_id, **kwargs)
    except PanelError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PanelError(str(exc)) from exc
    return {
        "ok": True,
        "role": resolved,
        "bank_id": bank,
        "mental_model": parse_mental_model(result),
    }


def refresh_mental_model(
    role: str,
    mental_model_id: str,
    *,
    client: Any | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    resolved = resolve_role(role)
    adapter = _require_client(client, enabled=enabled)
    bank = role_bank_id(resolved)
    try:
        result = adapter.refresh_mental_model(bank, mental_model_id)
    except PanelError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PanelError(str(exc)) from exc
    return {
        "ok": True,
        "role": resolved,
        "bank_id": bank,
        "mental_model_id": mental_model_id,
        "operation_id": _as_str(_pick(result, "operation_id")),
    }


def curate_memory(
    role: str,
    memory_id: str,
    *,
    text: str | None = None,
    reason: str | None = None,
    state: str | None = None,
    client: Any | None = None,
    enabled: bool | None = None,
) -> dict[str, Any]:
    """Edit or invalidate a *source* fact (not an observation)."""
    resolved = resolve_role(role)
    adapter = _require_client(client, enabled=enabled)
    bank = role_bank_id(resolved)
    kwargs: dict[str, Any] = {}
    if text is not None:
        kwargs["text"] = text.strip()
    if reason is not None:
        kwargs["reason"] = reason.strip()
    if state is not None:
        kwargs["state"] = state
    if not kwargs:
        raise PanelError("nothing to update", status_code=400)
    try:
        result = adapter.update_memory(bank, memory_id, **kwargs)
    except PanelError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PanelError(str(exc)) from exc
    return {
        "ok": True,
        "role": resolved,
        "bank_id": bank,
        "memory_id": memory_id,
        "memory": parse_observation(result) if result is not None else None,
    }
