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

from pathlib import Path

from pydantic import BaseModel

_PROMPTS_DIR = Path(__file__).parent.parent / "prompts" / "agents"


class Role(BaseModel):
    """Declarative definition of a HivePilot agent role."""

    name: str
    title: str
    prompt_file: Path
    model_profile: str
    inputs: list[str]
    outputs: list[str]
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


ROLES: dict[str, Role] = {
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
        models=["opencode-go/kimi-k2.7-code", "claude:claude-sonnet-4-6"],
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
        models=["opencode-go/glm-5.2", "claude:claude-haiku-4-5"],
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
    ),
}


def resolve_runner(role_name: str, policy: object | None = None) -> tuple[str, str | None]:
    """Resolve the effective (runner_kind, model) for *role_name*.

    Defaults come from the role binding (ROLES); a per-project policy may override
    runner/model (``role_overrides``) and constrain runners (``allowed_runners``).
    Raises if the role has no runner or the resolved runner is not allowed.
    """
    role = ROLES[role_name]
    runner = role.runner
    model = role.model or (role.models[0] if role.models else None)
    if policy is not None:
        override = (getattr(policy, "role_overrides", {}) or {}).get(role_name) or {}
        runner = override.get("runner", runner)
        model = override.get("model", model)
        allowed = getattr(policy, "allowed_runners", None)
        if allowed and runner not in allowed:
            raise RuntimeError(
                f"Role '{role_name}' resolves to runner '{runner}', not in allowed_runners {allowed}."
            )
    if not runner:
        raise RuntimeError(f"Role '{role_name}' has no runner binding.")
    return runner, model


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
