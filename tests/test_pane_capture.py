"""Run the agent inside a pane without losing anything HivePilot does around it.

Step 2 of the agent-in-pane work. The goal is visibility, not a new contract:
the SAME argv, the SAME prompt, the SAME allowed_tools and hooks -- only the
process lives in a herdr pane instead of a bare child process.

The thing that must survive is the capture. Cost and tokens come from the JSON
envelope on stdout of `--print`, and a pane's stdout belongs to the terminal.
So the envelope is redirected to a FILE and read back, rather than scraped from
the pane: `pane read` returns a rendered terminal snapshot -- wrapped, possibly
ANSI-laden, and bounded by scrollback -- and parsing JSON out of that would
turn a reliable capture into a guess.

Two failure modes this file pins, both learned the hard way in this runner:

- the sentinel is echoed UNCONDITIONALLY, so a failing agent still announces
  completion instead of hanging to the timeout;
- the exit status survives the echo, so a failed step is not reported as a
  success because the last command in the chain was `echo`.

Default OFF. This changes how every step executes, and a runner that reaches
for a terminal multiplexer on a machine that has none must degrade rather than
fail -- the flag is the seam that makes that a choice.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from hivepilot.runners.pane_capture import (
    build_pane_command,
    capture_paths,
    write_env_file,
)


def _cmd(argv=None, sentinel="S", **kw):
    paths = kw.pop("paths", None) or capture_paths("/tmp/hp")
    return build_pane_command(argv=argv or ["claude"], paths=paths, sentinel=sentinel, **kw)


class TestTheCommandKeepsTheEnvelope:
    def test_stdout_is_redirected_to_the_capture_file(self):
        paths = capture_paths("/tmp/hp")

        cmd = _cmd(["claude", "--print", "--output-format", "json"], paths=paths)

        assert f"> {paths.stdout}" in cmd
        assert f"2> {paths.stderr}" in cmd

    def test_the_agent_argv_is_quoted_not_concatenated(self):
        """A prompt is agent- or operator-authored text and will contain
        metacharacters; an unquoted argv would let it run as shell."""
        cmd = _cmd(["claude", "--print", "say ; rm -rf /"])

        assert "'say ; rm -rf /'" in cmd

    def test_the_sentinel_is_echoed_after_the_command(self):
        cmd = _cmd(sentinel="S_abc")

        assert cmd.index("claude") < cmd.index("S_abc")

    def test_the_sentinel_is_echoed_even_on_failure(self):
        """Otherwise a failing agent hangs to the timeout and is reported as
        'timed out' -- a wrong diagnosis, not merely a slow one."""
        cmd = _cmd()

        head = cmd.split("echo")[0]
        assert head.rstrip().endswith(";") or "$?" in head

    def test_the_exit_status_survives_the_echo(self):
        """`echo` succeeds. Without capturing `$?` first, every step would
        report success."""
        cmd = _cmd()

        assert "$?" in cmd
        assert cmd.rstrip().endswith(")")

    def test_the_sentinel_reaches_the_terminal_not_the_capture_file(self):
        """It must be visible to `pane wait-output`. Redirecting it into the
        capture file would make the wait time out while the file grew."""
        paths = capture_paths("/tmp/hp")

        cmd = _cmd(paths=paths, sentinel="S_marker")
        clause = cmd.split("echo S_marker", 1)[1]

        assert ">" not in clause


class TestTheExitStatusIsWrittenDown:
    """A pane hands back no exit status: `herdr pane run` reports whether the
    HERDR command worked, not the agent inside it. Without a status the runner
    would have to infer failure from the envelope -- and a crash that never
    produced one would read as an empty success."""

    def test_the_status_is_written_to_its_own_file(self):
        paths = capture_paths("/tmp/hp")

        cmd = _cmd(paths=paths)

        assert f"> {paths.rc}" in cmd

    def test_the_status_is_captured_before_anything_else_runs(self):
        """`$?` is clobbered by the next command. Written after the echo it
        would record the echo's status -- zero, always."""
        paths = capture_paths("/tmp/hp")

        cmd = _cmd(paths=paths)

        assert cmd.index("__hp_rc=$?") < cmd.index(paths.rc)

    def test_the_status_file_is_distinct_from_the_streams(self):
        paths = capture_paths("/tmp/hp")

        assert len({paths.stdout, paths.stderr, paths.rc}) == 3


class TestTheCapturePaths:
    def test_paths_are_unique_per_step(self):
        """Two steps writing the same file would have the second read the
        first one's envelope -- and report its cost."""
        a = capture_paths("/tmp/base")
        b = capture_paths("/tmp/base")

        assert a.stdout != b.stdout

    def test_stdout_and_stderr_are_separate(self):
        paths = capture_paths("/tmp/base")

        assert paths.stdout != paths.stderr

    def test_they_live_under_the_given_directory(self):
        paths = capture_paths("/var/lib/hivepilot/panes")

        assert paths.stdout.startswith("/var/lib/hivepilot/panes")

    @pytest.mark.parametrize("attr", ["stdout", "stderr", "rc"])
    def test_no_shell_metacharacters_in_a_generated_path(self, attr):
        """They are interpolated into a shell command."""
        value = getattr(capture_paths("/tmp/base"), attr)

        assert not any(c in value for c in " ;&|$`'\"*?")


