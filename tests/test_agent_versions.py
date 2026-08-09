"""Is the installed agent CLI CURRENT?

`probe_version` (#449) already answers "which version" and `agents list`
answers "is it on PATH". Neither answered "and is that the latest", nor "how
would you update it" — which turns out to depend on how the CLI was
installed, not on which CLI it is.

Deliberately generic. `claude` is one of twelve agent kinds
(`AGENT_RUNNER_KINDS`), and hard-coding it into the engine would break the
rule that the engine works for any deployment. The npm detection below keys
off the resolved binary path, not off a name.

**Updating is never automatic.** The probe reports; an operator decides. An
agent CLI updating itself underneath a running fleet changes the behaviour of
every role at once, with no signal in any run that anything moved.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.services import agent_versions as av


def _messages(findings) -> str:
    return " | ".join(f.message for f in findings)


def _severities(findings) -> set[str]:
    return {f.severity for f in findings}


class TestParsingTheVersionOutput:
    """Each CLI prints its version differently; none of them print only it."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2.1.220 (Claude Code)", "2.1.220"),
            ("claude-code/1.4.0 linux-x64", "1.4.0"),
            ("v0.9.13", "0.9.13"),
            ("codex 0.12.1\n", "0.12.1"),
            ("  1.0.0  ", "1.0.0"),
        ],
    )
    def test_a_version_is_extracted_from_noisy_output(self, raw: str, expected: str) -> None:
        assert av._parse_version(raw) == expected

    def test_output_with_no_version_yields_none(self) -> None:
        """None must not be confused with a version — it means the probe ran
        and could not tell, which is a different fact from "old"."""
        assert av._parse_version("command not found") is None
        assert av._parse_version("") is None


class TestComparingVersions:
    def test_a_newer_latest_is_outdated(self) -> None:
        assert av._is_outdated("2.1.220", "2.2.0") is True

    def test_the_same_version_is_not_outdated(self) -> None:
        assert av._is_outdated("2.1.220", "2.1.220") is False

    def test_a_newer_local_is_not_outdated(self) -> None:
        """A local build ahead of the registry is normal, not a problem."""
        assert av._is_outdated("2.2.0", "2.1.220") is False

    def test_numeric_segments_compare_as_numbers_not_strings(self) -> None:
        """The reason this needs code at all: "2.1.220" < "2.1.99" as strings,
        which would report a current CLI as outdated forever."""
        assert av._is_outdated("2.1.220", "2.1.99") is False
        assert av._is_outdated("2.1.99", "2.1.220") is True

    def test_an_unparseable_pair_is_undecidable_not_false(self) -> None:
        """Returning False would quietly claim "up to date"."""
        assert av._is_outdated("nightly", "2.1.0") is None
        assert av._is_outdated("2.1.0", None) is None


class TestDetectingHowItWasInstalled:
    """Keyed off the resolved path, so it works for every npm-installed agent
    CLI rather than for a hard-coded list of names."""

    def test_an_npm_global_package_is_recognised(self) -> None:
        p = Path("/usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe")

        assert av._npm_package_for(p) == "@anthropic-ai/claude-code"

    def test_an_unscoped_npm_package_is_recognised(self) -> None:
        p = Path("/usr/lib/node_modules/opencode/bin/opencode")

        assert av._npm_package_for(p) == "opencode"

    def test_a_non_npm_binary_yields_none(self) -> None:
        """A distro package or a static binary must not be handed an
        `npm install -g` instruction that would not work."""
        assert av._npm_package_for(Path("/usr/bin/codex")) is None
        assert av._npm_package_for(None) is None


