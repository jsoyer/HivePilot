"""One way to talk to a live agent, two backends behind it.

The operator's ask -- see the agents work, and talk to them -- is not a
feature we lack. It is a shape: roles already carry a name, a model, a memory
and skills, and they already message each other. What is missing is a single
place that says "start / inject / read / wait", so herdr and Orca are two
drivers rather than two parallel implementations.

Both expose the same four verbs, verified against the installed binaries on
2026-08-18:

    herdr  agent prompt <t> <text> | agent read <t> --lines N
           agent wait <t> --until <s> --timeout <ms> | agent list
    orca   terminal send --terminal <h> --text <t> --enter
           terminal read --terminal <h> --limit N
           terminal wait --terminal <h> --for tui-idle --timeout-ms <ms>

The vocabulary comes from herdr, not Orca, and that is deliberate: herdr
distinguishes **blocked** from **idle**, and Orca's `tui-idle` does not. A
false idle would cut a step while the agent is still thinking -- open point 5
of the ORCA-1 PRD. Mapping the poorer vocabulary onto the richer one keeps the
distinction available where the backend can express it, and honest (`UNKNOWN`)
where it cannot.

What is tested here is argv construction, on purpose: it is the part that can
be wrong silently, it needs no binary, and a wrong flag is exactly the defect
that would look like "the agent did not answer".
"""

from __future__ import annotations

import pytest

from hivepilot.services.agent_surface import (
    AgentState,
    HerdrDriver,
    OrcaDriver,
    resolve_driver,
)


class TestHerdrArgv:
    def test_prompt(self):
        assert HerdrDriver().send_argv("reviewer", "run the tests") == [
            "herdr",
            "agent",
            "prompt",
            "reviewer",
            "run the tests",
        ]

    def test_read_is_bounded(self):
        """An unbounded read of a live pane is how a 26 000-character QA report
        ends up in a prompt."""
        argv = HerdrDriver().read_argv("reviewer", limit=200)

        assert argv[:4] == ["herdr", "agent", "read", "reviewer"]
        assert "--lines" in argv and "200" in argv

    def test_wait_carries_a_timeout(self):
        """`herdr agent wait` without --timeout waits INDEFINITELY, per its own
        help. A pipeline step that hangs forever is worse than one that fails."""
        argv = HerdrDriver().wait_argv("reviewer", until=AgentState.IDLE, timeout_ms=60_000)

        assert "--timeout" in argv
        assert "60000" in argv
        assert "--until" in argv and "idle" in argv


class TestOrcaArgv:
    def test_send_presses_enter(self):
        """Text without --enter sits in the prompt line unsent; the agent looks
        idle and the operator waits for an answer that was never asked for."""
        argv = OrcaDriver().send_argv("term-7", "run the tests")

        assert argv[:3] == ["orca-ide", "terminal", "send"]
        assert "--enter" in argv
        assert "--text" in argv and "run the tests" in argv

    def test_never_invokes_bare_orca(self):
        """On Linux outside an Orca terminal, bare `orca` is the GNOME screen
        reader -- running it starts speech on the operator's machine."""
        for argv in (
            OrcaDriver().send_argv("t", "x"),
            OrcaDriver().read_argv("t", limit=10),
            OrcaDriver().wait_argv("t", until=AgentState.IDLE, timeout_ms=1000),
        ):
            assert argv[0] == "orca-ide"

    def test_wait_uses_tui_idle_and_ms(self):
        argv = OrcaDriver().wait_argv("term-7", until=AgentState.IDLE, timeout_ms=60_000)

        assert "--for" in argv and "tui-idle" in argv
        assert "--timeout-ms" in argv and "60000" in argv

    def test_read_is_bounded(self):
        argv = OrcaDriver().read_argv("term-7", limit=200)

        assert "--limit" in argv and "200" in argv


class TestTheStateVocabularyIsHerdrs:
    def test_blocked_is_expressible(self):
        assert AgentState.BLOCKED.value == "blocked"

    def test_orca_cannot_express_blocked_and_says_so(self):
        """Asking Orca to wait for BLOCKED must not silently degrade into
        waiting for idle -- that is the false-idle cut, wearing a different
        mask."""
        with pytest.raises(ValueError, match="blocked"):
            OrcaDriver().wait_argv("t", until=AgentState.BLOCKED, timeout_ms=1000)

    def test_herdr_can_wait_on_blocked(self):
        argv = HerdrDriver().wait_argv("t", until=AgentState.BLOCKED, timeout_ms=1000)

        assert "blocked" in argv


class TestChoosingADriver:
    def test_by_name(self):
        assert isinstance(resolve_driver("herdr"), HerdrDriver)
        assert isinstance(resolve_driver("orca"), OrcaDriver)

    def test_an_unknown_backend_is_refused_by_name(self):
        """Naming the backend beats 'unsupported': the operator set this in
        config and needs to know which value was rejected."""
        with pytest.raises(ValueError, match="tmux"):
            resolve_driver("tmux")

    def test_the_name_is_case_insensitive(self):
        assert isinstance(resolve_driver("HERDR"), HerdrDriver)


class TestNothingIsShellInterpolated:
    @pytest.mark.parametrize("hostile", ["; rm -rf /", "$(whoami)", "`id`", "a && b"])
    def test_text_travels_as_one_argv_element(self, hostile):
        """argv, never a shell string. A prompt is operator- or agent-authored
        text and will contain metacharacters."""
        argv = HerdrDriver().send_argv("reviewer", hostile)

        assert hostile in argv
        assert not any(a.startswith("sh ") or a == "-c" for a in argv)
