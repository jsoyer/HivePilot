"""
Role abstraction for HivePilot V4.

A Role is a declarative binding of:
  - a prompt file (the agent's mission, I/O contract, and constraints)
  - a Claude model profile (architecture / coding / automation)
  - an I/O contract (inputs and outputs)
  - pipeline metadata (order, whether the role can block the pipeline)

Roles are NOT stateful classes and are NOT executed here.
Execution is handled by the existing pipeline/runner machinery (another sprint).

Code-owned defaults (roles-model-effort-config-owned PRD, Sprint 2): the
in-code ``_DEFAULT_ROLES`` fallback is intentionally reduced to a single
generic ``developer -> claude`` binding — no hard-coded model, no optional
runner (opencode/gemini) dependency. The full multi-agent "company" roster
(CEO, CTO, CISO, Chief of Staff, Reviewer, QA, Documentation) that used to
live here is now config-owned: it ships as a restorable template at
``examples/roles.yaml`` (NOT auto-loaded) and, in this repository's own
dogfooded deployment, as the real ``roles.yaml`` at the repo root (unchanged
by this sprint — the full-replace loader means an existing customized
``roles.yaml`` keeps behaving exactly as before). A deployment with no
``roles.yaml`` at all now gets just the generic developer role instead of
silently depending on optional runner plugins (opencode/gemini) it may not
have configured.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from pydantic import BaseModel

from hivepilot.models import EffortLevel

if TYPE_CHECKING:
    from hivepilot.config import Settings

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts" / "agents"

log = logging.getLogger(__name__)


class Role(BaseModel):
    """Declarative definition of a HivePilot agent role."""

    name: str
    title: str
    prompt_file: Path
    model_profile: str
    inputs: list[str]
    outputs: list[str]
    # Keys routed into a stage's context when an upstream stage produced them,
    # but NEVER flagged as a "dangling input" by validate_config when no
    # upstream stage produces them (unlike `inputs`). Use case: a role shared
    # across pipelines that consumes a key only some pipelines' stages produce
    # (e.g. `design_spec` from a UI-only designer stage).
    optional_inputs: list[str] = []
    can_block: bool
    order: int
    # Sprint 2.1: runner + model binding (additive, defaulted — no existing tests broken)
    runner: str | None = None
    model: str | None = None
    models: list[str] | None = None
    display_name: str | None = None  # human-facing agent name (FR theme)
    host: str | None = None  # SSH host/alias to run this agent on (None = local)
    # Headless permission mode for claude-backed roles (the developer needs to
    # write code AND run tests autonomously). Without it, `claude --print` blocks
    # on an interactive permission prompt it cannot show and the run hangs.
    permission_mode: str | None = None
    # Task name that direct-agent commands (/ask <agent>, /dev, etc.) run for
    # this role. None means the role has no direct-command task (e.g. auditor).
    command_task: str | None = None


# Sprint 2 (roles-model-effort-config-owned PRD): reduced to a single
# generic role. The previous 8-role "company" roster (ceo/chief_of_staff/
# cto/reviewer/ciso/qa/documentation, plus this developer entry) lived here
# as hard-coded defaults; it now ships as a restorable, NOT-auto-loaded
# template at `examples/roles.yaml` (see that file for the exact previous
# values, including the opencode/gemini/codex/cursor runner bindings and
# dual-model debate config). `developer` is the only role a deployment gets
# "for free" with zero roles.yaml configuration — no hard-coded model (the
# runner picks its own default), no dependency on an optional runner plugin.
_DEFAULT_ROLES: dict[str, Role] = {
    "developer": Role(
        name="developer",
        display_name="Gustave",
        title="Developer",
        prompt_file=_PROMPTS_DIR / "developer.md",
        model_profile="coding",
        inputs=["technical_spec", "architecture_docs", "codebase_context"],
        outputs=["implementation", "test_suite", "implementation_notes"],
        can_block=False,
        order=1,
        runner="claude",
        # Full headless autonomy: Gustave writes code and runs the test suite
        # (TDD) without confirmation prompts. The human plan checkpoint gates the
        # pipeline before this stage, and execution is scoped to the component repo.
        permission_mode="bypassPermissions",
        command_task="developer",
    ),
}


def _resolve_prompt_path(prompt_filename: str, settings_obj: Settings) -> Path:
    """Resolve a role's prompt file through the config chain, falling back to
    the packaged prompts/agents/ copy.

    Resolution order:
      1. $XDG_CONFIG_HOME/hivepilot/prompts/agents/<prompt_filename>
      2. config_repo/prompts/agents/<prompt_filename>
      3. base_dir/prompts/agents/<prompt_filename>  (cwd fallback)
      4. _PROMPTS_DIR / <prompt_filename>  (packaged copy — FINAL fallback)

    ``settings_obj.resolve_config_path`` already implements tiers 1-3 (each
    ``.exists()``-checked, tier 3 returned unconditionally as the chain's own
    last resort). Here we additionally check ``.exists()`` on that result so a
    non-existent tier-3 guess doesn't shadow the packaged copy that ships with
    the app. Never raises — a missing file everywhere still yields a Path,
    letting callers' existing ``.exists()`` / ``""`` guards handle it safely.
    """
    candidate = settings_obj.resolve_config_path(Path("prompts") / "agents" / prompt_filename)
    if candidate.exists():
        return candidate
    return _PROMPTS_DIR / prompt_filename


def load_roles() -> dict[str, Role]:
    """Load roles from the configured roles_file (roles.yaml).

    Resolution order (via settings.resolve_config_path):
      1. $XDG_CONFIG_HOME/hivepilot/roles.yaml
      2. config_repo/roles.yaml
      3. base_dir/roles.yaml  (cwd fallback — repo root in dev)

    Each YAML entry's ``prompt_file`` is treated as a filename relative to
    ``prompts/agents/``. The loader resolves it through the same config chain
    (see ``_resolve_prompt_path``) so a prompt override placed in the config
    repo is picked up, falling back to ``_PROMPTS_DIR`` (identical to
    _DEFAULT_ROLES) when no override exists.

    On FileNotFoundError or any parse / validation error, logs a warning and
    returns _DEFAULT_ROLES so the application is never left without roles.
    """
    from hivepilot.config import settings  # local import to avoid circular at module load

    roles_path = settings.resolve_config_path(settings.roles_file)
    try:
        raw = yaml.safe_load(roles_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        log.warning("roles.yaml not found at %s — using built-in defaults", roles_path)
        return _DEFAULT_ROLES
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to read roles.yaml (%s) — using built-in defaults: %s", roles_path, exc)
        return _DEFAULT_ROLES

    try:
        entries: list[dict] = raw["roles"]
        result: dict[str, Role] = {}
        for entry in entries:
            entry = dict(entry)  # shallow copy so we don't mutate the parsed data
            prompt_filename = entry.pop("prompt_file")
            entry["prompt_file"] = _resolve_prompt_path(prompt_filename, settings)
            role = Role(**entry)
            result[role.name] = role
        return result
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to parse roles.yaml — using built-in defaults: %s", exc)
        return _DEFAULT_ROLES


def refresh_roles() -> None:
    """Re-load roles from disk and update the module-level ROLES dict in-place."""
    global ROLES  # noqa: PLW0603
    ROLES = load_roles()


# Module-level ROLES dict — sourced from roles.yaml at import time.
# Falls back to _DEFAULT_ROLES if the file is missing or invalid.
ROLES: dict[str, Role] = load_roles()


def resolve_runner(role_name: str, policy: object | None = None) -> tuple[str, str | None]:
    """Resolve the effective (runner_kind, model) for *role_name*.

    Defaults come from the role binding (ROLES); a per-project policy may override
    runner/model (``role_overrides``) and constrain runners (``allowed_runners``).
    Raises if the role has no runner or the resolved runner is not allowed.

    Unchanged implementation (roles-model-effort-config-owned PRD, Sprint 1) —
    kept standalone, rather than reimplemented on top of
    ``resolve_stage_dispatch``, so every existing caller (and every existing
    test that patches ``hivepilot.roles.resolve_runner`` directly) keeps
    working byte-identically. ``resolve_stage_dispatch`` below delegates BACK
    to this function whenever it has no stage override to apply.
    """
    role = ROLES[role_name]
    runner = role.runner
    model = role.model or (role.models[0] if role.models else None)
    if policy is not None:
        override = (getattr(policy, "role_overrides", {}) or {}).get(role_name) or {}
        runner = override.get("runner", runner)
        model = override.get("model", model)
        allowed = getattr(policy, "allowed_runners", None)
        # Fail-closed: an explicit empty list ([]) means "deny every runner",
        # NOT "no constraint". Only `None` (absent) means unconstrained. Using a
        # plain truthiness check here would treat [] as falsy and skip the gate
        # entirely (fail-OPEN) — the same sentinel/falsy class of bug as an
        # unknown-role `-1` inverting a `<` comparison.
        if allowed is not None and runner not in allowed:
            raise RuntimeError(
                f"Role '{role_name}' resolves to runner '{runner}', not in allowed_runners {allowed}."
            )
    if not runner:
        raise RuntimeError(f"Role '{role_name}' has no runner binding.")
    return runner, model


def resolve_stage_dispatch(
    role_name: str,
    policy: object | None = None,
    stage_model: str | None = None,
    stage_effort: EffortLevel | None = None,
) -> tuple[str, str | None, EffortLevel | None]:
    """Resolve the effective ``(runner_kind, model, effort)`` for *role_name*
    within an (optional) pipeline stage, applying the precedence:

        policy.role_overrides  >  stage  >  role  >  runner-default

    - ``runner``: the role's own binding, overridable by a policy
      ``role_overrides[role].runner`` entry. A stage never sets a runner in
      this sprint — only ``model``/``effort`` — so there is no stage layer
      for this element.
    - ``model``: the role's own binding (``role.model`` or the first entry of
      ``role.models``) as the base; *stage_model* (already resolved against
      the pipeline default by the caller — see
      ``hivepilot.models.resolve_stage_model``) overrides it; a policy
      ``role_overrides[role].model`` entry outranks BOTH, because policy is
      the security control that must never be short-circuited by a stage
      author.
    - ``effort``: ``None`` by default (roles have no effort concept of their
      own); *stage_effort* (already resolved against the pipeline default —
      see ``hivepilot.models.resolve_effort``) overrides it; a policy
      ``role_overrides[role].effort`` entry outranks both. ``None`` reaching
      a runner means "use that runner's own unset-default" (e.g.
      ``CodexRunner`` falls back to ``"medium"``).

    ``allowed_runners`` is still enforced fail-closed against the FINAL
    resolved runner, exactly as before this helper existed.

    Raises if the role has no runner or the resolved runner is not allowed.

    When *stage_model* and *stage_effort* are BOTH ``None`` (no stage
    override at all — the exact shape a plain ``resolve_runner`` call has),
    this delegates entirely to ``resolve_runner`` for the ``(runner, model)``
    pair — the SAME code path every existing caller/test already exercises —
    and only independently resolves ``effort`` from a policy override (a
    stage-less call has no other source for it). This keeps the "stage sets
    nothing" case byte-identical, including for tests that mock
    ``resolve_runner`` directly rather than the ROLES registry.
    """
    if stage_model is None and stage_effort is None:
        runner, model = resolve_runner(role_name, policy)
        effort: EffortLevel | None = None
        if policy is not None:
            override = (getattr(policy, "role_overrides", {}) or {}).get(role_name) or {}
            effort = override.get("effort")
        return runner, model, effort

    role = ROLES[role_name]
    runner = role.runner
    model = role.model or (role.models[0] if role.models else None)
    effort = None

    # Stage layer.
    if stage_model is not None:
        model = stage_model
    if stage_effort is not None:
        effort = stage_effort

    # Policy layer — outranks the stage layer above for every field it sets.
    if policy is not None:
        override = (getattr(policy, "role_overrides", {}) or {}).get(role_name) or {}
        runner = override.get("runner", runner)
        model = override.get("model", model)
        effort = override.get("effort", effort)
        allowed = getattr(policy, "allowed_runners", None)
        # Fail-closed: an explicit empty list ([]) means "deny every runner",
        # NOT "no constraint". Only `None` (absent) means unconstrained. Mirrors
        # the identical gate in `resolve_runner` — the parity test
        # `test_allowed_runners_gate_parity_*` asserts the two paths agree.
        if allowed is not None and runner not in allowed:
            raise RuntimeError(
                f"Role '{role_name}' resolves to runner '{runner}', not in allowed_runners {allowed}."
            )
    if not runner:
        raise RuntimeError(f"Role '{role_name}' has no runner binding.")
    return runner, model, effort


def resolve_host(role_name: str, policy: object | None = None) -> str | None:
    """Resolve the SSH host for *role_name* — role default, overridable per project
    via policy ``role_overrides[role].host``. ``None`` means run locally."""
    host = ROLES[role_name].host
    if policy is not None:
        override = (getattr(policy, "role_overrides", {}) or {}).get(role_name) or {}
        host = override.get("host", host)
    return host


def get_role(name: str) -> Role:
    """Return the Role for *name*; raises KeyError if not found."""
    return ROLES[name]


def list_roles() -> list[Role]:
    """Return all roles sorted ascending by their pipeline order."""
    return sorted(ROLES.values(), key=lambda r: r.order)