class TestProbing:
    def test_a_missing_binary_is_reported_not_raised(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(av.shutil, "which", lambda _n: None)

        probe = av.probe_agent_cli("claude")

        assert probe.on_path is False
        assert probe.version is None

    def test_a_probe_that_times_out_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """This shells out to an arbitrary binary. A hung CLI must not hang
        `config doctor`."""
        monkeypatch.setattr(av.shutil, "which", lambda _n: "/usr/bin/claude")

        def boom(*_a: object, **_k: object) -> object:
            raise av.subprocess.TimeoutExpired(cmd="claude", timeout=5)

        monkeypatch.setattr(av.subprocess, "run", boom)

        probe = av.probe_agent_cli("claude")

        assert probe.on_path is True
        assert probe.version is None
        assert probe.error is not None

    def test_a_successful_probe_reports_the_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(av.shutil, "which", lambda _n: "/usr/bin/claude")
        monkeypatch.setattr(
            av.subprocess,
            "run",
            lambda *a, **k: av.subprocess.CompletedProcess(a, 0, "2.1.220 (Claude Code)", ""),
        )

        probe = av.probe_agent_cli("claude")

        assert probe.version == "2.1.220"
        assert probe.error is None


class TestTheDoctorCheck:
    def test_only_ACTIVE_agent_kinds_are_probed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Twelve agent kinds exist and a deployment runs one or two. Probing
        all of them would print ten "not installed" lines that are not
        problems — the noise that made the first draft of
        `plugins_written_vs_installed` useless."""
        monkeypatch.setattr(av, "active_agent_runner_kinds", lambda: {"claude"})
        monkeypatch.setattr(
            av,
            "probe_agent_cli",
            lambda kind: av.AgentCliProbe(kind, "/usr/bin/x", True, "1.0", None, None),
        )

        findings = av.check_agent_cli_versions()

        assert len(findings) == 1
        assert "claude" in findings[0].message

    def test_an_active_agent_missing_from_PATH_is_a_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Registered and dispatchable, but every call will fail."""
        monkeypatch.setattr(av, "active_agent_runner_kinds", lambda: {"claude"})
        monkeypatch.setattr(
            av,
            "probe_agent_cli",
            lambda kind: av.AgentCliProbe(kind, None, False, None, None, None),
        )

        findings = av.check_agent_cli_versions()

        assert findings[0].severity == "warning"

    def test_the_version_is_printed_even_when_healthy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Same rule as the rest of the liveness checks: the number now is
        what makes a future number mean something."""
        monkeypatch.setattr(av, "active_agent_runner_kinds", lambda: {"claude"})
        monkeypatch.setattr(
            av,
            "probe_agent_cli",
            lambda kind: av.AgentCliProbe(kind, "/usr/bin/claude", True, "2.1.220", None, None),
        )

        findings = av.check_agent_cli_versions()

        assert findings[0].severity == "info"
        assert "2.1.220" in findings[0].message

    def test_the_doctor_never_reaches_the_network(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`config doctor` must stay fast and work offline. Checking the
        registry is opt-in, on the CLI command only."""
        called: list[str] = []
        monkeypatch.setattr(av, "active_agent_runner_kinds", lambda: {"claude"})
        monkeypatch.setattr(
            av,
            "probe_agent_cli",
            lambda kind: av.AgentCliProbe(kind, "/usr/bin/claude", True, "2.1.220", None, None),
        )

        def _record(pkg: str) -> str:
            called.append(pkg)
            return "9.9.9"

        monkeypatch.setattr(av, "fetch_latest_npm_version", _record)

        av.check_agent_cli_versions()

        assert called == []


class TestProbeVersionIsUnchanged:
    """`probe_version` predates this work (#449) and its callers in `cli.py`
    depend on its exact contract: the FIRST LINE of `--version`, capped, or
    None on any failure.

    Pinned because I overwrote this module while adding the newer helpers
    below, and briefly replaced `probe_version` with a parsed-version variant.
    Every caller would still have "worked" — they interpolate it into a
    string — while quietly reporting something different from what they were
    written to report.
    """

    def test_it_returns_the_whole_first_line_not_a_parsed_number(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            av.subprocess,
            "run",
            lambda *a, **k: av.subprocess.CompletedProcess(a, 0, "2.1.220 (Claude Code)\n", ""),
        )

        assert av.probe_version("claude") == "2.1.220 (Claude Code)"

    def test_a_nonzero_exit_yields_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            av.subprocess,
            "run",
            lambda *a, **k: av.subprocess.CompletedProcess(a, 1, "usage: ...", ""),
        )

        assert av.probe_version("claude") is None

    def test_a_failing_probe_yields_none_rather_than_raising(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: object, **_k: object) -> object:
            raise OSError("exec format error")

        monkeypatch.setattr(av.subprocess, "run", boom)

        assert av.probe_version("claude") is None


class TestApiOnlyKindsAreNotMissingCLIs:
    """`openrouter` reaches its model over HTTP and has no binary, ever.

    The first version of this check reported it as "registered as a runner
    but its CLI is not on PATH" and advised: *install the openrouter CLI, or
    disable the runner*. Following that advice would have disabled a working
    API runner — the same failure as recommending deletion of the live
    approvals topic: a cleanup suggestion that breaks a working feature is
    worse than no suggestion.

    The knowledge existed, as a private local in `cli.py`'s table, where no
    second consumer could see it. It is now `API_ONLY_AGENT_KINDS` in
    `agent_checks`, beside `AGENT_RUNNER_KINDS`.
    """

    def test_an_api_only_kind_is_not_reported_as_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(av, "active_agent_runner_kinds", lambda: {"openrouter"})
        monkeypatch.setattr(
            av,
            "probe_agent_cli",
            lambda kind: av.AgentCliProbe(kind, None, False, None, None, None),
        )

        findings = av.check_agent_cli_versions()

        assert _severities(findings) == {"info"}
        assert "API-only" in _messages(findings)

    def test_the_advice_never_says_to_disable_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The specific harm: disabling a runner that works."""
        monkeypatch.setattr(av, "active_agent_runner_kinds", lambda: {"openrouter"})
        monkeypatch.setattr(
            av,
            "probe_agent_cli",
            lambda kind: av.AgentCliProbe(kind, None, False, None, None, None),
        )

        assert all("_ENABLED=false" not in f.fix for f in av.check_agent_cli_versions())

    def test_it_is_still_REPORTED_rather_than_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Silence would make "no version line" ambiguous between "API-only"
        and "we forgot about it"."""
        monkeypatch.setattr(av, "active_agent_runner_kinds", lambda: {"openrouter"})
        monkeypatch.setattr(
            av,
            "probe_agent_cli",
            lambda kind: av.AgentCliProbe(kind, None, False, None, None, None),
        )

        assert [f for f in av.check_agent_cli_versions() if "openrouter" in f.message]

    def test_a_CLI_backed_kind_missing_from_PATH_still_warns(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard must keep doing its job for kinds that DO need a binary."""
        monkeypatch.setattr(av, "active_agent_runner_kinds", lambda: {"claude"})
        monkeypatch.setattr(
            av,
            "probe_agent_cli",
            lambda kind: av.AgentCliProbe(kind, None, False, None, None, None),
        )

        assert _severities(av.check_agent_cli_versions()) == {"warning"}

    def test_the_canonical_set_is_shared_not_duplicated(self) -> None:
        """It lived as a private local in cli.py, which is exactly why the
        doctor check could not see it."""
        from hivepilot.services.agent_checks import AGENT_RUNNER_KINDS, API_ONLY_AGENT_KINDS

        assert "openrouter" in API_ONLY_AGENT_KINDS
        assert API_ONLY_AGENT_KINDS <= AGENT_RUNNER_KINDS
