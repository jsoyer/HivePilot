"""
Role abstraction for HivePilot V4.

A Role is a declarative binding of:
  - a prompt file (the agent's mission, I/O contract, and constraints)
  - a Claude model profile (architecture / coding / automation)
  - an I/O contract (inputs and outputs)
  - pipeline metadata (order, whether the role can block the pipeline)

Roles are NOT stateful classes and are NOT executed here.
Execution is handled by the existing pipeline/runner machinery (another sprint).

Model profile assignments (all Claude for Phase 1):
  - architecture (opus):  CEO, CTO, CISO   — strategy / security decisions
  - coding (sonnet):      Developer, Reviewer, QA  — implementation / review
  - automation (haiku):   Chief of Staff, Documentation  — coordination / docs
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
    # Reasoning-effort knob — the ROLE tier of the unified
    # ``policy > stage > role > runner-default`` precedence (see
    # ``resolve_stage_dispatch`` and ``hivepilot.models.EffortLevel``). A
    # stage- or policy-level effort outranks this; ``None`` (default) means no
    # effort declared for this role, so dispatch stays byte-identical to a
    # pre-effort config. Validated as a closed ``EffortLevel`` literal — the
    # Claude runner maps the resolved level to ``MAX_THINKING_TOKENS`` and the
    # Codex runner to ``-c model_reasoning_effort=<level>``.
    effort: EffortLevel | None = None


# NOTE (Sprint 2, runner-defaults-plugins-mode PRD): `runner="opencode"` /
# `runner="gemini"` below are plain strings, resolved lazily at dispatch
# time via `resolve_runner()` -> `RunnerRegistry`/`resolve_runner_class`
# (hivepilot/registry.py) — there is no hard "this kind must be a built-in"
# assumption anywhere in this module. `opencode`/`gemini` moved from
# `hivepilot.registry._BUILTIN_RUNNERS` into default-on, PATH-gated plugins
# (plugins/opencode.py / plugins/gemini.py) this sprint; these role bindings
# keep resolving identically to before as long as the plugin is enabled
# (default True) and the CLI binary is on PATH — the same conditions that
# already had to hold for these roles to actually run. If either is false,
# `resolve_runner_class` now raises an actionable error naming the exact
# enable flag + required binary (see `RunnerPluginUnavailableError`).
_DEFAULT_ROLES: dict[str, Role] = {
    "ceo": Role(
        name="ceo",
        display_name="Aliénor",
        title="CEO",
        prompt_file=_PROMPTS_DIR / "ceo.md",
        model_profile="architecture",
        inputs=["roadmap", "metrics", "customer_feedback"],
        outputs=["objectives", "priorities", "constraints"],
        can_block=False,
        order=1,
        runner="opencode",
        models=["opencode-go/qwen3.7-max", "opencode-go/kimi-k2.6"],
        command_task="ceo-intake",
    ),
    "chief_of_staff": Role(
        name="chief_of_staff",
        display_name="Jules",
        title="Chief of Staff",
        prompt_file=_PROMPTS_DIR / "chief_of_staff.md",
        model_profile="automation",
        inputs=["objectives", "constraints", "status_report"],
        outputs=["execution_plan", "blocker_report", "cycle_report"],
        can_block=False,
        order=2,
        runner="cursor",
        command_task="cos-synthesis",
    ),
    "cto": Role(
        name="cto",
        display_name="Blaise",
        title="CTO",
        prompt_file=_PROMPTS_DIR / "cto.md",
        model_profile="architecture",
        inputs=["execution_plan", "architecture_docs", "tech_debt_log"],
        outputs=["technical_spec", "adr", "rejection_notice"],
        can_block=True,
        order=3,
        runner="opencode",
        # Single opencode model (claude brain removed to spare the claude quota the
        # developer stage needs). One model → runs single, no dual-model debate.
        models=["opencode-go/kimi-k2.7-code"],
        command_task="cto-review",
    ),
    "developer": Role(
        name="developer",
        display_name="Gustave",
        title="Developer",
        prompt_file=_PROMPTS_DIR / "developer.md",
        model_profile="coding",
        inputs=["technical_spec", "architecture_docs", "codebase_context"],
        outputs=["implementation", "test_suite", "implementation_notes"],
        can_block=False,
        order=4,
        runner="claude",
        # Full headless autonomy: Gustave writes code and runs the test suite
        # (TDD) without confirmation prompts. The human plan checkpoint gates the
        # pipeline before this stage, and execution is scoped to the component repo.
        permission_mode="bypassPermissions",
        command_task="developer",
    ),
    "reviewer": Role(
        name="reviewer",
        display_name="Victor",
        title="Reviewer",
        prompt_file=_PROMPTS_DIR / "reviewer.md",
        model_profile="coding",
        inputs=["implementation", "technical_spec", "test_suite"],
        outputs=["review_report", "approval"],
        can_block=True,
        order=5,
        runner="codex",
        model="gpt-5.5",
        command_task="reviewer",
    ),
    "ciso": Role(
        name="ciso",
        display_name="Hugo",
        title="CISO",
        prompt_file=_PROMPTS_DIR / "ciso.md",
        model_profile="architecture",
        inputs=["implementation", "review_report", "security_policy"],
        outputs=["security_report", "clearance"],
        can_block=True,
        order=6,
        runner="opencode",
        # Single opencode model (claude brain removed to spare the claude quota the
        # developer stage needs). One model → runs single, no dual-model debate.
        models=["opencode-go/glm-5.2"],
        command_task="ciso",
    ),
    "qa": Role(
        name="qa",
        display_name="Marie",
        title="QA",
        prompt_file=_PROMPTS_DIR / "qa.md",
        model_profile="coding",
        inputs=["implementation", "technical_spec", "test_suite"],
        outputs=["qa_test_suite", "test_report", "edge_case_log"],
        can_block=False,
        order=7,
        runner="cursor",
        command_task="qa",
    ),
    "documentation": Role(
        name="documentation",
        display_name="Théo",
        title="Documentation",
        prompt_file=_PROMPTS_DIR / "documentation.md",
        model_profile="automation",
        inputs=["implementation", "adr", "existing_docs"],
        outputs=["updated_docs", "updated_adrs", "changelog_entry"],
        can_block=False,
        order=8,
        runner="gemini",
        command_task="documentation",
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
            effort = override["effort"]
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