class TestTheEnvironmentTravelsToThePane:
    """The pane belongs to the herdr SERVER, and inherits its environment --
    not the scrubbed, overlaid, sandboxed one the runner spent its whole
    invocation building. Run the argv there as-is and the agent loses its API
    key, its OTel destination and every secret the step was granted."""

    def test_the_environment_is_sourced_before_the_command(self):
        cmd = _cmd(env_file="/tmp/hp/env.sh")

        assert cmd.index("env.sh") < cmd.index("claude")

    def test_the_environment_replaces_rather_than_adds_to_the_pane_s(self):
        """`subprocess.run(env=...)` REPLACES. Sourcing only ADDS, and the
        pane starts with the herdr SERVER's environment -- so a plain
        `. file; cmd` hands the agent everything the server carries on top of
        what we chose, including what `_apply_sandbox` took away. The sandbox
        would still be there and would no longer be deciding anything."""
        cmd = _cmd(env_file="/tmp/hp/env.sh")

        assert "env -i" in cmd
        assert cmd.index("env -i") < cmd.index("env.sh")

    def test_the_agent_argv_is_not_interpolated_into_the_inner_script(self):
        """It goes through one more layer of quoting than the direct path, and
        a nested escape of a prompt full of quotes is where an injection would
        hide. Passing it as positional parameters means the inner shell never
        parses it."""
        cmd = _cmd(["claude", "--print", 'it\'s "quoted"; id'], env_file="/tmp/e.sh")

        assert 'exec "$0" "$@"' in cmd

    def test_values_are_quoted(self, tmp_path):
        """A value with a space or a backtick would otherwise be executed at
        source time -- and secrets are exactly the values most likely to hold
        punctuation."""
        target = tmp_path / "env.sh"

        write_env_file(str(target), {"K": "a b; echo pwned `id`"})

        assert "export K='a b; echo pwned `id`'" in target.read_text()

    def test_the_file_is_not_readable_by_other_users(self, tmp_path):
        """It holds the step's secrets in cleartext on a shared box."""
        target = tmp_path / "env.sh"

        write_env_file(str(target), {"ANTHROPIC_API_KEY": "sk-live"})

        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_a_hostile_key_is_dropped_rather_than_written(self, tmp_path):
        """`shlex.quote` protects the VALUE. A key is not quoted -- it is a
        shell identifier -- so a key carrying a newline would inject a whole
        line no quoting could contain."""
        target = tmp_path / "env.sh"

        write_env_file(str(target), {"OK": "1", "BAD\nexport EVIL": "x", "AL SO": "y"})

        body = target.read_text()
        assert "export OK=1\n" in body
        assert "EVIL" not in body
        assert "AL SO" not in body

    def test_no_environment_means_no_source_clause(self):
        """Sourcing a file that was never written would fail the whole line
        before the agent ever started."""
        cmd = _cmd(env_file=None)

        assert cmd.startswith("claude ")

    def test_the_working_directory_is_entered_first(self):
        """The pane starts wherever the server was launched. `state.db`, the
        plugin directory and `runs/` are all cwd-relative, so a step running
        from the wrong directory reads a different database."""
        cmd = _cmd(cwd="/srv/projects/noxys")

        assert cmd.index("cd /srv/projects/noxys") < cmd.index("claude")


class TestTheComposedLineActuallyRuns:
    """Every test above reads the string. These run it.

    A shell line can satisfy every substring assertion and still be a syntax
    error, or quote something one level too few. Only a shell settles that."""

    @staticmethod
    def _execute(cmd: str) -> str:
        import subprocess

        return subprocess.run(
            ["/bin/sh", "-c", cmd], capture_output=True, text=True, check=False
        ).stdout

    def test_the_status_and_streams_land_where_promised(self, tmp_path):
        paths = capture_paths(str(tmp_path))
        cmd = build_pane_command(
            argv=["/bin/sh", "-c", "echo OUT; echo ERR >&2; exit 3"],
            paths=paths,
            sentinel="S_run",
        )

        terminal = self._execute(cmd)

        assert Path(paths.stdout).read_text() == "OUT\n"
        assert Path(paths.stderr).read_text() == "ERR\n"
        assert Path(paths.rc).read_text().strip() == "3"
        assert "S_run" in terminal

    def test_a_prompt_full_of_metacharacters_is_never_executed(self, tmp_path):
        """The one that matters: an agent-authored prompt reaching the shell."""
        paths = capture_paths(str(tmp_path))
        hostile = "$(touch " + str(tmp_path / "pwned") + ") `id` ; echo no"
        cmd = build_pane_command(argv=["/bin/echo", hostile], paths=paths, sentinel="S")

        self._execute(cmd)

        assert not (tmp_path / "pwned").exists()
        assert Path(paths.stdout).read_text().strip() == hostile

    def test_the_environment_is_the_one_we_wrote_and_nothing_else(self, tmp_path):
        """Run with a variable set in the CALLING shell that we did not put in
        the file. Under replacement semantics the agent must not see it."""
        paths = capture_paths(str(tmp_path))
        env_file = str(tmp_path / "env.sh")
        write_env_file(env_file, {"PATH": os.environ["PATH"], "MINE": "yes"})
        cmd = build_pane_command(
            argv=["/bin/sh", "-c", 'echo "mine=$MINE theirs=${THEIRS:-absent}"'],
            paths=paths,
            sentinel="S",
            env_file=env_file,
        )

        self._execute(f"THEIRS=leaked; export THEIRS; {cmd}")

        assert Path(paths.stdout).read_text().strip() == "mine=yes theirs=absent"
