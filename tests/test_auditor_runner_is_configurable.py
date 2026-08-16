"""Henri must not be hardwired to a runner the engine treats as optional.

Measured on run 635 (greenfield-forage, the delivery-proof run):

    Runner kind 'vibe' is provided by an optional, PATH-gated plugin that is
    not currently active [...] or the 'vibe' CLI binary is not on PATH.

Nothing in the pipeline asked for `vibe`. The task declares
`runner: claude`. The request came from the auditor, which hardcoded
`kind="vibe"` — and `vibe` stopped being a builtin when it moved to a gated
plugin (#520). So every pipeline cycle on a deployment without the Mistral
CLI ends on an auditor error that names a runner the operator never chose.

Henri's independence (an outside model auditing Claude's work) is a real
property and worth keeping — so the runner becomes a SETTING rather than a
constant, defaulting to a builtin that is always dispatchable. A deployment
that wants the outsider back sets `HIVEPILOT_AUDITOR_RUNNER=openrouter`
(or re-enables the vibe plugin) instead of editing the engine.
"""

from __future__ import annotations

import inspect

import pytest

from hivepilot.config import settings
from hivepilot.services import auditor_service


class TestTheAuditorRunnerIsNotHardcoded:
    def test_the_module_names_no_runner_kind_literally(self):
        """The defect in one assertion: `kind="vibe"` in the source."""
        source = inspect.getsource(auditor_service._run_henri)

        assert '"vibe"' not in source, "the auditor still hardcodes a runner kind"
        assert "'vibe'" not in source

    def test_the_default_runner_is_dispatchable_without_a_plugin(self):
        """A default that needs an optional plugin is a default that fails on
        a stock install — which is exactly what happened."""
        from hivepilot.services import agent_checks

        assert settings.auditor_runner in agent_checks.AGENT_RUNNER_KINDS
        assert settings.auditor_runner not in {"vibe", "codex", "cursor"}

    def test_the_step_and_the_definition_agree(self, monkeypatch, tmp_path):
        """`TaskStep.runner` and `RunnerDefinition.kind` are two names for the
        same choice; a fix that changes one and not the other is the kind of
        half-applied change that hides for a release."""
        from hivepilot.models import ProjectConfig

        monkeypatch.setattr(settings, "auditor_runner", "openrouter", raising=False)
        monkeypatch.setattr(
            auditor_service, "_auditor_prompt_path", lambda: tmp_path / "auditor.md"
        )

        seen: dict = {}

        class Registry:
            def capture_definition(self, rdef, payload):
                seen["kind"] = rdef.kind
                seen["runner"] = payload.step.runner
                return "  note  "

        note = auditor_service._run_henri(
            ProjectConfig(path=tmp_path), "contexte", Registry(), label="audit-1"
        )

        assert note == "note"
        assert seen["kind"] == "openrouter"
        assert seen["runner"] == "openrouter"


class TestAGatedPluginIsNotAdvertisedAsABuiltin:
    """`vibe` became a gated plugin (#520) but `doctor`'s built-in tuple was
    not updated with it, so the table rendered it through the built-in loop --
    reporting a plugin's availability against the wrong mechanism.

    Note what is deliberately NOT changed here: `MANDATORY_AGENTS` still
    lists codex and vibe. Its only consumer says "HivePilot needs at least
    ONE of", so an install carrying only codex genuinely has an agent, and
    shrinking that tuple would tell such an operator they have none.
    """

    def test_the_cli_builtin_tuple_no_longer_claims_vibe(self):
        from hivepilot import cli

        assert '_builtin_agent_kinds = ("claude", "openrouter")' in inspect.getsource(cli)

    def test_mandatory_agents_still_accepts_any_one_agent(self):
        from hivepilot.services import agent_checks

        assert agent_checks.MANDATORY_AGENTS[0] == "claude"
        assert "vibe" in agent_checks.MANDATORY_AGENTS


@pytest.mark.parametrize("kind", ["vibe", "codex", "cursor"])
def test_gated_kinds_remain_valid_agent_kinds(kind):
    """Un-advertising them as builtins must NOT un-declare them: a pipeline
    running only `cursor` once tripped the fail-closed no-agent guard for
    exactly this reason."""
    from hivepilot.services import agent_checks

    assert kind in agent_checks.AGENT_RUNNER_KINDS
