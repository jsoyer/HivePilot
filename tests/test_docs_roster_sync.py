"""Guardrail: docs/v4/AGENTS.md must stay in sync with roles.py.

Catches the common drift where an agent is renamed or a model swapped in roles.py
but the canonical roster table in AGENTS.md is forgotten (the bug we just fixed).
"""

from __future__ import annotations

from pathlib import Path

from hivepilot.roles import list_roles

_AGENTS_DOC = Path(__file__).resolve().parents[1] / "docs" / "v4" / "AGENTS.md"


def _short(model: str) -> str:
    # "opencode-go/kimi-k2.7-code" -> "kimi-k2.7-code" ; "claude:claude-sonnet-4-6" -> "claude-sonnet-4-6"
    return model.split("/")[-1].split(":")[-1]


def test_agents_doc_lists_every_role() -> None:
    doc = _AGENTS_DOC.read_text(encoding="utf-8")
    for role in list_roles():
        assert role.display_name and role.display_name in doc, (
            f"Agent '{role.display_name}' ({role.name}) missing from AGENTS.md"
        )
        assert role.runner and role.runner in doc, (
            f"Runner '{role.runner}' for {role.name} missing from AGENTS.md"
        )
        models = role.models or ([role.model] if role.model else [])
        for model in models:
            short = _short(model)
            assert short in doc, f"Model '{model}' ({short}) for {role.name} missing from AGENTS.md"


def test_agents_doc_mentions_henri_auditor() -> None:
    # Henri is a meta-agent (not in ROLES) but documented in the roster.
    doc = _AGENTS_DOC.read_text(encoding="utf-8")
    assert "Henri" in doc
    assert "auditor" in doc.lower()
