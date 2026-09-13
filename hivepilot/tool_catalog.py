"""HP-95 tool catalog — risk × policy × volatile × idempotency.

Coworker pattern (``src/shared/tool-catalog.ts``), rewritten in Python.
Intrinsic classification lives in ``tool_catalog.yaml``. ``policies.yaml``
may override **policy** (allow / deny / require_approval) only — never
``risk``. YAML axes (HP-95 / Coworker): ``risk``, ``defaultPolicy``,
``volatile``, ``idempotency``.

Unknown tool token → deny (fail-closed). No catch-all ``*`` ships in the
default YAML; an unmatched token is unknown, not "unclassified allow".

Orthogonal axes — do **not** merge these into risk tiers:

- outward tokens (``hivepilot.outward.OUTWARD_ACTIONS``) — may this become
  visible outside the machine?
- plugin capabilities (``hivepilot.plugin_capabilities.PLUGIN_CAPABILITIES``)
  — network | filesystem | subprocess | secrets_access | env

HP-94 Approvals inbox ``kind`` for a catalog decision is always ``tool``.
HP-61 / HP-86 ``change_class`` stays a separate field: a policy override
cannot widen a high/critical tool to ``allow`` unless the caller supplies
``change_class=mechanical``. This module does not persist rules or replace
the existing approval-rule table.

Helpers here are for the future HP approval path. They do not execute
typed tools and do not change ``GET /v1/tools``.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

from hivepilot.config import settings
from hivepilot.outward import OUTWARD_ACTIONS
from hivepilot.plugin_capabilities import PLUGIN_CAPABILITIES
from hivepilot.services.approval_rules_service import MECHANICAL, normalize_change_class
from hivepilot.services.policy_service import get_policy
from hivepilot.services.typed_tools import TypedTool

# Closed vocabularies. Adding a token is additive; removing/renaming is a
# breaking change for YAML already referencing it.
RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high", "critical")
POLICIES: tuple[str, ...] = ("allow", "deny", "require_approval")
HIGH_RISKS: frozenset[str] = frozenset({"high", "critical"})

# HP-94: one Approvals inbox. ``kind`` is orthogonal to HP-61 change_class.
APPROVAL_KINDS: frozenset[str] = frozenset({"partition", "tool", "memory", "skill_evolution"})
TOOL_APPROVAL_KIND = "tool"


class ToolCatalogError(ValueError):
    """Invalid catalog YAML or policy override."""


@dataclass(frozen=True)
class ToolEntry:
    """One intrinsic catalog row. ``risk`` is immutable after load."""

    token: str
    risk: str
    default_policy: str
    volatile: bool
    idempotent: bool
    source_kind: str = ""
    qualified_name: str = ""

    # Linear / Coworker YAML field names (HP-95).
    @property
    def defaultPolicy(self) -> str:
        return self.default_policy

    @property
    def idempotency(self) -> bool:
        return self.idempotent

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "risk": self.risk,
            "defaultPolicy": self.default_policy,
            "volatile": self.volatile,
            "idempotency": self.idempotent,
            "source_kind": self.source_kind,
            "qualified_name": self.qualified_name,
        }


@dataclass(frozen=True)
class ToolDecision:
    """Resolution of one tool token. ``risk`` is always the catalog value."""

    token: str
    known: bool
    risk: str
    policy: str
    default_policy: str
    volatile: bool
    idempotent: bool
    overridden: bool
    approval_kind: str = TOOL_APPROVAL_KIND

    @property
    def allowed(self) -> bool:
        return self.known and self.policy == "allow"

    @property
    def denied(self) -> bool:
        return self.policy == "deny" or not self.known

    @property
    def needs_approval(self) -> bool:
        return self.known and self.policy == "require_approval"

    @property
    def defaultPolicy(self) -> str:
        return self.default_policy

    @property
    def idempotency(self) -> bool:
        return self.idempotent

    def to_dict(self) -> dict[str, Any]:
        return {
            "token": self.token,
            "known": self.known,
            "risk": self.risk,
            "policy": self.policy,
            "defaultPolicy": self.default_policy,
            "volatile": self.volatile,
            "idempotency": self.idempotent,
            "overridden": self.overridden,
            "approval_kind": self.approval_kind,
            "allowed": self.allowed,
            "denied": self.denied,
            "needs_approval": self.needs_approval,
        }


@dataclass(frozen=True)
class ToolCatalog:
    entries: tuple[ToolEntry, ...]
    path: Path | None = None


_cache: dict[str, ToolCatalog] = {}

_GLOB_CHARS = frozenset("*?[")


def _is_glob(pattern: str) -> bool:
    return bool(_GLOB_CHARS & set(pattern))


def _name_matches(pattern: str, value: str) -> bool:
    if not pattern:
        return True
    if _is_glob(pattern):
        return fnmatch.fnmatchcase(value, pattern)
    return pattern == value


def _specificity(entry: ToolEntry) -> int:
    score = 0
    if entry.token:
        score += 40 if _is_glob(entry.token) else 100
    if entry.qualified_name:
        score += 30 if _is_glob(entry.qualified_name) else 80
    if entry.source_kind:
        score += 20
    return score


def _normalize_token(raw: object) -> str:
    if not isinstance(raw, str):
        return ""
    return raw.strip()


def _require_enum(value: object, allowed: tuple[str, ...], *, field: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ToolCatalogError(
            f"tool catalog {field} must be one of {list(allowed)}; got {value!r}"
        )
    return value


def _require_bool(value: object, *, field: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ToolCatalogError(f"tool catalog {field} must be a bool; got {value!r}")
    return value


def _parse_entry(raw: object, index: int) -> ToolEntry:
    if not isinstance(raw, dict):
        raise ToolCatalogError(f"tool catalog tools[{index}] must be a mapping")
    token = _normalize_token(raw.get("token"))
    match = raw.get("match") or {}
    if match and not isinstance(match, dict):
        raise ToolCatalogError(f"tool catalog tools[{index}].match must be a mapping")
    source_kind = _normalize_token(match.get("source_kind") if isinstance(match, dict) else "")
    qualified_name = _normalize_token(
        match.get("qualified_name") if isinstance(match, dict) else ""
    )
    if not token and not source_kind and not qualified_name:
        raise ToolCatalogError(
            f"tool catalog tools[{index}] needs token or match.qualified_name/source_kind"
        )
    return ToolEntry(
        token=token,
        risk=_require_enum(raw.get("risk"), RISK_LEVELS, field="risk"),
        default_policy=_require_enum(
            raw.get("defaultPolicy", raw.get("default_policy")),
            POLICIES,
            field="defaultPolicy",
        ),
        volatile=_require_bool(raw.get("volatile"), field="volatile", default=False),
        idempotent=_require_bool(
            raw.get("idempotency", raw.get("idempotent")),
            field="idempotency",
            default=False,
        ),
        source_kind=source_kind,
        qualified_name=qualified_name,
    )


def _empty_catalog(path: Path | None) -> ToolCatalog:
    return ToolCatalog(entries=(), path=path)


def load_catalog(path: Path | None = None, *, force: bool = False) -> ToolCatalog:
    """Load ``tool_catalog.yaml``. Missing file → empty catalog (deny-all)."""
    resolved = settings.resolve_config_path(path or settings.tool_catalog_file)
    cache_key = str(resolved)
    if not force and cache_key in _cache:
        return _cache[cache_key]
    if not resolved.exists():
        catalog = _empty_catalog(resolved)
        _cache[cache_key] = catalog
        return catalog
    with resolved.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ToolCatalogError("tool_catalog.yaml must be a mapping")
    tools_raw = raw.get("tools")
    if tools_raw is None:
        catalog = _empty_catalog(resolved)
        _cache[cache_key] = catalog
        return catalog
    if not isinstance(tools_raw, list):
        raise ToolCatalogError("tool_catalog.yaml key 'tools' must be a list")
    entries = tuple(_parse_entry(item, index) for index, item in enumerate(tools_raw))
    catalog = ToolCatalog(entries=entries, path=resolved)
    _cache[cache_key] = catalog
    return catalog


def reload_catalog() -> None:
    """Drop the in-process catalog cache (tests / config reload)."""
    _cache.clear()


def lint_catalog(path: Path | None = None) -> list[str]:
    """Return catalog problems. Missing file is not a problem (deny-all)."""
    try:
        resolved = settings.resolve_config_path(path or settings.tool_catalog_file)
        if not resolved.exists():
            return []
        load_catalog(resolved, force=True)
    except ToolCatalogError as exc:
        return [str(exc)]
    except yaml.YAMLError as exc:
        return [f"YAML parse error in tool_catalog.yaml: {exc}"]
    return []


def _entry_matches(
    entry: ToolEntry,
    *,
    token: str,
    source_kind: str,
    qualified_name: str,
) -> bool:
    if entry.token and not _name_matches(entry.token, token):
        # Also allow a token pattern to match the qualified name when the
        # caller resolved a typed-tool row (mcp__src__local).
        if not (qualified_name and _name_matches(entry.token, qualified_name)):
            return False
    if entry.source_kind and entry.source_kind != source_kind:
        return False
    if entry.qualified_name and not _name_matches(entry.qualified_name, qualified_name or token):
        return False
    return bool(entry.token or entry.source_kind or entry.qualified_name)


def _lookup(
    catalog: ToolCatalog,
    *,
    token: str,
    source_kind: str,
    qualified_name: str,
) -> ToolEntry | None:
    hits = [
        entry
        for entry in catalog.entries
        if _entry_matches(
            entry, token=token, source_kind=source_kind, qualified_name=qualified_name
        )
    ]
    if not hits:
        return None
    hits.sort(key=_specificity, reverse=True)
    return hits[0]


def _normalize_override(raw: object) -> str:
    if raw is None or raw == "":
        return ""
    if not isinstance(raw, str) or raw not in POLICIES:
        raise ToolCatalogError(
            f"tool_policies override must be one of {list(POLICIES)}; got {raw!r}"
        )
    return raw


def _override_for(token: str, overrides: Mapping[str, str]) -> str:
    """Most-specific matching key in ``tool_policies`` (exact > glob)."""
    exact = overrides.get(token)
    if exact is not None:
        return _normalize_override(exact)
    globs = [
        (pattern, value)
        for pattern, value in overrides.items()
        if _is_glob(pattern) and _name_matches(pattern, token)
    ]
    if not globs:
        return ""
    globs.sort(key=lambda item: item[0].count("*") + item[0].count("?"))
    return _normalize_override(globs[0][1])


def _effective_policy(entry: ToolEntry, override: str, change_class: str) -> str:
    """Apply a policy override without ever touching ``entry.risk``.

    High / critical cannot become ``allow`` unless HP-86 ``change_class``
    is ``mechanical``. Tightening to ``deny`` / ``require_approval`` is
    always allowed.
    """
    policy = override if override in POLICIES else entry.default_policy
    claimed = normalize_change_class(change_class, persist=False)
    if policy == "allow" and entry.risk in HIGH_RISKS and claimed != MECHANICAL:
        return "require_approval"
    return policy


def _unknown_decision(token: str) -> ToolDecision:
    return ToolDecision(
        token=token,
        known=False,
        risk="",
        policy="deny",
        default_policy="deny",
        volatile=True,
        idempotent=False,
        overridden=False,
        approval_kind=TOOL_APPROVAL_KIND,
    )


def resolve(
    token: str,
    *,
    project: str | None = None,
    change_class: str = "",
    catalog: ToolCatalog | None = None,
    policy_overrides: Mapping[str, str] | None = None,
    source_kind: str | None = None,
    qualified_name: str | None = None,
) -> ToolDecision:
    """Resolve a tool token. Unknown → deny. ``risk`` is never mutated."""
    cleaned = _normalize_token(token)
    if not cleaned:
        return _unknown_decision("")
    loaded = catalog if catalog is not None else load_catalog()
    qn = _normalize_token(qualified_name) or cleaned
    kind = _normalize_token(source_kind)
    entry = _lookup(loaded, token=cleaned, source_kind=kind, qualified_name=qn)
    if entry is None:
        return _unknown_decision(cleaned)

    overrides: Mapping[str, str]
    if policy_overrides is not None:
        overrides = policy_overrides
    elif project is not None:
        overrides = get_policy(project).tool_policies
    else:
        overrides = {}

    override = _override_for(cleaned, overrides)
    if not override and qn != cleaned:
        override = _override_for(qn, overrides)
    policy = _effective_policy(entry, override, change_class)
    return ToolDecision(
        token=cleaned,
        known=True,
        risk=entry.risk,
        policy=policy,
        default_policy=entry.default_policy,
        volatile=entry.volatile,
        idempotent=entry.idempotent,
        overridden=bool(override) and policy != entry.default_policy,
        approval_kind=TOOL_APPROVAL_KIND,
    )


def resolve_typed_tool(
    tool: TypedTool,
    *,
    project: str | None = None,
    change_class: str = "",
    catalog: ToolCatalog | None = None,
    policy_overrides: Mapping[str, str] | None = None,
) -> ToolDecision:
    """Resolve an HP-58 typed-tool row without changing its schema."""
    return resolve(
        tool.qualified_name,
        project=project,
        change_class=change_class,
        catalog=catalog,
        policy_overrides=policy_overrides,
        source_kind=tool.source_kind,
        qualified_name=tool.qualified_name,
    )


def approval_payload(decision: ToolDecision) -> dict[str, Any]:
    """Tiny hook for the HP-94 Approvals inbox. Does not persist HP-61 rules.

    ``kind`` is always ``tool``. Callers that want auto-approve must still
    pass HP-86 ``change_class=mechanical`` into :func:`resolve` — this
    helper never invents mechanical.
    """
    return {
        "kind": decision.approval_kind,
        "token": decision.token,
        "risk": decision.risk,
        "policy": decision.policy,
        "volatile": decision.volatile,
        "idempotency": decision.idempotent,
        "known": decision.known,
    }


def axes_are_orthogonal() -> bool:
    """True when catalog vocabularies share no tokens with the other axes.

    Used by tests; also documents the invariant in one place. Risk / policy
    are not plugin capabilities and are not outward actions.
    """
    catalog_tokens = set(RISK_LEVELS) | set(POLICIES)
    return catalog_tokens.isdisjoint(PLUGIN_CAPABILITIES) and catalog_tokens.isdisjoint(
        OUTWARD_ACTIONS
    )
