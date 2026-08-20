"""Executing a step in a pane must be indistinguishable from executing it here.

`capture()` in the Claude runner consumes a `CompletedProcess`: `.returncode`,
`.stdout`, `.stderr`. Everything downstream -- the usage envelope, the cost, the
permission-denial reporting, the failure context, the hint matching -- is built
on those three fields and is well tested already. So the pane path returns the
SAME object rather than growing a second set of branches beside it. If the
substitution is faithful, none of that code knows the difference.

Which makes the interesting tests the ones about what a pane can silently lose:

- the exit status, which `herdr pane run` does not report (it reports whether
  HERDR worked);
- the environment, which the pane inherits from the herdr SERVER;
- the secrets file, which must not outlive the step;
- the pane itself, which must not accumulate one per step forever.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from hivepilot.runners.pane_exec import PaneExecutionError, run_in_pane


class FakeHerdr:
    """A herdr CLI that records its calls and writes what the shell would.

    Deliberately writes the capture files itself: the real command line is
    exercised by tests/test_pane_capture.py, and what matters here is that this
    layer reads back what a shell would have left behind.
    """

    def __init__(self, *, rc: str | None = "0", stdout: str = "{}", stderr: str = ""):
        self.calls: list[list[str]] = []
        self.rc = rc
        self.stdout = stdout
        self.stderr = stderr
        self.split_returncode = 0
        self.wait_returncode = 0

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        verb = argv[2] if len(argv) > 2 else ""
        if verb == "split":
            return subprocess.CompletedProcess(
                argv, self.split_returncode, '{"result": {"pane_id": "pane-7"}}', ""
            )
        if verb == "run":
            self._materialise(argv[-1])
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        if verb == "wait-output":
            return subprocess.CompletedProcess(argv, self.wait_returncode, "{}", "")
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    def _materialise(self, command: str) -> None:
        """Write the files the redirections in `command` name."""
        for token in command.replace(";", " ").split():
            if token.endswith(".stdout.json"):
                Path(token).write_text(self.stdout)
            elif token.endswith(".stderr.log"):
                Path(token).write_text(self.stderr)
            elif token.endswith(".rc") and self.rc is not None:
                Path(token).write_text(self.rc + "\n")


def _run(tmp_path, herdr, **kw):
    kw.setdefault("cwd", str(tmp_path))
    kw.setdefault("env", {})
    kw.setdefault("timeout", 30)
    return run_in_pane(["claude", "--print"], capture_dir=str(tmp_path), run_cli=herdr, **kw)


class TestItLooksLikeASubprocess:
    def test_the_envelope_comes_back_as_stdout(self, tmp_path):
        herdr = FakeHerdr(stdout='{"result": "done", "total_cost_usd": 0.4}')

        result = _run(tmp_path, herdr)

        assert result.stdout == '{"result": "done", "total_cost_usd": 0.4}'

    def test_stderr_comes_back_separately(self, tmp_path):
        """Interleaved into stdout it would make the envelope unparseable, and
        a step that worked would record null usage."""
        herdr = FakeHerdr(stdout="{}", stderr="warning: rate limited")

        result = _run(tmp_path, herdr)

        assert result.stderr == "warning: rate limited"
        assert "rate limited" not in result.stdout

    def test_a_zero_status_is_reported_as_success(self, tmp_path):
        assert _run(tmp_path, FakeHerdr(rc="0")).returncode == 0

    def test_a_failing_agent_reports_its_own_status(self, tmp_path):
        """Not herdr's. `herdr pane run` exits 0 for a command that died."""
        assert _run(tmp_path, FakeHerdr(rc="137")).returncode == 137


