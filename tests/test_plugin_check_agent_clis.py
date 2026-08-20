"""`plugins check` could not judge the plugins `agents install` now delivers.

#561 made agent CLI plugins deployable: `agents install codex` places
`plugins/codex.py`. Measured on the box straight afterwards, `plugins check`
then said:

    codex    unknown   the source could not be read; this is not a verdict
    cursor   unknown   the source could not be read; this is not a verdict

Not a lie — `UNKNOWN` is deliberately excluded from `needs_action`, so it never
pretends to a verdict it does not have. But it is a hole exactly where this
codebase keeps being bitten: `herdr.py` ran NINE DAYS stale on the box, a
merged fix inert the whole time, and nothing said so. Shipping a deployment
path without its staleness check reproduces the very failure the deployment
path was written to end.

The cause is one missing argument. `plugins check` fetches into a scratch
directory to compare, and `fetch_plugin` rejects agent CLI names unless told
otherwise — so `source_text` came back None and every such plugin scored
`unknown`.

Passing the flag here does NOT reopen the second install path the installer
refuses. `check` installs nothing: it reads into a temp dir and throws it away.
The rule is "one install path per thing", and comparing is not installing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.services import plugin_installer


class TestCheckCanReadWhatInstallDelivers:
    # The flag's own behaviour -- that it lets the fetch reach
    # `plugins/codex.py` -- is pinned in
    # tests/test_agent_plugin_delivery.py::TestTheGateStaysShutOnTheOtherPath.
    # Repeating it here would be a second assertion of one fact, which is how
    # two tests of the same thing end up disagreeing.

    def test_without_the_flag_it_still_refuses(self):
        """`plugins install codex` must keep answering "unknown plugin" —
        two install paths for one thing eventually disagree. Only the read
        path opens the door."""
        with pytest.raises(ValueError, match="unknown plugin"):
            plugin_installer.fetch_plugin("codex", dest_dir=Path("/tmp"))


class TestTheCliPassesIt:
    def test_plugins_check_asks_for_agent_clis_too(self, tmp_path, monkeypatch):
        """Pinned at the CALL, because the failure was invisible in output:
        `unknown` is a legitimate answer for an offline box, so the table
        looked plausible either way. Only the argument distinguishes them."""
        from unittest.mock import patch

        from typer.testing import CliRunner

        from hivepilot.cli import app

        plugin_dir = tmp_path / "plugins"
        plugin_dir.mkdir()
        (plugin_dir / "codex.py").write_text("# installed copy\n", encoding="utf-8")

        seen: list[dict] = []

        def fake_fetch(name, **kwargs):
            seen.append({"name": name, **kwargs})
            raise RuntimeError("offline")

        with (
            patch.object(plugin_installer, "installed_plugins_dir", return_value=plugin_dir),
            patch.object(plugin_installer, "fetch_plugin", side_effect=fake_fetch),
        ):
            CliRunner().invoke(app, ["plugins", "check"])

        codex_calls = [c for c in seen if c["name"] == "codex"]
        assert codex_calls, "check never looked at the installed codex.py"
        assert codex_calls[0].get("allow_agent_cli") is True
