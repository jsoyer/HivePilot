"""Enabling a plugin must install what the plugin needs, not just name it.

`plugins enable herdr` declares its prerequisite -- "the `herdr` binary on
PATH" -- and then installs nothing. `_install_binary` only consults
`AGENT_INSTALL_SPECS`, which covers coding-agent CLIs, so every non-agent
plugin (herdr, tmux, rtk, token_savior, orca) reaches the install step, finds
no spec, and silently returns False. The operator is told the prerequisite is
missing by a plugin they just asked to turn on.

The operator's requirement is the plain one: enabling a plugin installs it.

Two shapes of prerequisite, and conflating them is what makes this awkward:

- a BINARY: drop it on PATH (herdr, tmux). One command, done.
- a SERVICE: Orca is an AppImage plus a systemd unit plus an update job plus
  a pairing step. Nothing about that fits "put a file on PATH", and pretending
  it does would produce an enable that reports success over a service that is
  not running.

Every command here stays under the existing discipline of `agent_install`:
pinned, maintainer-vetted constants, executed only through `propose_install`,
which refuses outright without a TTY on both ends. Nothing dynamic is ever
concatenated in.
"""

from __future__ import annotations

import pytest

from hivepilot.services import agent_install


class TestNonAgentToolsHaveInstallSpecs:
    @pytest.mark.parametrize("tool", ["herdr", "orca"])
    def test_the_tool_is_registered(self, tool):
        spec = agent_install.get_install_spec(tool)

        assert spec is not None, f"{tool} has no install spec, so enabling it installs nothing"

    def test_herdr_installs_a_binary(self):
        spec = agent_install.get_install_spec("herdr")

        assert spec.binary == "herdr"
        assert spec.command, "herdr is installable; a docs-only spec would leave enable() inert"

    def test_orca_is_declared_a_service_not_a_binary(self):
        """An AppImage under systemd with its own updater is not a PATH drop.
        Reporting it as one would let `enable` claim success over a dead
        service."""
        spec = agent_install.get_install_spec("orca")

        assert spec.kind == "service"

    def test_a_coding_agent_stays_a_binary(self):
        """The new field must not reclassify what already worked."""
        assert agent_install.get_install_spec("claude").kind == "binary"


class TestTheVettingDisciplineHolds:
    def test_every_spec_cites_its_source(self):
        for name, spec in agent_install.all_install_specs().items():
            assert spec.docs_url.startswith("https://"), f"{name} has no citable source"

    def test_no_spec_interpolates_anything(self):
        """`command` is executed as a shell pipeline. A format placeholder in
        one is how a constant becomes an injection point."""
        for name, spec in agent_install.all_install_specs().items():
            if spec.command:
                assert "{" not in spec.command, f"{name}'s command looks interpolated"
                assert "$(" not in spec.command, f"{name}'s command substitutes a subshell"

    def test_the_orca_appimage_comes_from_its_own_release_channel(self):
        """Pinned to the vendor's `latest/download` URL, which is what the
        operator's own working update script already fetches -- not a mirror
        and not a guess."""
        spec = agent_install.get_install_spec("orca")

        assert "github.com/stablyai/orca/releases" in (spec.command or "")


class TestEnableReachesTheToolSpecs:
    def test_a_non_agent_kind_resolves_a_spec(self, monkeypatch):
        """The defect in one assertion: `_install_binary` used to see only
        agent kinds, so herdr fell through to 'nothing to install'."""
        from hivepilot.services import plugin_enable

        seen: dict = {}

        def _fake_propose(spec, assume_yes=False):
            seen["binary"] = spec.binary

            class R:
                ran = True
                exit_code = 0

            return R()

        monkeypatch.setattr(agent_install, "propose_install", _fake_propose)

        assert plugin_enable._install_binary("herdr") is True
        assert seen["binary"] == "herdr"

    def test_a_service_kind_is_not_installed_as_a_binary(self, monkeypatch):
        """`_install_binary` must decline a service rather than run its
        command and report a PATH install that never happened."""
        from hivepilot.services import plugin_enable

        called = False

        def _fake_propose(spec, assume_yes=False):
            nonlocal called
            called = True
            raise AssertionError("a service must not go through the binary path")

        monkeypatch.setattr(agent_install, "propose_install", _fake_propose)

        assert plugin_enable._install_binary("orca") is False
        assert called is False

    def test_an_unknown_kind_still_installs_nothing(self, monkeypatch):
        from hivepilot.services import plugin_enable

        monkeypatch.setattr(
            agent_install,
            "propose_install",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")),
        )

        assert plugin_enable._install_binary("no-such-kind") is False


class TestHerdrIsFetchedPerArchitecture:
    """The operator's instruction, in a test: install herdr by command line,
    not Homebrew. The Debian box has no brew and herdr was already there at
    0.7.5, so a package manager we would have to install first is not an
    install path."""

    def test_it_does_not_go_through_homebrew(self):
        spec = agent_install.get_install_spec("herdr")

        assert "brew" not in (spec.command or "")

    def test_it_fetches_the_release_asset_for_this_machine(self):
        spec = agent_install.get_install_spec("herdr")

        assert "github.com/ogulcancelik/herdr/releases" in spec.command
        assert "herdr-linux-x86_64" in spec.command

    def test_an_architecture_without_an_asset_is_docs_only(self, monkeypatch):
        """Better no command than one that downloads a binary for the wrong
        machine and leaves it on PATH, where it fails much later as an exec
        format error."""
        import platform

        monkeypatch.setattr(platform, "machine", lambda: "riscv64")

        assert agent_install.get_install_spec("herdr") is None

    def test_the_arch_lookup_is_case_insensitive(self, monkeypatch):
        import platform

        monkeypatch.setattr(platform, "machine", lambda: "X86_64")

        assert agent_install.get_install_spec("herdr") is not None
