"""Plugin capability manifest (Phase 26b) — an ADVISORY declaration surface
plus a fail-closed, load-time admission gate for what a plugin *intends* to
do (network access, filesystem writes, subprocess execution, secrets access,
raw environment reads), and a pure-`ast` static scanner used by the
`hivepilot plugins audit` CLI command.

IMPORTANT — what this is NOT: HivePilot plugins are imported and executed as
ordinary in-process Python. `hivepilot/plugins.py`'s `_load_plugin_module`
compiles and `exec()`s a plugin's source directly in the current
interpreter (see `docs/PLUGINS.md` "Trust model"). This module provides NO
interpreter-level sandboxing, no seccomp/subprocess isolation, and no import
hook that can actually PREVENT a plugin from calling `socket.socket()` or
`subprocess.run()` regardless of what it declared here. A plugin that lies
about its capabilities (or declares none at all) still runs with the full
privileges of the host process once loaded — identical honesty to the
existing `min_role` panel/skill gates (see `PanelInvalidMinRoleError` /
`SkillInvalidMinRoleError` in `hivepilot/plugins.py`).

What this DOES provide:

1. A declared, closed vocabulary of capability tokens (`PLUGIN_CAPABILITIES`)
   a plugin MAY declare via `register()["capabilities"] = [...]`.
2. A load-time admission gate (`validate_capabilities`) an operator controls
   via `settings.plugins_capability_policy` (`hivepilot/config.py`) — the set
   of capability tokens the operator is willing to ALLOW a plugin to
   declare. A plugin declaring a capability the operator has not allowed is
   DENIED AT LOAD (fail-closed; `plugins_capability_policy` defaults to `[]`
   — deny every declared capability), atomically rolling back that plugin's
   OTHER contributions exactly like a `PanelInvalidMinRoleError` /
   `SkillInvalidMinRoleError` (see `hivepilot/plugins.py`'s `_load_into`). A
   plugin that declares no capabilities at all (`register()` omits the
   `"capabilities"` key, or sets it to `None`) is completely unaffected —
   purely additive, backward-compatible with every plugin shipped before
   this manifest existed.
3. A read-only, best-effort STATIC scanner (`audit_plugin_source`) that
   `ast`-parses a plugin's source TEXT — never imports/execs it, never calls
   its `register()` — to flag risky imports/calls (subprocess, network
   sockets, `os.system`, `eval`/`exec`, write-mode `open`, `ctypes`, ...) and
   cross-reference them against that plugin's own declared `capabilities`
   manifest (itself extracted statically, best-effort, from a literal
   `"capabilities": [...]` entry in its `register()` return value) to surface
   UNDER-declaration. This is advisory, not exhaustive: a dynamically
   constructed capabilities list, or a risky call this scanner doesn't
   recognize, will not be caught.

True process-level isolation (subprocess sandboxing, seccomp, OS-level
capability dropping) is documented future work — see `docs/PLUGINS.md`.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

# A closed, documented vocabulary of declarable capability tokens. Adding a
# new token here is additive; removing/renaming one is a breaking change for
# any plugin/operator policy already referencing it.
PLUGIN_CAPABILITIES: tuple[str, ...] = (
    "network",  # outbound/inbound network I/O (sockets, HTTP clients, ...)
    "filesystem",  # reads/writes files outside the plugin's own module
    "subprocess",  # spawns child processes / shells out
    "secrets_access",  # resolves/reads secret values
    "env",  # reads raw process environment variables directly
)


class PluginCapabilityInvalidError(RuntimeError):
    """Raised when a plugin declares a `capabilities` manifest that is
    structurally invalid: not a list/tuple/set of strings, or containing a
    token outside the closed `PLUGIN_CAPABILITIES` vocabulary.

    Mirrors `PanelInvalidMinRoleError`'s fail-closed rationale
    (`hivepilot/plugins.py`): an unrecognized or malformed capability token
    is rejected at registration time rather than silently accepted, atomic
    with every other collision/validation error in the plugin loader (the
    whole plugin's staged contribution set rolls back).
    """


class PluginCapabilityDeniedError(RuntimeError):
    """Raised when a plugin declares a capability token that IS valid (one of
    `PLUGIN_CAPABILITIES`) but is NOT present in the operator's
    `settings.plugins_capability_policy` allow-list.

    Fail-closed by default: `plugins_capability_policy` defaults to `[]`, so
    ANY declared capability is denied unless the operator has explicitly
    opted it in. This is the load-time admission gate — a plugin requesting
    a capability the operator has not allowed never loads at all (atomic
    per-plugin rollback, same as every other validation error in the plugin
    loader).
    """


def validate_capabilities(name: str, declared: object, policy: frozenset[str]) -> frozenset[str]:
    """Validate a plugin's declared `capabilities` manifest against the closed
    `PLUGIN_CAPABILITIES` vocabulary and the operator's allow-list `policy`.

    - `declared is None` (the plugin's `register()` omitted `"capabilities"`
      entirely, or explicitly set it to `None`) -> returns `frozenset()`.
      Backward-compatible no-op: a plugin declaring nothing is unaffected
      regardless of how restrictive (or permissive) `policy` is.
    - `declared` is anything other than a `list`/`tuple`/`set` where every
      element is a `str` -> raises `PluginCapabilityInvalidError`.
      isinstance()-first, mirroring `PanelInvalidMinRoleError`'s check order,
      so a non-hashable/non-string value can never raise a raw `TypeError`.
    - any token in `declared` that is not one of `PLUGIN_CAPABILITIES` ->
      raises `PluginCapabilityInvalidError`, naming the unrecognized
      token(s).
    - any token in `declared` that IS a recognized capability but is not in
      `policy` -> raises `PluginCapabilityDeniedError`, naming the denied
      token(s). Fail-closed: an empty `policy` (the operator default) denies
      every declared capability.
    - otherwise -> returns `frozenset(declared)`.

    Error messages name only the plugin's own `name` and the offending
    capability token(s) — never any other plugin's/operator's state — matching
    the anti-leak discipline of every other validation error raised by the
    plugin loader.
    """
    if declared is None:
        return frozenset()

    if not isinstance(declared, (list, tuple, set)) or not all(
        isinstance(token, str) for token in declared
    ):
        raise PluginCapabilityInvalidError(
            f"plugin '{name}' declares an invalid capabilities manifest "
            f"{declared!r}; must be a list of strings"
        )

    declared_set = frozenset(declared)

    unknown = declared_set - frozenset(PLUGIN_CAPABILITIES)
    if unknown:
        raise PluginCapabilityInvalidError(
            f"plugin '{name}' declares unrecognized capability token(s) "
            f"{sorted(unknown)}; must be one of {sorted(PLUGIN_CAPABILITIES)}"
        )

    denied = declared_set - policy
    if denied:
        raise PluginCapabilityDeniedError(
            f"plugin '{name}' declares capability token(s) {sorted(denied)} "
            "not permitted by the operator's plugins_capability_policy "
            "allow-list; denied at load (fail-closed)"
        )

    return declared_set


# ---------------------------------------------------------------------------
# `plugins audit` static scanner — pure `ast`, never imports/execs a plugin.
# ---------------------------------------------------------------------------

# Maps a risky import's top-level module name to the `PLUGIN_CAPABILITIES`
# token it implies, or `None` when the import is risky but doesn't map
# cleanly onto the closed capability vocabulary (still flagged, just not
# used for under-declaration cross-referencing).
_IMPORT_CAPABILITY_MAP: dict[str, str | None] = {
    "subprocess": "subprocess",
    "socket": "network",
    "urllib": "network",
    "http": "network",
    "requests": "network",
    "ctypes": None,
}

_WRITE_MODE_CHARS = frozenset("wax+")  # any of write/append/exclusive-create/update


@dataclass(frozen=True)
class RiskyFinding:
    """A single risky import/call flagged by `audit_plugin_source`.

    `capability` is the `PLUGIN_CAPABILITIES` token this finding implies, or
    `None` when the finding is risky but has no clean 1:1 capability mapping
    (e.g. `eval`/`exec`/`ctypes` — flagged for visibility, never used for
    under-declaration cross-referencing since there is no token to compare
    against).
    """

    pattern: str
    capability: str | None
    lineno: int


@dataclass(frozen=True)
class PluginAuditResult:
    """Result of statically auditing one plugin's source text.

    `declared_capabilities` is a BEST-EFFORT static extraction of a literal
    `"capabilities": [...]` entry in the plugin's `register()` return value
    — empty (never `None`) when `register()` isn't found, doesn't return a
    literal dict, or declares no capabilities key. `under_declared` is the
    set of capability tokens implied by `findings` that are absent from
    `declared_capabilities` — the actionable "you probably need to declare
    this" signal `plugins audit` renders.
    """

    declared_capabilities: frozenset[str]
    findings: tuple[RiskyFinding, ...]
    under_declared: frozenset[str]


def _is_os_system_call(func: ast.expr) -> bool:
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "system"
        and isinstance(func.value, ast.Name)
        and func.value.id == "os"
    )


def _open_write_mode(node: ast.Call) -> str | None:
    """Return the literal `mode` string argument of an `open(...)` call, from
    either the 2nd positional argument or a `mode=` keyword — `None` if the
    mode can't be statically determined (e.g. a variable) or wasn't passed
    (defaults to read mode, not risky)."""
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        value = node.args[1].value
        return value if isinstance(value, str) else None
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            value = kw.value.value
            return value if isinstance(value, str) else None
    return None


def _scan_call(node: ast.Call) -> list[RiskyFinding]:
    findings: list[RiskyFinding] = []
    func = node.func

    if _is_os_system_call(func):
        findings.append(RiskyFinding("os.system(...)", "subprocess", node.lineno))
    elif isinstance(func, ast.Name) and func.id in ("eval", "exec"):
        findings.append(RiskyFinding(f"{func.id}(...)", None, node.lineno))
    elif isinstance(func, ast.Name) and func.id == "open":
        mode = _open_write_mode(node)
        if mode is not None and _WRITE_MODE_CHARS & set(mode):
            findings.append(RiskyFinding(f"open(..., mode={mode!r})", "filesystem", node.lineno))

    return findings


def _scan_risky_patterns(tree: ast.AST) -> list[RiskyFinding]:
    findings: list[RiskyFinding] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if top in _IMPORT_CAPABILITY_MAP:
                    findings.append(
                        RiskyFinding(
                            f"import {alias.name}", _IMPORT_CAPABILITY_MAP[top], node.lineno
                        )
                    )
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".", 1)[0]
            if top in _IMPORT_CAPABILITY_MAP:
                findings.append(
                    RiskyFinding(
                        f"from {node.module} import ...",
                        _IMPORT_CAPABILITY_MAP[top],
                        node.lineno,
                    )
                )
        elif isinstance(node, ast.Call):
            findings.extend(_scan_call(node))
    return findings


def _extract_declared_capabilities(tree: ast.Module) -> frozenset[str]:
    """Best-effort static extraction of `register()`'s declared
    `"capabilities"` literal, WITHOUT ever calling `register()`.

    Only recognizes a top-level `def register(): ... return {..., "capabilities":
    [...], ...}` shape with a literal list/tuple/set of string constants.
    Anything more dynamic (built from a variable, a function call, an
    f-string, a conditional) is invisible to this scanner and yields an
    empty result — documented as a known limitation of `plugins audit`, not
    a load-time gate (the real gate is `validate_capabilities`, which runs
    against the plugin's ACTUAL runtime `register()` return value).
    """
    declared: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "register"):
            continue
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict)):
                continue
            for key, value in zip(sub.value.keys, sub.value.values, strict=False):
                if not (isinstance(key, ast.Constant) and key.value == "capabilities"):
                    continue
                if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
                    for elt in value.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            declared.add(elt.value)
    return frozenset(declared)


def audit_plugin_source(source: str) -> PluginAuditResult:
    """Statically `ast`-parse a plugin's source TEXT and return a
    `PluginAuditResult` — the sole entry point for `hivepilot plugins audit`.

    NEVER imports, `exec()`s, or calls `register()` on the plugin — pure
    `ast.parse()` of `source`. Raises `SyntaxError` if `source` doesn't parse
    as valid Python (callers should catch this and report it as a per-plugin
    finding rather than aborting the whole audit).
    """
    tree = ast.parse(source)
    findings = tuple(_scan_risky_patterns(tree))
    declared = _extract_declared_capabilities(tree)
    used_capabilities = frozenset(f.capability for f in findings if f.capability is not None)
    under_declared = used_capabilities - declared
    return PluginAuditResult(
        declared_capabilities=declared, findings=findings, under_declared=under_declared
    )