class TestAMissingStatusIsNotASuccess:
    """The single most dangerous outcome. If the status file is absent the
    shell never reached the end of the line -- the pane was killed, the server
    dropped it, the box rebooted. Defaulting that to 0 would report every lost
    step as a clean success with empty output."""

    def test_an_absent_status_file_is_a_failure(self, tmp_path):
        result = _run(tmp_path, FakeHerdr(rc=None))

        assert result.returncode != 0

    def test_an_unparseable_status_is_a_failure(self, tmp_path):
        result = _run(tmp_path, FakeHerdr(rc="not-a-number"))

        assert result.returncode != 0

    def test_the_envelope_survives_a_missing_status(self, tmp_path):
        """The agent may have run to completion and spent real money before
        the pane was lost. The failure path records cost from stdout, so
        discarding it here would lose the accounting for exactly the steps
        that went wrong."""
        herdr = FakeHerdr(rc=None, stdout='{"result": "x", "total_cost_usd": 2.5}')

        result = _run(tmp_path, herdr)

        assert "total_cost_usd" in result.stdout

    def test_the_reason_is_stated_not_left_blank(self, tmp_path):
        result = _run(tmp_path, FakeHerdr(rc=None))

        assert "status" in result.stderr.lower()

    def test_the_sentinel_is_not_readable_as_a_signal_death(self, tmp_path):
        """A NEGATIVE exit code means "killed by signal N" by convention, and
        the runner's failure context reads it that way. With a negative
        sentinel it reported `signal: SIG1024` and told the operator "an
        INFRASTRUCTURE failure (the OS terminated it)" -- every word invented
        by the sentinel itself. The real reason belongs in stderr, where it can
        be read, not encoded in a number that means something else."""
        from hivepilot.runners.claude_runner import classify_signal_exit

        result = _run(tmp_path, FakeHerdr(rc=None))

        assert result.returncode > 0
        assert classify_signal_exit(result.returncode) is None

    def test_the_sentinel_cannot_collide_with_a_real_exit_status(self, tmp_path):
        """POSIX allows 0-255. A sentinel inside that range would make some
        genuine failure indistinguishable from a lost pane."""
        result = _run(tmp_path, FakeHerdr(rc=None))

        assert result.returncode > 255


class TestTheEnvironmentAndItsSecrets:
    def test_the_environment_reaches_the_pane(self, tmp_path):
        herdr = FakeHerdr()

        _run(tmp_path, herdr, env={"ANTHROPIC_API_KEY": "sk-live"})

        run_call = next(c for c in herdr.calls if c[2] == "run")
        assert ". " in run_call[-1]

    def test_the_secrets_file_does_not_outlive_the_step(self, tmp_path):
        """It holds the step's credentials in cleartext."""
        herdr = FakeHerdr()

        _run(tmp_path, herdr, env={"ANTHROPIC_API_KEY": "sk-live"})

        assert list(tmp_path.glob("*.env.sh")) == []

    def test_the_secrets_file_is_removed_even_when_the_step_fails(self, tmp_path):
        herdr = FakeHerdr()
        herdr.wait_returncode = 1

        with pytest.raises(PaneExecutionError):
            _run(tmp_path, herdr, env={"K": "v"})

        assert list(tmp_path.glob("*.env.sh")) == []

    def test_no_environment_writes_no_file_at_all(self, tmp_path):
        herdr = FakeHerdr()

        _run(tmp_path, herdr, env={})

        run_call = next(c for c in herdr.calls if c[2] == "run")
        assert ". " not in run_call[-1]


