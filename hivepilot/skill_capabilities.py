"""HP-108 skill→tools gate for concierge/chat.

Coworker ``skill-capabilities.ts`` pattern, rewritten in Python. This
module does **not** vendor Coworker TS/Electron, keyword-match a chat
message to force-load skills, change ``PipelineStage.skills`` /
``hivepilot stage attach-skill``, or apply HP-109 / HP-112 / HP-114.

Contracts:

- A skill maps to HP-95 catalog tool tokens (``Read``, ``Bash``, …).
- Concierge/chat tool resolution drops tokens whose owning skills are
  **off**. HP-105 ``enabled=False`` is the off switch. A cataloged
  skill with no trust row stays on. Names missing from the catalog
  grant nothing.
- Tokens not claimed by any skill pass through the role allowlist.
- A token claimed by several skills stays if **any** owner is on.
- The classifier stays no-tools (``concierge_service._CLASSIFIER_NO_TOOLS``).
- Attach stays explicit YAML. No query/keyword argument loads a skill.
"""

from __future__ import annotations

import contextvars
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

import yaml

from hivepilot.plugins import PluginManager
from hivepilot.skill_catalog import SkillCatalog
from hivepilot.skill_trust import get as get_trust

# Bundled read-only audit skill — used when SKILL.md has no allowed-tools.
DEFAULT_SKILL_TOOLS: dict[str, tuple[str, ...]] = {
    "improve": ("Read", "Grep", "Glob"),
}

_FRONTMATTER = re.compile(
    r"\A---[ \t]*\r?\n(?P<body>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
_SPLIT_TOOLS = re.compile(r"[\s,]+")

_CHAT_SURFACE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "hivepilot_chat_skill_tool_gate", default=False
)


class SkillCapabilitiesError(ValueError):
    """Invalid skill→tools map or resolve arguments."""


def catalog_token(raw: str) -> str:
    """Bare HP-95 token. ``Bash(gh:*)`` → ``Bash``."""
    cleaned = (raw or "").strip()
    if not cleaned:
        return ""
    if "(" in cleaned:
        return cleaned.split("(", 1)[0].strip()
    return cleaned


def parse_skill_tool_tokens(files: Mapping[str, str] | None) -> tuple[str, ...]:
    """Read ``allowed-tools`` / ``allowed_tools`` from ``SKILL.md`` frontmatter."""
    if not files:
        return ()
    raw = files.get("SKILL.md")
    if raw is None:
        raw = files.get("skill.md")
    if not raw:
        return ()
    match = _FRONTMATTER.match(raw)
    if match is None:
        return ()
    try:
        meta = yaml.safe_load(match.group("body"))
    except yaml.YAMLError:
        return ()
    if not isinstance(meta, dict):
        return ()
    value = meta.get("allowed-tools", meta.get("allowed_tools"))
    return _coerce_tool_list(value)


def _coerce_tool_list(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        parts = [part.strip() for part in _SPLIT_TOOLS.split(value) if part.strip()]
        return tuple(parts)
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def build_capability_map(
    catalog: SkillCatalog | None = None,
    *,
    overlay: Mapping[str, Sequence[str]] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Skill name → tokens. Overlay wins, then frontmatter, then defaults.

    Bundled defaults (``improve``) claim tokens only when that skill is in
    the catalog and ``SKILL.md`` has no ``allowed-tools``. An empty catalog
    must not strip ``Read`` / ``Grep`` / ``Glob`` from chat.
    """
    result: dict[str, tuple[str, ...]] = {}
    if catalog is not None:
        for revision in catalog.list_active():
            parsed = parse_skill_tool_tokens(revision.files)
            if parsed:
                result[revision.name] = parsed
            elif revision.name in DEFAULT_SKILL_TOOLS:
                result[revision.name] = DEFAULT_SKILL_TOOLS[revision.name]
    if overlay:
        for name, tokens in overlay.items():
            key = (name or "").strip()
            if not key:
                raise SkillCapabilitiesError("capability map skill name is required")
            result[key] = tuple(str(token).strip() for token in tokens if str(token).strip())
    return result


def default_catalog() -> SkillCatalog:
    """Directory + plugin skills. Tests inject their own catalog."""
    return SkillCatalog().scan(plugin_manager=PluginManager())


def skill_is_on(
    name: str,
    *,
    tenant: str = "default",
    catalog: SkillCatalog | None = None,
) -> bool:
    """True when the skill is in the catalog and not explicitly disabled.

    HP-105 ``enabled=False`` is the off switch. A cataloged revision with
    no trust row stays on — scan does not register trust, so unknown must
    not strip tools. Names missing from the catalog are off.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        return False
    if catalog is None:
        catalog = default_catalog()
    revision = catalog.active_revision(cleaned)
    if revision is None:
        return False
    trust = get_trust(
        revision.revision_id,
        tenant=tenant,
        skill_name=cleaned,
        logical_id=revision.logical_id,
    )
    if trust.known and not trust.enabled:
        return False
    return True


def resolve_chat_tools(
    requested: Sequence[str],
    *,
    tenant: str = "default",
    catalog: SkillCatalog | None = None,
    capability_map: Mapping[str, Sequence[str]] | None = None,
) -> list[str]:
    """Filter a concierge/chat allowlist. Never adds tools from message text.

    A requested token is dropped when it is mapped to one or more skills
    and every owner is off. Unmapped tokens are unchanged. Order is kept.
    """
    if catalog is None:
        catalog = default_catalog()
    cap_map = build_capability_map(catalog, overlay=capability_map)
    granted: set[str] = set()
    mapped: set[str] = set()
    for skill_name, tokens in cap_map.items():
        bases = {catalog_token(token) for token in tokens if catalog_token(token)}
        mapped.update(bases)
        if skill_is_on(skill_name, tenant=tenant, catalog=catalog):
            granted.update(bases)

    kept: list[str] = []
    seen: set[str] = set()
    for raw in requested:
        original = (raw or "").strip()
        if not original or original in seen:
            continue
        base = catalog_token(original)
        if base in mapped and base not in granted:
            continue
        seen.add(original)
        kept.append(original)
    return kept


@contextmanager
def chat_tool_surface() -> Iterator[None]:
    """Mark this thread as concierge/chat so role allowlists are gated."""
    token = _CHAT_SURFACE.set(True)
    try:
        yield
    finally:
        _CHAT_SURFACE.reset(token)


def chat_surface_active() -> bool:
    return bool(_CHAT_SURFACE.get())


def gate_chat_allowed_tools(
    tools: list[str] | None,
    *,
    tenant: str = "default",
    catalog: SkillCatalog | None = None,
    capability_map: Mapping[str, Sequence[str]] | None = None,
) -> list[str] | None:
    """Apply the gate only on the chat surface. Pipelines pass through."""
    if not chat_surface_active():
        return list(tools) if tools else tools
    if tools is None:
        return None
    return resolve_chat_tools(
        tools,
        tenant=tenant,
        catalog=catalog,
        capability_map=capability_map,
    )


def on_chat_surface(fn: Callable[..., Any], /, **kwargs: Any) -> Any:
    """Run ``fn`` with the concierge/chat skill→tools gate active."""
    with chat_tool_surface():
        return fn(**kwargs)
