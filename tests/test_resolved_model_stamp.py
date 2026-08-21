"""`steps.model` must carry the model that ANSWERED, never the word asked for.

Measured on the box before this existed: 742 steps, 422 with a model, in two
vocabularies —

    claude-sonnet-5  174     the resolved identifier
    claude-opus-5    150
    claude-haiku-4-5  22
    sonnet            42     the config alias
    opus              32
    haiku              2

...and 320 rows NULL. The split had a cause: the orchestrator GUESSED from
config (`runner_def.model`) while the runner separately RESOLVED (profile,
`settings.default_model`), and only `usage.model` — the CLI's own self-report —
ever carried the truth. When usage was absent, the alias was stamped.

An alias is not an answer. Six months on, "this run used sonnet" does not say
which sonnet, and once `model: latest` is allowed the column would record the
word "latest" — a trail that answers nothing about what actually ran.

So the runner now STATES what it dispatched with, before dispatching, and the
orchestrator prefers: the CLI's self-report, else the runner's resolution, else
the configured string.
"""

from __future__ import annotations

import pytest

from hivepilot.runners.base import (
    UsageInfo,
    pop_last_resolved_model,
    set_last_resolved_model,
    set_last_usage,
)


@pytest.fixture(autouse=True)
def _clear_stash():
    """Both stashes are ContextVars. Leaving one set would let a step inherit
    the previous step's model — the exact bug class this table exists to make
    visible."""
    set_last_resolved_model(None)
    set_last_usage(None)
    yield
    set_last_resolved_model(None)
    set_last_usage(None)


class TestTheStashIsReadAndClear:
    def test_it_returns_what_was_set(self):
        set_last_resolved_model("claude-opus-5")

        assert pop_last_resolved_model() == "claude-opus-5"

    def test_a_second_pop_is_empty(self):
        """Read-and-clear, like the usage stash: a step that never dispatched
        must not inherit the previous step's model."""
        set_last_resolved_model("claude-opus-5")
        pop_last_resolved_model()

        assert pop_last_resolved_model() is None

    def test_it_is_separate_from_the_usage_stash(self):
        """Deliberately NOT folded into `UsageInfo`: callers test `if usage:`
        to mean "did this step report token usage", and a model-only UsageInfo
        would make that answer True for every step."""
        set_last_resolved_model("claude-opus-5")

        from hivepilot.runners.base import pop_last_usage

        assert pop_last_usage() is None
        assert pop_last_resolved_model() == "claude-opus-5"


class TestPrecedence:
    """The whole point. `_record_step_success` and its failure twin share one
    rule: self-report > resolution > config."""

    @staticmethod
    def _stamped(monkeypatch, *, usage, resolved, configured, failed=False):
        from hivepilot import orchestrator

        seen: dict = {}

        def _capture(run_id, step, status, detail=None, **kw):
            seen.update(kw)

        monkeypatch.setattr(orchestrator.state_service, "record_step", _capture)
        if failed:
            orchestrator._record_step_failure(
                1,
                "s",
                "boom",
                provider="claude",
                model=configured,
                usage=usage,
                resolved_model=resolved,
            )
        else:
            orchestrator._record_step_success(
                1, "s", "claude", configured, usage, resolved_model=resolved
            )
        return seen.get("model")

    def test_the_cli_self_report_wins(self, monkeypatch):
        """It is the only witness to what actually answered."""
        stamped = self._stamped(
            monkeypatch,
            usage=UsageInfo(model="claude-sonnet-5", input_tokens=10),
            resolved="sonnet-from-profile",
            configured="sonnet",
        )

        assert stamped == "claude-sonnet-5"

    def test_the_resolution_beats_the_config_alias(self, monkeypatch):
        """The 76 rows that used to read `sonnet`. No usage, but the runner
        still knows what it dispatched with."""
        stamped = self._stamped(
            monkeypatch, usage=None, resolved="claude-sonnet-5", configured="sonnet"
        )

        assert stamped == "claude-sonnet-5"

    def test_the_config_is_the_last_resort_not_the_first(self, monkeypatch):
        stamped = self._stamped(monkeypatch, usage=None, resolved=None, configured="sonnet")

        assert stamped == "sonnet"

    def test_a_failed_step_is_stamped_too(self, monkeypatch):
        """A step that failed still ran on a model. `set_last_resolved_model`
        is called BEFORE dispatch precisely so this path has an answer."""
        stamped = self._stamped(
            monkeypatch, usage=None, resolved="claude-opus-5", configured="opus", failed=True
        )

        assert stamped == "claude-opus-5"

    def test_a_failed_step_with_usage_still_prefers_the_self_report(self, monkeypatch):
        stamped = self._stamped(
            monkeypatch,
            usage=UsageInfo(model="claude-opus-5"),
            resolved="opus",
            configured="opus",
            failed=True,
        )

        assert stamped == "claude-opus-5"

    def test_nothing_anywhere_stays_none(self, monkeypatch):
        """No invented value. A model that is genuinely unknown must read as
        unknown — the 320 NULL rows were honest, unlike the 76 aliases."""
        stamped = self._stamped(monkeypatch, usage=None, resolved=None, configured=None)

        assert stamped is None


class TestTheRunnersActuallyStateIt:
    """The precedence is worthless if no runner ever populates the stash.
    These pin the CALL, because a missing one is invisible in output — the
    orchestrator would simply fall back to the config string and look fine."""

    def test_claude_states_its_model_before_dispatching(self):
        import inspect

        from hivepilot.runners import claude_runner

        src = inspect.getsource(claude_runner.ClaudeRunner._build_invocation)

        assert "set_last_resolved_model(model)" in src
        # BEFORE the args are built: a dispatch that raises must still have
        # stated what it was about to run on.
        assert src.index("set_last_resolved_model") < src.index('"--model"')

    def test_the_claude_api_path_states_it_too(self):
        import inspect

        from hivepilot.runners import claude_runner

        src = inspect.getsource(claude_runner)
        api = src[src.index("Claude API mode requires a model") - 600 :]

        assert "set_last_resolved_model" in api[:600]

    def test_prompt_cli_runners_state_it(self):
        """Covers codex, cursor, gemini, ollama, opencode, pi, qwen_code,
        kimi_cli, antigravity and vibe in one place — they all build their
        args through this method."""
        import inspect

        from hivepilot.runners import prompt_cli_runner

        src = inspect.getsource(prompt_cli_runner.PromptCliRunner._build_cli_args)

        assert "set_last_resolved_model(model)" in src


class TestTheOrchestratorPopsIt:
    def test_every_recorder_call_site_passes_it(self):
        """Three sites: review success, step success, step failure. A site
        that forgets silently reverts to stamping the config alias."""
        import inspect

        from hivepilot import orchestrator

        src = inspect.getsource(orchestrator)

        assert src.count("resolved_model=pop_last_resolved_model()") == 3