class TestItTalksToHerdrCorrectly:
    def test_the_pane_id_is_taken_from_the_split(self, tmp_path):
        herdr = FakeHerdr()

        _run(tmp_path, herdr)

        assert next(c for c in herdr.calls if c[2] == "run")[3] == "pane-7"

    def test_the_wait_takes_the_pane_id_before_its_flags(self, tmp_path):
        """Probed against 0.8.0: with the id last, every form answers `unknown
        option: <the VALUE>` -- the parser consumes the flag and then treats
        its argument as another option. The documented order does not work."""
        herdr = FakeHerdr()

        _run(tmp_path, herdr)

        wait = next(c for c in herdr.calls if c[2] == "wait-output")
        assert wait[3] == "pane-7"
        assert wait.index("pane-7") < wait.index("--match")

    def test_the_wait_matches_this_step_s_own_marker(self, tmp_path):
        """A marker shared between steps would hand the second step the
        first one's completion -- and the first one's output."""
        herdr = FakeHerdr()

        _run(tmp_path, herdr)
        _run(tmp_path, herdr)

        markers = [c[c.index("--match") + 1] for c in herdr.calls if c[2] == "wait-output"]
        assert len(set(markers)) == len(markers)

    def test_the_marker_is_absent_from_the_command_it_waits_on(self, tmp_path):
        """`pane run` types the command and the shell echoes it, so a marker
        written literally into the command is on screen before the step starts
        -- the wait would match the echo and return immediately. Measured on
        the box: 0.0s against a command that takes 6s."""
        herdr = FakeHerdr()

        _run(tmp_path, herdr)

        wait = next(c for c in herdr.calls if c[2] == "wait-output")
        run = next(c for c in herdr.calls if c[2] == "run")
        assert wait[wait.index("--match") + 1] not in run[-1]

    def test_the_wait_is_always_bounded(self, tmp_path):
        """herdr's own help says an omitted timeout waits forever, which would
        hang the scheduler on a pane nobody is watching."""
        herdr = FakeHerdr()

        _run(tmp_path, herdr, timeout=None)

        wait = next(c for c in herdr.calls if c[2] == "wait-output")
        assert "--timeout" in wait
        assert int(wait[wait.index("--timeout") + 1]) > 0

    def test_the_timeout_is_sent_in_milliseconds(self, tmp_path):
        herdr = FakeHerdr()

        _run(tmp_path, herdr, timeout=30)

        wait = next(c for c in herdr.calls if c[2] == "wait-output")
        assert wait[wait.index("--timeout") + 1] == "30000"

    def test_a_failed_split_stops_before_running_anything(self, tmp_path):
        """Otherwise the command runs against an empty pane id and the agent
        dispatches somewhere nobody can see."""
        herdr = FakeHerdr()
        herdr.split_returncode = 1

        with pytest.raises(PaneExecutionError):
            _run(tmp_path, herdr)

        assert not any(c[2] == "run" for c in herdr.calls)

    def test_the_pane_is_closed_afterwards(self, tmp_path):
        """One pane per step, never reclaimed, is a leak the operator only
        notices when the server is unusable.

        `close` exactly: 0.8.0 has no `kill` verb. This assertion accepted
        either at first, which made it pass against a verb herdr does not
        have -- a test that cannot fail on the wrong answer is not a test."""
        herdr = FakeHerdr()

        _run(tmp_path, herdr)

        assert [c[2] for c in herdr.calls].count("close") == 1

    def test_a_target_pane_is_split_instead_of_the_focused_one(self, tmp_path):
        """`--current` means *whatever workspace the operator is looking at*.
        With one workspace that is indistinguishable from correct; with one per
        run it puts a step in another run's workspace, and merely focusing a
        different tab moves where agents appear.

        Measured on the box against TWO workspaces -- the only arrangement in
        which the right answer and `--current` differ: `pane split --pane <ID>`
        lands in <ID>'s workspace while the operator is focused elsewhere."""
        herdr = FakeHerdr()

        _run(tmp_path, herdr, target_pane="w6:p1")

        split = next(c for c in herdr.calls if c[2] == "split")
        assert split[split.index("--pane") + 1] == "w6:p1"
        assert "--current" not in split

    def test_without_a_target_it_still_splits_the_current_one(self, tmp_path):
        """A run with no workspace of its own -- the step-2 behaviour -- must
        keep working, or turning worktrees on becomes all-or-nothing."""
        herdr = FakeHerdr()

        _run(tmp_path, herdr)

        split = next(c for c in herdr.calls if c[2] == "split")
        assert "--current" in split
        assert "--pane" not in split

    def test_the_split_direction_is_one_herdr_accepts(self, tmp_path):
        """`--direction` takes `right` or `down` -- probed. Anything else
        fails the split, which fails the whole step."""
        herdr = FakeHerdr()

        _run(tmp_path, herdr)

        split = next(c for c in herdr.calls if c[2] == "split")
        assert split[split.index("--direction") + 1] in {"right", "down"}


class TestAPaneItCannotOpenMustNotCostTheStep:
    """Found by the first run with every flag on at once, not by a test.

    `--current` means "the pane the operator is looking at", and herdr resolves
    it from `HERDR_PANE_ID`. In a systemd unit that variable is empty, so the
    split answers `exit 2: --current requires HERDR_PANE_ID` -- and the step
    died. Measured on run 704: the auditor stage, which runs outside the run's
    workspace scope, failed for exactly this.

    The fallback I kept "so a run without a workspace keeps working" did not
    work in the one place it was needed: a headless box.

    And the polarity was inconsistent with my own rule. `run_workspace` already
    fails OPEN -- a dead herdr costs visibility, never a run. `run_in_pane` did
    not. A pane is a place to WATCH a step from; being unable to open one is
    not a reason to skip the step.

    `PaneExecutionError` is exactly the right thing to catch: it is raised only
    when the pane infrastructure fails. An agent that runs and exits non-zero
    comes back as a CompletedProcess and still fails the step, as it must.
    """

    def test_it_names_the_missing_variable_rather_than_the_raw_exit(self, tmp_path):
        """`exit 2` alone sends the reader to herdr's source. The cause is a
        missing environment variable and the message should say so."""
        herdr = FakeHerdr()
        herdr.split_returncode = 2

        with pytest.raises(PaneExecutionError, match="HERDR_PANE_ID|no pane to split"):
            _run(tmp_path, herdr)

    def test_the_error_is_distinguishable_from_an_agent_failure(self, tmp_path):
        """The caller falls back on one and not the other, so they must not be
        the same exception."""
        herdr = FakeHerdr(rc="1")

        result = _run(tmp_path, herdr)

        assert result.returncode == 1  # the agent failed; no exception raised
