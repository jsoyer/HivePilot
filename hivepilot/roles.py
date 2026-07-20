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
from typing import TYPE_CHECKING, cast

import yaml
from pydantic import BaseModel

from hivepilot.models import EffortLevel, validate_effort

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
    # Reasoning-effort knob — the ROLE tier of the unified
    # ``policy > stage > role > runner-default`` precedence (see
    # ``resolve_stage_dispatch`` and ``hivepilot.models.EffortLevel``). A
    # stage- or policy-level effort outranks this; ``None`` (default) means no
    # effort declared for this role, so dispatch stays byte-identical to a
    # pre-effort config. Validated as a closed ``EffortLevel`` literal — the
    # Claude runner maps the resolved level to ``MAX_THINKING_TOKENS`` and the
    # Codex runner to ``-c model_reasoning_effort=<level>``.
    effort: EffortLevel | None = None


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


def _load_roles_strict() -> dict[str, Role]:
    """Load roles from the configured roles_file (roles.yaml), RAISING on any
    failure (missing file, unparseable YAML, schema/validation error) instead
    of ever falling back to ``_DEFAULT_ROLES``.

    This is the strict core that both bootstrap loading (``load_roles``,
    which catches everything and falls back to defaults) and hot-reload
    (``refresh_roles``, which catches everything and keeps the PREVIOUS live
    ``ROLES`` — never downgrades to the generic single-role default) build
    on. Factored out so a reload of a broken roles.yaml on a running process
    can be distinguished from "first boot, nothing configured yet" — the two
    callers want genuinely different fallback behavior on failure.

    Resolution order (via settings.resolve_config_path):
      1. $XDG_CONFIG_HOME/hivepilot/roles.yaml
      2. config_repo/roles.yaml
      3. base_dir/roles.yaml  (cwd fallback — repo root in dev)

    Each YAML entry's ``prompt_file`` is treated as a filename relative to
    ``prompts/agents/``. The loader resolves it through the same config chain
    (see ``_resolve_prompt_path``) so a prompt override placed in the config
    repo is picked up, falling back to ``_PROMPTS_DIR`` (identical to
    _DEFAULT_ROLES) when no override exists.
    """
    from hivepilot.config import settings  # local import to avoid circular at module load

    roles_path = settings.resolve_config_path(settings.roles_file)
    raw = yaml.safe_load(roles_path.read_text(encoding="utf-8"))
    entries: list[dict] = raw["roles"]
    result: dict[str, Role] = {}
    for entry in entries:
        entry = dict(entry)  # shallow copy so we don't mutate the parsed data
        prompt_filename = entry.pop("prompt_file")
        entry["prompt_file"] = _resolve_prompt_path(prompt_filename, settings)
        role = Role(**entry)
        result[role.name] = role
    return result


def load_roles() -> dict[str, Role]:
    """Bootstrap loader — wraps ``_load_roles_strict()`` and, on ANY failure
    (missing file, parse error, validation error), logs a warning and returns
    ``_DEFAULT_ROLES`` so the application is never left without roles. Used
    at import time (module-level ``ROLES`` below) and by any caller that
    wants "give me something usable, no matter what" semantics.

    See ``_load_roles_strict`` for the resolution order / prompt_file
    handling. See ``refresh_roles`` for the hot-reload counterpart, which
    deliberately does NOT fall back to ``_DEFAULT_ROLES`` on failure.
    """
    try:
        return _load_roles_strict()
    except FileNotFoundError as exc:
        log.warning("roles.yaml not found (%s) — using built-in defaults", exc)
        return _DEFAULT_ROLES
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to load roles.yaml — using built-in defaults: %s", exc)
        return _DEFAULT_ROLES


def refresh_roles() -> bool:
    """Hot-reload roles from disk into the module-level ``ROLES`` dict,
    fail-closed TO THE PREVIOUS LIVE CONFIG (not to ``_DEFAULT_ROLES``).

    Snapshots nothing explicitly — the current ``ROLES`` global itself IS the
    snapshot; on success it is atomically rebound (a single GIL-atomic
    reference assignment) to the freshly loaded dict; on ANY exception from
    ``_load_roles_strict()`` (missing file, bad YAML, schema/validation
    error) the exception TYPE is logged and ``ROLES`` is left completely
    untouched, so a broken ``roles.yaml`` deployed to a running process can
    never silently downgrade a rich, already-loaded roster (e.g. the 8-role
    "company" roster) down to the generic single-``developer`` fallback that
    ``load_roles()`` uses at bootstrap. Returns ``True`` on a successful
    swap, ``False`` if the previous config was kept.

    Known residual: a reload that lands MID pipeline run could let a later
    stage of that same run see the new role bindings while an earlier stage
    already ran against the old ones (``get_role``/``ROLES`` are read live at
    each call site, not snapshotted per-run). Safe reload points (SIGHUP,
    the scheduler tick boundary, the explicit admin endpoint) make this
    unlikely in practice; eliminating it fully requires threading a per-run
    roles snapshot through ``run_task``/``run_pipeline`` — a deferred
    follow-up, not attempted here. See docs/DEPLOY-PRODUCTION.md §10.
    """
    global ROLES  # noqa: PLW0603
    try:
        new_roles = _load_roles_strict()
    except Exception as exc:  # noqa: BLE001 — a bad candidate must never touch live ROLES
        log.warning("roles.refresh_failed — keeping previous roles config: %s", type(exc).__name__)
        return False
    ROLES = new_roles
    log.info("roles.refreshed — %d role(s) loaded", len(new_roles))
    return True


