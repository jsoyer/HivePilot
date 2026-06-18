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


ROLES: dict[str, Role] = {
    "ceo": Role(
        name="ceo",
        title="CEO",
        prompt_file=_PROMPTS_DIR / "ceo.md",
        model_profile="architecture",
        inputs=["roadmap", "metrics", "customer_feedback"],
        outputs=["objectives", "priorities", "constraints"],
        can_block=False,
        order=1,
    ),
    "chief_of_staff": Role(
        name="chief_of_staff",
        title="Chief of Staff",
        prompt_file=_PROMPTS_DIR / "chief_of_staff.md",
        model_profile="automation",
        inputs=["objectives", "constraints", "status_report"],
        outputs=["execution_plan", "blocker_report", "cycle_report"],
        can_block=False,
        order=2,
    ),
    "cto": Role(
        name="cto",
        title="CTO",
        prompt_file=_PROMPTS_DIR / "cto.md",
        model_profile="architecture",
        inputs=["execution_plan", "architecture_docs", "tech_debt_log"],
        outputs=["technical_spec", "adr", "rejection_notice"],
        can_block=True,
        order=3,
    ),
    "developer": Role(
        name="developer",
        title="Developer",
        prompt_file=_PROMPTS_DIR / "developer.md",
        model_profile="coding",
        inputs=["technical_spec", "architecture_docs", "codebase_context"],
        outputs=["implementation", "test_suite", "implementation_notes"],
        can_block=False,
        order=4,
    ),
    "reviewer": Role(
        name="reviewer",
        title="Reviewer",
        prompt_file=_PROMPTS_DIR / "reviewer.md",
        model_profile="coding",
        inputs=["implementation", "technical_spec", "test_suite"],
        outputs=["review_report", "approval"],
        can_block=True,
        order=5,
    ),
    "ciso": Role(
        name="ciso",
        title="CISO",
        prompt_file=_PROMPTS_DIR / "ciso.md",
        model_profile="architecture",
        inputs=["implementation", "review_report", "security_policy"],
        outputs=["security_report", "clearance"],
        can_block=True,
        order=6,
    ),
    "qa": Role(
        name="qa",
        title="QA",
        prompt_file=_PROMPTS_DIR / "qa.md",
        model_profile="coding",
        inputs=["implementation", "technical_spec", "test_suite"],
        outputs=["qa_test_suite", "test_report", "edge_case_log"],
        can_block=False,
        order=7,
    ),
    "documentation": Role(
        name="documentation",
        title="Documentation",
        prompt_file=_PROMPTS_DIR / "documentation.md",
        model_profile="automation",
        inputs=["implementation", "adr", "existing_docs"],
        outputs=["updated_docs", "updated_adrs", "changelog_entry"],
        can_block=False,
        order=8,
    ),
}


def get_role(name: str) -> Role:
    """Return the Role for *name*; raises KeyError if not found."""
    return ROLES[name]


def list_roles() -> list[Role]:
    """Return all roles sorted ascending by their pipeline order."""
    return sorted(ROLES.values(), key=lambda r: r.order)
