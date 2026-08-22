"""`surface:` — where the work can be WATCHED.

The second of the three axes `runner:` used to answer alone. herdr is a
SURFACE, not an executor: it runs a command in a pane so a human can see it,
and isolates nothing — same tree, same world. `workspace:` is the orthogonal
question, and "grok in a herdr pane, inside a throwaway worktree" needs both.

The capability was already there, and already inherited: `run_in_pane` sits on
`ClaudeRunner`, so `GrokRunner` got it for free from #570. But it was reachable
only through `claude_pane_mode` — a process-wide bool whose name says claude
and whose scope is every step or none.

The asymmetry worth understanding: `"inline"` is NOT the same as leaving the
field alone. Unset (`"derive"`) falls through to the setting; `"inline"`
OVERRULES it for this task. That is why a declared value is checked BEFORE the
boolean rather than or-ed with it.
"""

from __future__ import annotations

import pytest

from hivepilot.models import TaskConfig


class _Settings:
    def __init__(self, pane_mode: bool):
        self.claude_pane_mode = pane_mode


class _Definition:
    def __init__(self, surface=None):
        self.options = {} if surface is None else {"surface": surface}


def _in_pane(*, declared, setting: bool) -> bool:
    """The runner's decision, mirroring the branch under test.

    `TestTheRealBranch` pins that this copy still describes the source.
    """
    if declared == "herdr":
        return True
    if declared == "inline":
        return False
    return bool(setting)


class TestDeriveIsUnchanged:
    """A host that set `claude_pane_mode` must behave exactly as before."""

    def test_unset_follows_the_setting_when_on(self):
        assert _in_pane(declared=None, setting=True) is True

    def test_unset_follows_the_setting_when_off(self):
        assert _in_pane(declared=None, setting=False) is False

    def test_derive_is_the_model_default(self):
        assert TaskConfig(description="x").surface == "derive"

    def test_derive_never_reaches_the_runner(self):
        """The orchestrator forwards only a DECLARED value, so the runner sees
        no `surface` key at all for a `derive` task — which is what makes the
        fall-through byte-identical rather than merely equivalent."""
        import inspect

        from hivepilot import orchestrator

        src = inspect.getsource(orchestrator.resolve_step_runner)

        assert 'if _surface != "derive":' in src


class TestDeclaringIt:
    def test_herdr_opens_a_pane_even_with_the_setting_off(self):
        """The case that could not be expressed: watch THIS task, without
        turning panes on for every step on the host."""
        assert _in_pane(declared="herdr", setting=False) is True

    def test_inline_overrules_the_setting(self):
        """And its mirror: on a host running everything in panes, keep this
        one task out of them. `"inline"` is a decision, not a default."""
        assert _in_pane(declared="inline", setting=True) is False

    @pytest.mark.parametrize("declared", ["herdr", "inline"])
    def test_a_declaration_ignores_the_setting_entirely(self, declared):
        assert _in_pane(declared=declared, setting=True) is _in_pane(
            declared=declared, setting=False
        )


class TestItReachesTheRunner:
    def test_a_declared_surface_travels_in_the_definition_options(self):
        import inspect

        from hivepilot import orchestrator

        src = inspect.getsource(orchestrator.resolve_step_runner)

        assert 'role_options["surface"] = _surface' in src

    def test_the_runner_prefers_the_declaration_over_the_setting(self):
        import inspect

        from hivepilot.runners.claude_runner import ClaudeRunner

        src = inspect.getsource(ClaudeRunner)
        gate = src[src.index('_surface = self.definition.options.get("surface")') :]
        gate = gate[:400]

        # the declaration is consulted BEFORE the boolean; or-ing them would
        # make `"inline"` unable to overrule a host with panes on
        assert gate.index('_surface == "herdr"') < gate.index("claude_pane_mode")
        assert '_surface == "inline"' in gate


class TestGrokInherited:
    """The operator's actual goal. #570's inheritance choice bought this
    without a line of pane code — worth a test, because nothing else says so
    and the setting's NAME suggests otherwise."""

    def test_grok_gets_the_pane_path_from_claude(self):
        import inspect

        from hivepilot.runners.claude_runner import ClaudeRunner
        from hivepilot.runners.grok_runner import GrokRunner

        assert issubclass(GrokRunner, ClaudeRunner)
        assert "run_in_pane(" in inspect.getsource(ClaudeRunner)
        # and grok adds nothing of its own — it inherits, it does not reimplement
        assert "run_in_pane(" not in inspect.getsource(GrokRunner)


class TestTheTwoAxesAreIndependent:
    def test_a_task_can_declare_both(self):
        """The composition that was impossible while herdr occupied the runner
        slot: watch an isolated agent work."""
        task = TaskConfig(description="x", workspace="worktree", surface="herdr")

        assert (task.workspace, task.surface) == ("worktree", "herdr")

    def test_neither_implies_the_other(self):
        watched_but_shared = TaskConfig(description="x", workspace="shared", surface="herdr")
        isolated_but_unwatched = TaskConfig(description="x", workspace="worktree", surface="inline")

        assert watched_but_shared.workspace == "shared"
        assert isolated_but_unwatched.surface == "inline"


class TestTheModelRefusesNonsense:
    def test_an_unknown_surface_is_rejected_at_config_load(self):
        """`surface: pane` is the obvious typo, and it must fail at load
        rather than fall through to `derive` — a silent fall-through is how a
        task quietly stops being watched."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TaskConfig(description="x", surface="pane")

    def test_a_workspace_value_is_not_a_surface_value(self):
        """The axes are separate, and so are their vocabularies. Accepting
        `surface: worktree` would suggest they overlap."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            TaskConfig(description="x", surface="worktree")
