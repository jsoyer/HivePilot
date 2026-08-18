"""`herdr wait agent-status` does not exist, and never did.

Measured against herdr 0.7.5 AND 0.8.0 on 2026-08-18. The runner calls::

    herdr wait agent-status <pane> --status idle --timeout <ms>

and herdr answers `unknown command: wait`. There is no top-level `wait` in
either version -- the plugin was written against mocks and never run against a
server.

The obvious replacement is not one. `herdr agent wait <target>` needs an AGENT
target, and a pane running a shell command is not an agent -- probed on the
box, it answers `agent_not_found` for a live pane id. Waiting on agent status
is the wrong question here anyway: we are not waiting for an agent to think,
we are waiting for a COMMAND to finish.

What 0.8.0 does offer is `pane wait-output --match <TEXT>`, which is exactly
the right shape once the command announces its own completion. The runner
already wraps the command (cd, env file), so the wrapper appends a sentinel.

Two properties the sentinel must have, and both are failure modes:

- it must be UNIQUE per step, or a second step in the same pane matches the
  first one's marker and reads output that is not its own;
- it must be echoed unconditionally, after success AND failure, or a failing
  command hangs until the timeout instead of failing -- and the operator gets
  "timed out" for something that finished in a second.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest


def _herdr():
    path = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "herdr.py"
    spec = importlib.util.spec_from_file_location("hivepilot_test_herdr_wait", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class TestTheSentinel:
    def test_it_is_unique_per_call(self):
        """Two steps in one pane must not match each other's marker."""
        h = _herdr()
        a = h.HerdrRunner._completion_sentinel()
        b = h.HerdrRunner._completion_sentinel()

        assert a != b

    def test_it_carries_a_recognisable_prefix(self):
        """So a human reading a pane can tell our marker from step output."""
        assert "HIVEPILOT" in _herdr().HerdrRunner._completion_sentinel()

    def test_it_has_no_shell_metacharacters(self):
        """It is interpolated into a shell command and passed to --match."""
        sentinel = _herdr().HerdrRunner._completion_sentinel()

        assert not any(c in sentinel for c in " ;&|$`'\"\\\n*?")


class TestTheWrappedCommand:
    def test_the_sentinel_is_echoed_after_the_command(self):
        h = _herdr()
        wrapped = h.HerdrRunner._wrap_with_sentinel("pytest -q", "HIVEPILOT_DONE_abc")

        assert "pytest -q" in wrapped
        assert wrapped.index("pytest -q") < wrapped.index("HIVEPILOT_DONE_abc")

    def test_it_is_echoed_even_when_the_command_fails(self):
        """A failing command must still announce completion. Otherwise it hangs
        to the timeout and reports 'timed out' for something that finished in a
        second -- a wrong diagnosis, not just a slow one."""
        wrapped = _herdr().HerdrRunner._wrap_with_sentinel("false", "HIVEPILOT_DONE_abc")

        assert ";" in wrapped or "||" in wrapped
        assert "&&" not in wrapped.split("HIVEPILOT_DONE_abc")[0][-4:]

    def test_the_exit_status_survives_the_sentinel(self):
        """The step's own status must not be replaced by the echo's."""
        wrapped = _herdr().HerdrRunner._wrap_with_sentinel("false", "HIVEPILOT_DONE_abc")

        assert "$?" in wrapped


class TestTheWaitCall:
    def test_it_uses_wait_output_not_the_command_that_does_not_exist(self):
        argv = _herdr().HerdrRunner._wait_argv("w1:p1", "HIVEPILOT_DONE_abc", 60_000)

        assert argv[:3] == ["herdr", "pane", "wait-output"]
        assert "agent-status" not in argv

    def test_it_matches_the_sentinel_literally(self):
        """`--match` is a literal substring; `--regex` would turn a marker into
        a pattern and match things it should not."""
        argv = _herdr().HerdrRunner._wait_argv("w1:p1", "HIVEPILOT_DONE_abc", 60_000)

        assert "--match" in argv
        assert "HIVEPILOT_DONE_abc" in argv
        assert "--regex" not in argv

    def test_the_pane_id_is_positional_and_last(self):
        argv = _herdr().HerdrRunner._wait_argv("w1:p1", "S", 1000)

        assert argv[-1] == "w1:p1"

    @pytest.mark.parametrize("timeout", [1000, 300_000])
    def test_a_timeout_is_always_passed(self, timeout):
        """Without one, herdr waits indefinitely -- a step that hangs forever
        is worse than one that fails."""
        argv = _herdr().HerdrRunner._wait_argv("w1:p1", "S", timeout)

        assert str(timeout) in argv
