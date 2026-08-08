"""Nothing records which agent CLI version produced a run.

`doctor` reports whether `claude` is on PATH and stops there. The box runs
2.1.220 and a change would pass unnoticed — which matters more than it
sounds, because the CLI *is* the runtime:

- `WaitForMcpServers`, which `token_savior` bootstraps against, is a Claude
  Code internal whose name is version-dependent.
- `--mcp-config` and `--strict-mcp-config` are recent flags.
- `Read(./**)`-style scoped permission specifiers, which the noxys roles now
  depend on for secret containment, are a permission-syntax feature. If a
  future CLI parsed that string differently, a grant we rely on to *refuse*
  `/etc/hivepilot/shared.env` could quietly widen.

So this reports the version. It deliberately does **not** update anything:
updating the agent CLI would change every role's behaviour with no PR, no
review and no verdict, and the deployment already sets
`CLAUDE_CODE_DISABLE_LEGACY_MODEL_REMAP` precisely because an update can
remap a model silently. Being a version behind is the lesser harm; not
knowing which version is the one to fix.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hivepilot.services.agent_versions import probe_version


class TestItReadsTheVersion:
    def test_first_line_of_stdout(self) -> None:
        with patch("hivepilot.services.agent_versions.subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout="2.1.220 (Claude Code)\n", stderr="")

            assert probe_version("claude") == "2.1.220 (Claude Code)"

    def test_only_the_first_line(self) -> None:
        """Some CLIs print a banner after the version. The banner is noise in
        a diagnostic table."""
        with patch("hivepilot.services.agent_versions.subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout="1.2.3\nupdate available\n", stderr="")

            assert probe_version("codex") == "1.2.3"

    def test_stderr_is_used_when_stdout_is_empty(self) -> None:
        """`--version` on stderr is common enough to handle rather than
        report as unknown."""
        with patch("hivepilot.services.agent_versions.subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout="", stderr="v0.9.1\n")

            assert probe_version("gemini") == "v0.9.1"


class TestItNeverBreaksTheDoctor:
    def test_missing_binary_is_none(self) -> None:
        with patch("hivepilot.services.agent_versions.subprocess.run") as m:
            m.side_effect = FileNotFoundError()

            assert probe_version("nope") is None

    def test_nonzero_exit_is_none(self) -> None:
        """A CLI that does not understand `--version` has told us nothing.
        Reporting its usage text as a version would be worse than silence."""
        with patch("hivepilot.services.agent_versions.subprocess.run") as m:
            m.return_value = MagicMock(returncode=2, stdout="usage: ...", stderr="")

            assert probe_version("weird") is None

    def test_timeout_is_none(self) -> None:
        """A diagnostic must not hang the command it diagnoses."""
        import subprocess

        with patch("hivepilot.services.agent_versions.subprocess.run") as m:
            m.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=5)

            assert probe_version("claude") is None

    def test_any_other_exception_is_none(self) -> None:
        with patch("hivepilot.services.agent_versions.subprocess.run") as m:
            m.side_effect = OSError("boom")

            assert probe_version("claude") is None

    def test_empty_output_is_none_not_empty_string(self) -> None:
        """An empty string would render as `()` in the table and read as a
        version of nothing."""
        with patch("hivepilot.services.agent_versions.subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout="   \n", stderr="")

            assert probe_version("claude") is None


class TestItStaysReadable:
    def test_a_long_line_is_bounded(self) -> None:
        with patch("hivepilot.services.agent_versions.subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout="x" * 500, stderr="")

            out = probe_version("claude")

        assert out is not None
        assert len(out) <= 80

    def test_it_asks_with_version_only(self) -> None:
        """No subcommand, no flags that could do work. `--version` is the one
        invocation safe to run against an unknown binary."""
        with patch("hivepilot.services.agent_versions.subprocess.run") as m:
            m.return_value = MagicMock(returncode=0, stdout="1.0", stderr="")
            probe_version("claude")

        assert m.call_args.args[0] == ["claude", "--version"]
