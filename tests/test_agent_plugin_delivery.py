"""An agent CLI whose plugin file never reaches the box is a silent removal.

`plugins/*.py` does not ship in the wheel, and `hivepilot plugins install` only
knows the curated fetch registry. Agent CLIs are excluded from it on purpose --
`plugin_installer` says they are "already covered by the guided `hivepilot
agents install` flow", and they are NOT: that flow installs the BINARY and has
never placed a plugin file. The comment describes a coverage that does not
exist.

Measured on the box before writing this, which is what makes it a fact rather
than a reading of the code:

    vibe.py     2026-08-14 16:44   <- copied by hand after #520
    codex.py    ABSENT
    cursor.py   ABSENT
    gemini.py   ABSENT
    opencode.py ABSENT
    ollama.py   ABSENT

Nobody noticed for codex/cursor because their binaries were absent anyway. The
day a previously-builtin runner becomes a gated plugin -- which is what
happened to `vibe` -- the deploy drops it, silently, and the only symptom is a
runner that no longer exists.

Shipping `plugins/` in the wheel is the other route and is NOT taken here: it
risks squatting a top-level `plugins` name in every installation, which is an
operator decision about packaging rather than an engine one.

The rule this file pins is small and the failure mode it prevents is not: a
binary installed without its plugin must be reported LOUDLY. Silence is the
defect.
"""

from __future__ import annotations

import pytest

from hivepilot.services.agent_plugin_delivery import (
    agents_owing_a_plugin,
    deliver_plugin_for,
    plugin_file_for_agent,
)


class TestWhichAgentsOweAPluginFile:
    @pytest.mark.parametrize(
        "kind", ["codex", "cursor", "gemini", "opencode", "ollama", "vibe", "antigravity"]
    )
    def test_every_gated_agent_cli_names_its_plugin(self, kind):
        """These are exactly the kinds `plugin_installer.AGENT_CLI_PLUGINS`
        excludes from the curated registry. Excluded from one path and absent
        from the other is how they reached nobody."""
        assert plugin_file_for_agent(kind) == f"{kind}.py"

    def test_underscored_kinds_keep_their_module_name(self):
        """`kimi_cli`, not `kimi-cli`: the plugin file is a Python module and
        the fetch path is `plugins/<name>.py`. A hyphen would 404, which the
        current code would report as 'no plugin for this agent'."""
        assert plugin_file_for_agent("kimi_cli") == "kimi_cli.py"
        assert plugin_file_for_agent("qwen_code") == "qwen_code.py"

    def test_claude_owes_nothing_because_it_is_built_in(self):
        """`claude` is a builtin runner, not a plugin. Fetching a file for it
        would 404 and report a failure for something that is working."""
        assert plugin_file_for_agent("claude") is None

    def test_a_tool_that_is_not_an_agent_owes_nothing(self):
        """`gh` and `herdr` are installed through the same guided flow but are
        covered by the curated plugin registry, so this path must not
        double-install them."""
        assert plugin_file_for_agent("gh") is None
        assert plugin_file_for_agent("herdr") is None

    def test_an_unknown_kind_owes_nothing_rather_than_guessing(self):
        """A typo must not become a fetch for `plugins/<typo>.py`."""
        assert plugin_file_for_agent("not-a-real-agent") is None
        assert plugin_file_for_agent("") is None


class TestTheListStaysHonest:
    def test_it_is_derived_from_the_installer_not_retyped(self):
        """Two hand-maintained lists of the same fact eventually disagree, and
        the disagreement would be invisible: a kind added to
        `AGENT_CLI_PLUGINS` but missed here would go back to shipping
        nothing."""
        from hivepilot.services.agent_plugin_delivery import agents_owing_a_plugin
        from hivepilot.services.plugin_installer import AGENT_CLI_PLUGINS

        assert agents_owing_a_plugin() == set(AGENT_CLI_PLUGINS)


