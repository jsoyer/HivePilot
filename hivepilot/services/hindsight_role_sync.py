"""Push each HivePilot role into its own Hindsight bank (HP-52).

Two bank namespaces — do not collapse them:

- HP-51 episodic retain/recall: ``{project}:{task}:{role}``
- HP-52 role identity: ``role:{name}``

Hindsight names three identity knobs. HivePilot maps only what it already
owns:

=============  ==============================================  =======
Hindsight      HivePilot source                                v1
=============  ==============================================  =======
Mission        full role prompt (file or materialized store)   yes
Directives     ``get_rules_for_role()`` (paths + prose)        yes
Disposition    skepticism / literalism / empathy 1–5           **no**
=============  ==============================================  =======

Disposition is not a field on ``Role`` (``display_name`` is a nickname).
This module never sends ``disposition_*`` kwargs, so Hindsight keeps its
own defaults. ``allowed_tools`` / ``permission_mode`` are execution gates,
not directives.

Mission and directives only affect Hindsight ``reflect`` (HP-54). This
slice writes bank config; it does not call ``reflect``.

Dormant unless ``HIVEPILOT_HINDSIGHT_ENABLED``. A missing extra, an
unreachable server, or a raising SDK call never fails ``refresh_roles``.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

ROLE_BANK_PREFIX = "role:"
DIRECTIVE_TAG = "hivepilot"
DIRECTIVE_NAME_PREFIX = "hp-rule-"
_MUST_READ = "MUST read before acting: "


@dataclass(frozen=True)
class RoleBankPayload:
    """What we would send. Disposition is intentionally absent."""

    bank_id: str
    role_name: str
    reflect_mission: str
    directives: tuple[str, ...]


@dataclass(frozen=True)
class ManagedDirective:
    """One Hindsight directive we created (or would replace)."""

    directive_id: str
    name: str
    content: str
    tags: tuple[str, ...] = ()


class RoleBankAdapter(Protocol):
    """Narrow surface over ``hindsight_client.Hindsight``. Tests fake this."""

    def ensure_bank(self, bank_id: str, *, reflect_mission: str) -> None: ...

    def upsert_config(self, bank_id: str, *, reflect_mission: str) -> None: ...

    def list_directives(self, bank_id: str) -> Sequence[ManagedDirective]: ...

    def create_directive(self, bank_id: str, *, name: str, content: str) -> None: ...

    def delete_directive(self, bank_id: str, directive_id: str) -> None: ...


def role_bank_id(name: str) -> str:
    """Stable identity bank for a role. Distinct from the HP-51 run bank."""
    return f"{ROLE_BANK_PREFIX}{name}"


def is_rule_path(rule: str) -> bool:
    """True for a file path the agent must read; false for a prose statement."""
    stripped = rule.strip()
    if not stripped:
        return False
    lower = stripped.lower()
    if lower.startswith(("http://", "https://")):
        return False
    if stripped.startswith(("/", "./", "../")):
        return True
    if " " in stripped:
        return False
    return "/" in stripped or stripped.endswith((".md", ".txt", ".rules"))


def directive_from_rule(rule: str) -> str:
    """Path-like rules become an explicit read-before-acting instruction."""
    stripped = rule.strip()
    if not stripped:
        return ""
    if is_rule_path(stripped):
        return f"{_MUST_READ}{stripped}"
    return stripped


def managed_directive_name(content: str) -> str:
    """Content-addressed name so a no-op refresh does not recreate directives."""
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    return f"{DIRECTIVE_NAME_PREFIX}{digest}"


def role_prompt_text(role: Any) -> str:
    """Read the live prompt. Store-backed roles already materialize to a file."""
    path = getattr(role, "prompt_file", None)
    if path is None:
        return ""
    resolved = Path(path)
    try:
        if resolved.is_file():
            return resolved.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return ""


def build_role_payload(
    role: Any,
    *,
    rules: Sequence[str] | None = None,
) -> RoleBankPayload:
    """Map one ``Role`` onto a Hindsight bank payload. No network."""
    name = str(getattr(role, "name", "") or "")
    if rules is None:
        from hivepilot.agent_rules import get_rules_for_role

        rules = get_rules_for_role(name)
    directives = tuple(text for rule in rules if (text := directive_from_rule(rule)))
    return RoleBankPayload(
        bank_id=role_bank_id(name),
        role_name=name,
        reflect_mission=role_prompt_text(role),
        directives=directives,
    )


def _enabled() -> bool:
    from hivepilot.config import settings

    return bool(getattr(settings, "hindsight_enabled", False))


def _raw_sdk_client() -> Any | None:
    try:
        from hindsight_client import Hindsight  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 — optional extra
        return None

    from hivepilot.config import settings

    kwargs: dict[str, Any] = {
        "base_url": (
            getattr(settings, "hindsight_base_url", None) or "http://127.0.0.1:8888"
        ).strip()
    }
    api_key = (getattr(settings, "hindsight_api_key", None) or "").strip()
    if api_key:
        kwargs["api_key"] = api_key
    return Hindsight(**kwargs)


def hindsight_sdk_client() -> Any | None:
    """Shared HTTP SDK constructor (HP-51 retain/recall, HP-52 banks, HP-54 reflect)."""
    return _raw_sdk_client()


class SdkHindsightBankClient:
    """Adapter that only ever forwards mission + directives — never disposition."""

    def __init__(self, raw: Any) -> None:
        self._raw = raw

    def ensure_bank(self, bank_id: str, *, reflect_mission: str) -> None:
        # create_bank is create-or-update. Do not pass disposition_* even as None:
        # older generated clients serialise explicit nulls into the body.
        self._raw.create_bank(bank_id=bank_id, reflect_mission=reflect_mission)

    def upsert_config(self, bank_id: str, *, reflect_mission: str) -> None:
        self._raw.update_bank_config(bank_id, reflect_mission=reflect_mission)

    def list_directives(self, bank_id: str) -> list[ManagedDirective]:
        try:
            response = self._raw.list_directives(bank_id, tags=[DIRECTIVE_TAG])
        except TypeError:
            response = self._raw.list_directives(bank_id)
        return [
            item
            for item in _parse_directives(response)
            if item.name.startswith(DIRECTIVE_NAME_PREFIX)
        ]

    def create_directive(self, bank_id: str, *, name: str, content: str) -> None:
        self._raw.create_directive(bank_id, name=name, content=content, tags=[DIRECTIVE_TAG])

    def delete_directive(self, bank_id: str, directive_id: str) -> None:
        self._raw.delete_directive(bank_id, directive_id)


def default_adapter() -> RoleBankAdapter | None:
    raw = _raw_sdk_client()
    if raw is None:
        return None
    return SdkHindsightBankClient(raw)


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    return ()


def _parse_directives(response: Any) -> list[ManagedDirective]:
    if response is None:
        return []
    if isinstance(response, list):
        items = response
    elif isinstance(response, dict):
        # Check keys first — dict.items is a method, not the payload list.
        items = response.get("items") or response.get("directives") or response.get("data")
        if not isinstance(items, list):
            return []
    else:
        items = None
        for attr in ("items", "directives", "data"):
            if hasattr(response, attr):
                items = getattr(response, attr)
                break
        if not isinstance(items, list):
            return []
    parsed: list[ManagedDirective] = []
    for item in items:
        if isinstance(item, dict):
            did = str(item.get("id") or item.get("directive_id") or "")
            name = str(item.get("name") or "")
            content = str(item.get("content") or "")
            tags = _as_str_tuple(item.get("tags"))
        else:
            did = str(getattr(item, "id", None) or getattr(item, "directive_id", "") or "")
            name = str(getattr(item, "name", "") or "")
            content = str(getattr(item, "content", "") or "")
            tags = _as_str_tuple(getattr(item, "tags", None))
        if not did and not name:
            continue
        parsed.append(ManagedDirective(directive_id=did, name=name, content=content, tags=tags))
    return parsed


def _reconcile_directives(adapter: RoleBankAdapter, payload: RoleBankPayload) -> None:
    desired = {managed_directive_name(text): text for text in payload.directives}
    existing = {item.name: item for item in adapter.list_directives(payload.bank_id)}
    for name, content in desired.items():
        if name in existing:
            continue
        adapter.create_directive(payload.bank_id, name=name, content=content)
    for name, item in existing.items():
        if name in desired:
            continue
        if item.directive_id:
            adapter.delete_directive(payload.bank_id, item.directive_id)


def _push(adapter: RoleBankAdapter, payload: RoleBankPayload) -> None:
    try:
        adapter.ensure_bank(payload.bank_id, reflect_mission=payload.reflect_mission)
    except Exception as exc:  # noqa: BLE001 — bank may already exist
        logger.info(
            "hindsight_role_sync.ensure_bank",
            bank=payload.bank_id,
            error=str(exc),
        )
    try:
        adapter.upsert_config(payload.bank_id, reflect_mission=payload.reflect_mission)
    except Exception as exc:  # noqa: BLE001 — config API can be disabled server-side
        logger.warning("hindsight_role_sync.config_failed", bank=payload.bank_id, error=str(exc))
    try:
        _reconcile_directives(adapter, payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "hindsight_role_sync.directives_failed", bank=payload.bank_id, error=str(exc)
        )


def sync_role_to_hindsight(
    role: Any,
    *,
    client: RoleBankAdapter | None = None,
    enabled: bool | None = None,
) -> RoleBankPayload | None:
    """Push one role. Returns the payload when a push was attempted."""
    if not (enabled if enabled is not None else _enabled()):
        return None
    adapter = client if client is not None else default_adapter()
    if adapter is None:
        logger.info("hindsight_role_sync.skipped_no_client", role=getattr(role, "name", None))
        return None
    payload = build_role_payload(role)
    _push(adapter, payload)
    logger.info(
        "hindsight_role_sync.pushed",
        bank=payload.bank_id,
        directives=len(payload.directives),
        mission_chars=len(payload.reflect_mission),
    )
    return payload


def sync_all_roles(
    roles: Mapping[str, Any] | None = None,
    *,
    client: RoleBankAdapter | None = None,
    enabled: bool | None = None,
) -> int:
    """Push every role. Returns how many pushes were attempted (not HTTP 200s)."""
    if not (enabled if enabled is not None else _enabled()):
        return 0
    roster: Mapping[str, Any]
    if roles is None:
        from hivepilot.roles import ROLES

        roster = ROLES
    else:
        roster = roles
    pushed = 0
    for role in roster.values():
        try:
            if sync_role_to_hindsight(role, client=client, enabled=True) is not None:
                pushed += 1
        except Exception as exc:  # noqa: BLE001 — one role must not block the roster
            logger.warning(
                "hindsight_role_sync.role_failed",
                role=getattr(role, "name", None),
                error=str(exc),
            )
    return pushed
