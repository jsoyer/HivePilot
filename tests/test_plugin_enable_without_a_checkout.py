"""Enabling a plugin on a pip-installed box placed nothing, and said nothing.

`_place_plugin_file` copies from `settings.base_dir / "plugins" / <file>` --
a SOURCE CHECKOUT. The production box installs HivePilot from a wheel, and
`plugins/*.py` does not ship in it, so that directory does not exist there.
The copy silently returns None, the flag is written anyway, and the operator
gets an enabled plugin with no file behind it. That is what forced `vibe.py`
to be hand-copied onto the box.

Its own docstring already names the defect. What was missing is that the fix
was already in the codebase: `plugin_installer.fetch_plugin` pulls
`plugins/<name>.py` from `settings.plugins_source_repo` at
`settings.plugins_source_ref` -- the same vetted path `plugins install` uses,
with its own trust boundary (only curated names, bounded body, no dynamic
URL).

So: local checkout first (a developer's edits must win over a published file),
then the fetch. And when both fail, return None so the caller reports it --
never a flag written over nothing.

Two packaging routes were tried and REJECTED before this, both measured on a
built wheel rather than assumed:

- `[tool.setuptools.package-dir]` mapping alone -> 0 files in the wheel;
- adding `plugins*` to `packages.find` -> ships them, but squats a top-level
  `plugins` module in every site-packages on the machine.
"""

from __future__ import annotations

import pytest

from hivepilot.services import plugin_enable


@pytest.fixture
def no_checkout(monkeypatch, tmp_path):
    """A box installed from a wheel: no `<base_dir>/plugins` directory."""
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "base_dir", tmp_path, raising=False)
    return tmp_path


class TestItFallsBackToTheFetchThatAlreadyExists:
    def test_a_missing_local_source_is_fetched(self, no_checkout, monkeypatch, tmp_path):
        """The defect in one assertion: without this, enable placed nothing."""
        fetched: dict = {}
        dest = tmp_path / "installed" / "vibe.py"

        def _fake_fetch(name, **kwargs):
            fetched["name"] = name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("# fetched\n")
            return dest

        from hivepilot.services import plugin_installer

        monkeypatch.setattr(plugin_installer, "fetch_plugin", _fake_fetch)

        result = plugin_enable._place_plugin_file("vibe")

        assert result == dest
        assert fetched["name"] == "vibe"

    def test_a_hyphenated_kind_fetches_its_module_name(self, no_checkout, monkeypatch, tmp_path):
        """`qwen-code` is the runner kind; `qwen_code.py` is the file. Fetching
        the kind verbatim would 404 against the source repo."""
        fetched: dict = {}

        def _fake_fetch(name, **kwargs):
            fetched["name"] = name
            p = tmp_path / "qwen_code.py"
            p.write_text("# fetched\n")
            return p

        from hivepilot.services import plugin_installer

        monkeypatch.setattr(plugin_installer, "fetch_plugin", _fake_fetch)

        plugin_enable._place_plugin_file("qwen-code")

        assert fetched["name"] == "qwen_code"


class TestTheLocalCheckoutStillWins:
    def test_a_present_local_file_is_used_without_fetching(self, monkeypatch, tmp_path):
        """A developer editing plugins/vibe.py must see THEIR file installed,
        not the published one -- otherwise local iteration is impossible."""
        from hivepilot.config import settings
        from hivepilot.services import plugin_installer

        source_dir = tmp_path / "plugins"
        source_dir.mkdir(parents=True)
        (source_dir / "vibe.py").write_text("# local edit\n")
        monkeypatch.setattr(settings, "base_dir", tmp_path, raising=False)
        monkeypatch.setattr(
            plugin_installer,
            "fetch_plugin",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")),
        )
        monkeypatch.setattr(
            plugin_installer, "installed_plugins_dir", lambda: tmp_path / "installed"
        )

        result = plugin_enable._place_plugin_file("vibe")

        assert result is not None
        assert result.read_text() == "# local edit\n"


class TestAFailedFetchIsNotSilent:
    def test_an_unfetchable_plugin_returns_none(self, no_checkout, monkeypatch):
        """`fetch_plugin` raises for an unknown name and on network failure.
        Returning None lets `enable` report it; swallowing it into a written
        flag is the exact silence this fixes."""
        from hivepilot.services import plugin_installer

        monkeypatch.setattr(
            plugin_installer,
            "fetch_plugin",
            lambda *a, **k: (_ for _ in ()).throw(ValueError("unknown plugin")),
        )

        assert plugin_enable._place_plugin_file("no-such-plugin") is None

    def test_a_network_error_returns_none_rather_than_propagating(self, no_checkout, monkeypatch):
        from hivepilot.services import plugin_installer

        monkeypatch.setattr(
            plugin_installer,
            "fetch_plugin",
            lambda *a, **k: (_ for _ in ()).throw(OSError("connection refused")),
        )

        assert plugin_enable._place_plugin_file("vibe") is None