class TestDeliveringIt:
    """Placing the file is half; SAYING so when it fails is the other half.

    A binary installed without its plugin is the exact state this task exists
    to remove, and it is invisible from the outside -- the agent's binary is on
    PATH and the runner simply does not exist.
    """

    def test_an_agent_that_owes_a_plugin_gets_one_fetched(self, tmp_path):
        asked = []

        def fake_fetch(name, **kw):
            asked.append(name)
            return tmp_path / f"{name}.py"

        result = deliver_plugin_for("codex", fetch=fake_fetch)

        assert asked == ["codex"]
        assert result["state"] == "placed"

    def test_a_builtin_runner_asks_for_nothing(self, tmp_path):
        """`claude` is builtin. Fetching for it would 404 and report a failure
        for something that works."""
        asked = []

        result = deliver_plugin_for("claude", fetch=lambda n, **kw: asked.append(n))

        assert asked == []
        assert result["state"] == "not_needed"

    def test_a_failed_fetch_is_reported_not_swallowed(self):
        """The whole defect in one line. A silent failure here leaves a box
        with the binary and no runner, and nothing anywhere says so."""

        def explode(name, **kw):
            raise RuntimeError("404 from the source repo")

        result = deliver_plugin_for("cursor", fetch=explode)

        assert result["state"] == "failed"
        assert "cursor" in result["message"]

    def test_the_failure_message_names_a_fix_that_actually_works(self):
        """My first draft told the operator to run
        `hivepilot plugins install <kind>`, and the test passed because it
        asserted a string I had written. That command answers "unknown
        plugin": agent CLIs are excluded from the curated registry, which is
        the very reason this delivery path exists.

        So this asserts the advice is EXECUTABLE, not that it is present."""
        from hivepilot.services.plugin_installer import AGENT_CLI_PLUGINS

        def explode(name, **kw):
            raise RuntimeError("network is down")

        message = deliver_plugin_for("vibe", fetch=explode)["message"]

        assert "vibe" in AGENT_CLI_PLUGINS  # so `plugins install vibe` cannot work
        assert "plugins install" not in message
        assert "agents install vibe" in message

    def test_every_owed_agent_gets_advice_that_would_run(self):
        """Not just vibe. A kind added to AGENT_CLI_PLUGINS later must not
        inherit advice pointing at a command that refuses it."""

        def explode(name, **kw):
            raise RuntimeError("boom")

        for kind in agents_owing_a_plugin():
            message = deliver_plugin_for(kind, fetch=explode)["message"]
            assert "plugins install" not in message, kind
            assert f"agents install {kind}" in message, kind

    def test_delivery_never_raises(self):
        """It runs at the end of an install that has already succeeded.
        Raising here would turn a working binary into a failed command."""
        deliver_plugin_for("codex", fetch=lambda n, **kw: (_ for _ in ()).throw(OSError("disk")))


class TestTheGateStaysShutOnTheOtherPath:
    """`agents install` may fetch these; `plugins install` still may not.

    The installer's own comment is explicit: "two install paths for one thing
    eventually disagree; there is one". Widening the allowlist outright would
    have created exactly the second path it refuses, so the door opens only for
    the caller that passes the flag.
    """

    def test_plugins_install_still_refuses_an_agent_cli(self):
        import pytest as _pytest

        from hivepilot.services.plugin_installer import fetch_plugin

        with _pytest.raises(ValueError, match="unknown plugin"):
            fetch_plugin("codex")

    def test_the_refusal_does_not_advertise_what_it_will_not_install(self):
        """Listing agent CLIs as "available" would send the operator down a
        path that refuses them a second time."""
        import pytest as _pytest

        from hivepilot.services.plugin_installer import fetch_plugin

        with _pytest.raises(ValueError) as exc:
            fetch_plugin("codex")

        assert "cursor" not in str(exc.value)

    def test_the_agent_path_is_the_one_that_opens_it(self):
        """Asserts the network was REACHED, not merely that one message was
        absent.

        My first version caught any exception and checked only that it did not
        say "unknown plugin". The implementation was broken -- it evaluated
        `dict | frozenset` and raised TypeError -- and that message does not
        contain "unknown plugin" either, so the test passed over code that
        could not run. mypy found it; this assertion would not have.
        """
        from unittest.mock import patch

        from hivepilot.services.plugin_installer import fetch_plugin

        with patch("hivepilot.services.plugin_installer.requests.get") as get:
            get.side_effect = RuntimeError("reached the network")
            with pytest.raises(Exception, match="reached the network"):
                fetch_plugin("codex", allow_agent_cli=True)

            # The URL is the point: the name passed validation and the fetch
            # was actually attempted for `plugins/codex.py`.
            assert get.call_count == 1
            assert "plugins/codex.py" in get.call_args.args[0]