# Module-level ROLES dict — sourced from roles.yaml at import time.
# Falls back to _DEFAULT_ROLES if the file is missing or invalid.
ROLES: dict[str, Role] = load_roles()


def resolve_runner(
    role_name: str, policy: object | None = None
) -> tuple[str, str | None, EffortLevel | None]:
    """Resolve the effective ``(runner_kind, model, effort)`` for *role_name*.

    Defaults come from the role binding (ROLES); a per-project policy may override
    runner/model (``role_overrides``) and constrain runners (``allowed_runners``).
    ``effort`` is the role's own ``Role.effort`` — the ROLE tier of the unified
    ``policy > stage > role > runner-default`` precedence (see
    ``resolve_stage_dispatch``, which layers stage/policy effort over this).
    Raises if the role has no runner or the resolved runner is not allowed.
    """
    role = ROLES[role_name]
    runner = role.runner
    model = role.model or (role.models[0] if role.models else None)
    effort: EffortLevel | None = role.effort
    if policy is not None:
        override = (getattr(policy, "role_overrides", {}) or {}).get(role_name) or {}
        runner = override.get("runner", runner)
        model = override.get("model", model)
        allowed = getattr(policy, "allowed_runners", None)
        # Fail-closed: an explicit empty list ([]) means "deny every runner",
        # NOT "no constraint". Only `None` (absent) means unconstrained. A plain
        # truthiness check would treat [] as falsy and skip the gate entirely
        # (fail-OPEN) — the same sentinel/falsy class of bug as an unknown-role
        # `-1` inverting a `<` comparison.
        if allowed is not None and runner not in allowed:
            raise RuntimeError(
                f"Role '{role_name}' resolves to runner '{runner}', not in allowed_runners {allowed}."
            )
    if not runner:
        raise RuntimeError(f"Role '{role_name}' has no runner binding.")
    return runner, model, effort


def resolve_stage_dispatch(
    role_name: str,
    policy: object | None = None,
    stage_model: str | None = None,
    stage_effort: EffortLevel | None = None,
) -> tuple[str, str | None, EffortLevel | None]:
    """Resolve the effective ``(runner_kind, model, effort)`` for *role_name*
    within an (optional) pipeline stage, applying the UNIFIED precedence:

        policy.role_overrides  >  stage  >  role  >  runner-default

    Both the stage- and role-level effort systems collapse into this single
    chain (they were reconciled from two independently-shipped mechanisms — the
    stage/pipeline model+effort knob and the per-role/step Claude effort knob):

    - ``runner``: the role's binding, overridable by policy
      ``role_overrides[role].runner`` (a stage never sets a runner here).
    - ``model``: role binding base; *stage_model* (already resolved against the
      pipeline default — see ``hivepilot.models.resolve_stage_model``) overrides
      it; policy ``role_overrides[role].model`` outranks BOTH.
    - ``effort``: the ROLE's own ``Role.effort`` is the base (via
      ``resolve_runner`` — role IS a real fallback tier, not "no concept");
      *stage_effort* (see ``hivepilot.models.resolve_effort``) overrides it;
      policy ``role_overrides[role].effort`` outranks both. ``None`` reaching a
      runner means "use that runner's own unset-default" (e.g. ``CodexRunner``
      falls back to ``"medium"``; the Claude runner injects no
      ``MAX_THINKING_TOKENS``).

    Policy is the top security control and must never be short-circuited by a
    stage author — hence it is re-applied LAST, on top of the stage layer, even
    though ``resolve_runner`` already baked it into ``model``.

    Implemented entirely on top of ``resolve_runner`` (which owns the base
    resolution AND the fail-closed ``allowed_runners`` gate against the FINAL
    runner), so this shares one code path with every direct caller/test — when
    *stage_model*/*stage_effort* are both ``None`` the result is byte-identical
    to ``resolve_runner`` (the policy re-apply is idempotent).
    """
    runner, model, effort = resolve_runner(role_name, policy)

    # Stage layer — overrides the role base (but never policy, re-applied below).
    if stage_model is not None:
        model = stage_model
    if stage_effort is not None:
        effort = stage_effort

    # Policy layer — top of precedence. `resolve_runner` already applied
    # policy.model; re-apply here so a stage_model cannot outrank policy.
    if policy is not None:
        override = (getattr(policy, "role_overrides", {}) or {}).get(role_name) or {}
        if "model" in override:
            model = override["model"]
        if "effort" in override:
            # `validate_effort` raises ValueError for anything outside
            # EFFORT_LEVELS (the exact `get_args(EffortLevel)` set), so the
            # cast only narrows a value already proven to be a legal
            # EffortLevel — no unchecked type-safety hole.
            effort = cast("EffortLevel", validate_effort(override["effort"]))
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
