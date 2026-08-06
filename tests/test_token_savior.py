"""token-savior: wire the MCP server onto a step, honestly.

token-savior (github.com/mibayy/token-savior, MIT) is an MCP server that
indexes a codebase into symbols so an agent can fetch `get(symbol)` instead
of reading whole files. HivePilot's claude runner already knows how to pass
`--mcp-config` and `--allowed-tools`; what was missing is the wiring, which
otherwise means hand-writing a JSON stanza per deployment.

Three positions this file encodes.

**Opt-in, default off.** The server indexes the repository and keeps a
memory database. That is a decision for whoever owns the code, so it
follows `headroom_enabled`'s precedent rather than `rtk_enabled`'s.

**Never override an explicit choice.** A role that already declares
`mcp_config` has been configured deliberately. The hook fills a gap; it does
not take the wheel.

**The README's numbers are not ours.** It claims 97.9% vs 78.3% and -80%
active tokens, measured by its author on its author's benchmark. This plugin
therefore reports no savings of its own -- inventing a counter from a vendor
ratio is how `codebase-memory-mcp`'s 99.2% became ~10% here. HivePilot
already records `input_tokens`/`cache_read_tokens`/`cost_usd` per step; the
honest measurement is the same review run twice.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hivepilot import plugins as plugin_mod


@pytest.fixture
def token_savior(monkeypatch, tmp_path):
    """Load the plugin module with the feature enabled and a temp config dir."""
    import importlib

    module = importlib.import_module("plugins.token_savior")
    importlib.reload(module)
    monkeypatch.setattr(module.settings, "token_savior_enabled", True)
    monkeypatch.setattr(module, "_config_dir", lambda: tmp_path)
    # The binary is not installed in CI or on a dev box, and every wiring
    # test is about what happens once it IS — its absence has its own tests.
    monkeypatch.setattr(module, "_server_available", lambda: True)
    return module


class _Step:
    def __init__(self, **metadata):
        self.name = "review"
        self.metadata = dict(metadata)


class _Project:
    def __init__(self, path: str):
        self.path = path


class _Payload:
    def __init__(self, step=None, *, project: Any = None, project_name: str | None = None):
        self.step = step or _Step()
        self.metadata: dict[str, Any] = {}
        # Declared rather than bolted on afterwards: the hook reads both, and
        # a double that only grows them at the call site type-checks nowhere
        # and drifts from the real payload silently.
        self.project = project
        self.project_name = project_name


class TestGating:
    def test_it_is_off_by_default(self) -> None:
        """A server that indexes the codebase and stores memory is the
        owner's call, not a default."""
        from hivepilot.config import Settings

        assert Settings().token_savior_enabled is False

    def test_disabled_contributes_nothing(self, token_savior, monkeypatch) -> None:
        monkeypatch.setattr(token_savior.settings, "token_savior_enabled", False)

        assert token_savior.register() == {}

    def test_enabled_contributes_a_hook_and_a_health_check(self, token_savior) -> None:
        contributed = token_savior.register()

        assert "before_step" in contributed
        # A dict, not a bare function — `_load_into` calls `.items()` on it.
        # The first version returned the function and passed every unit test
        # here while failing at real load, which is what the manager test
        # below now catches.
        assert callable(contributed["health"]["token_savior"])

    def test_it_declares_filesystem_and_not_subprocess(self, token_savior) -> None:
        """A token-savior process is spawned, but not by this module — the
        agent CLI launches it from the config stanza. This module writes one
        JSON file.

        `plugins_capability_policy` defaults to deny-everything, so an
        inflated manifest costs an operator an allowlist entry for the most
        load-bearing token there is, and discriminates nothing.
        """
        capabilities = token_savior.register()["capabilities"]

        assert capabilities == ["filesystem"]


class TestItLoadsForReal:
    def test_the_plugin_manager_accepts_it(self, token_savior, monkeypatch) -> None:
        """Unit tests asserting on `register()`'s keys cannot see a shape
        error — `health` was a bare function and every one of them passed.
        Only a real load calls `.items()` on it.
        """
        from hivepilot.plugins import PluginManager

        monkeypatch.setenv("HIVEPILOT_TOKEN_SAVIOR_ENABLED", "1")
        monkeypatch.setattr("hivepilot.config.settings.plugins_capability_policy", ["filesystem"])

        manager = PluginManager()

        assert "token_savior" in manager.health
        # And it runs, through the manager's own never-raise wrapper.
        assert manager.run_health_check("token_savior").status in ("ok", "degraded")


class TestWiringAStep:
    def test_it_points_the_step_at_an_mcp_config(self, token_savior) -> None:
        payload = _Payload()

        token_savior.before_step(payload=payload)

        assert payload.step.metadata["mcp_config"]

    def test_the_config_it_writes_is_valid_and_names_the_server(
        self, token_savior, tmp_path
    ) -> None:
        payload = _Payload()

        token_savior.before_step(payload=payload)
        written = json.loads((tmp_path / token_savior._CONFIG_FILENAME).read_text())

        assert token_savior._SERVER_NAME in written["mcpServers"]

    def test_the_index_points_at_the_step_s_own_project(self, token_savior, tmp_path) -> None:
        """This is the whole reason the config is generated rather than
        hand-written. `WORKSPACE_ROOTS` decides which repository gets
        indexed, so a stale one gives the agent confident answers about
        somebody else's code — wrong in the way that reads as right.
        """
        payload = _Payload(project=_Project("/srv/repos/noxys"), project_name="noxys")

        token_savior.before_step(payload=payload)
        written = json.loads((tmp_path / "noxys.mcp.json").read_text())

        env = written["mcpServers"][token_savior._SERVER_NAME]["env"]
        assert env["WORKSPACE_ROOTS"] == "/srv/repos/noxys"

    def test_two_projects_do_not_share_one_config(self, token_savior, tmp_path) -> None:
        """One file per project, or the second run silently re-points the
        first project's index."""
        for name, path in (("noxys", "/srv/a"), ("mirador", "/srv/b")):
            token_savior.before_step(payload=_Payload(project=_Project(path), project_name=name))

        assert (tmp_path / "noxys.mcp.json").exists()
        assert (tmp_path / "mirador.mcp.json").exists()

    def test_a_project_name_cannot_escape_the_config_dir(self, token_savior, tmp_path) -> None:
        """Project names come from config, but they become a filename."""
        payload = _Payload(project=_Project("/srv/x"), project_name="../../etc/evil")

        token_savior.before_step(payload=payload)

        written = Path(payload.step.metadata["mcp_config"])
        assert written.parent == tmp_path

    def test_it_disables_its_own_memory_engine(self, token_savior, tmp_path) -> None:
        """token-savior ships a memory engine; HivePilot already has mem0.

        Two stores recalling near-identical context into one prompt is paying
        twice for one fact. It happens to be inert today only because the
        `[memory-vector]` extra is not installed — an accident of packaging
        that any future `pip install` would undo silently.
        """
        payload = _Payload(project=_Project("/srv/x"), project_name="x")

        token_savior.before_step(payload=payload)
        written = json.loads((tmp_path / "x.mcp.json").read_text())

        env = written["mcpServers"][token_savior._SERVER_NAME]["env"]
        assert env["TS_MEMORY_DISABLE"] == "1"

    def test_it_pre_approves_only_its_own_tools(self, token_savior) -> None:
        """`--allowed-tools` is a whitelist that RESTRICTS. Adding a broad
        entry here would widen what every other tool may do."""
        payload = _Payload()

        token_savior.before_step(payload=payload)

        for tool in payload.step.metadata["allowed_tools"]:
            assert tool.startswith(f"mcp__{token_savior._SERVER_NAME}")

    def test_existing_allowed_tools_survive(self, token_savior) -> None:
        """Replacing the list would silently revoke grants a role depends on
        — the defect that cost three wrong hypotheses on the groomer."""
        payload = _Payload(_Step(allowed_tools=["Bash(rtk gh pr diff:*)"]))

        token_savior.before_step(payload=payload)

        assert "Bash(rtk gh pr diff:*)" in payload.step.metadata["allowed_tools"]


class TestItNeverTakesTheWheel:
    def test_an_explicit_mcp_config_is_left_alone(self, token_savior) -> None:
        payload = _Payload(_Step(mcp_config="/etc/hivepilot/mine.json"))

        token_savior.before_step(payload=payload)

        assert payload.step.metadata["mcp_config"] == "/etc/hivepilot/mine.json"

    def test_it_does_not_add_its_tools_to_someone_elses_config(self, token_savior) -> None:
        """Pre-approving tools for a server that is not loaded is noise at
        best and a false sense of availability at worst."""
        payload = _Payload(_Step(mcp_config="/etc/hivepilot/mine.json"))

        token_savior.before_step(payload=payload)

        assert "allowed_tools" not in payload.step.metadata


class TestItNeverBreaksAStep:
    def test_a_missing_payload_is_survived(self, token_savior) -> None:
        token_savior.before_step()

    def test_a_payload_without_a_step_is_survived(self, token_savior) -> None:
        token_savior.before_step(payload=object())

    def test_an_unwritable_config_dir_is_survived(self, token_savior, monkeypatch) -> None:
        """A pipeline must not die because an optional index could not be
        wired. It degrades to running without the server."""
        monkeypatch.setattr(
            token_savior, "_config_dir", lambda: (_ for _ in ()).throw(OSError("nope"))
        )
        payload = _Payload()

        token_savior.before_step(payload=payload)

        assert "mcp_config" not in payload.step.metadata


class TestHealthTellsTheTruth:
    def test_absent_is_reported_as_degraded_not_ok(self, token_savior, monkeypatch) -> None:
        """ "Enabled" is not "working". A health check that says ok because a
        flag is set is the defect the Health panel was rebuilt to remove."""
        monkeypatch.setattr(token_savior, "_server_available", lambda: False)

        status = token_savior.health()

        assert status.status == "degraded"

    def test_present_is_reported_ok(self, token_savior, monkeypatch) -> None:
        monkeypatch.setattr(token_savior, "_server_available", lambda: True)

        assert token_savior.health().status == "ok"

    def test_the_detail_says_which_it_is(self, token_savior, monkeypatch) -> None:
        monkeypatch.setattr(token_savior, "_server_available", lambda: False)

        assert "token-savior" in token_savior.health().detail.lower()

    def test_health_never_raises(self, token_savior, monkeypatch) -> None:
        monkeypatch.setattr(
            token_savior,
            "_server_available",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        assert token_savior.health().status in plugin_mod.HEALTH_STATUSES

    def test_it_claims_no_savings_of_its_own(self, token_savior) -> None:
        """The README's 97.9%/-80% were measured by its author on its
        author's benchmark. Surfacing a vendor ratio as if it were our
        telemetry is exactly how codebase-memory-mcp's 99.2% became ~10%."""
        detail = token_savior.health().detail

        for vendor_number in ("97.9", "80%", "83%", "78.3"):
            assert vendor_number not in detail
