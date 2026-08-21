"""First-party plugins ship inside the wheel, and are found LAST.

The defect this closes: `pip install` never updated a plugin. They lived only
in a top-level `plugins/` (not packaged) and in `$XDG_DATA_HOME/hivepilot/
plugins/` (written by nothing but `plugins install`), so a merged fix to
`herdr.py` ran NINE DAYS stale on the box with `plugins check` reporting
nothing -- there was no in-wheel copy to compare against.

Two things need pinning, and neither is "the files exist":

    the tier is LAST in scan order. First would mean a `pip install` silently
    overrides a plugin an operator deliberately edited in their config repo
    or installed dir. Last makes it a floor;

    the directory is NOT a package. The loader loads by file path; an
    `__init__.py` would additionally make every plugin importable as
    `hivepilot.bundled_plugins.X`, giving two module objects with two sets of
    class identities for the same source -- the trap that had `mypy .`
    reporting 20 phantom errors against `build/lib/`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import BUNDLED_PLUGINS

from hivepilot import plugins as plugins_mod
from hivepilot.services.plugin_installer import AGENT_CLI_PLUGINS, KNOWN_EXAMPLE_PLUGINS


class TestItIsWhereTheResolverSaysItIs:
    def test_conftest_and_the_resolver_agree(self):
        """`BUNDLED_PLUGINS` is computed independently of
        `_bundled_plugins_dir` on purpose -- a test that locates plugins
        through the resolver it is testing cannot notice that resolver
        pointing somewhere wrong. This is where the two are reconciled."""
        assert plugins_mod._bundled_plugins_dir() == BUNDLED_PLUGINS

    def test_it_travels_with_the_package_not_the_repo(self):
        """Anchored to `hivepilot/`, so it survives `base_dir` being `/` --
        which is what the services actually run with. `base_dir/plugins` has
        never resolved to anything in production."""
        import hivepilot

        assert BUNDLED_PLUGINS.parent == Path(hivepilot.__file__).resolve().parent

    def test_it_is_not_a_package(self):
        """No `__init__.py`. Adding one makes every plugin importable under a
        second module name while the loader still loads it by path -- two
        module objects, two class identities, one source file."""
        assert not (BUNDLED_PLUGINS / "__init__.py").exists()


class TestEveryInstallableNameIsBundled:
    """`fetch_plugin` reads the wheel instead of the network for first-party
    names. That is only safe if every name it accepts is actually present --
    otherwise it falls through to a URL under the OLD `plugins/` path, which
    now 404s."""

    @pytest.mark.parametrize("name", sorted(set(KNOWN_EXAMPLE_PLUGINS) | set(AGENT_CLI_PLUGINS)))
    def test_registry_name_has_a_file(self, name):
        assert (BUNDLED_PLUGINS / f"{name}.py").is_file()


class TestInstallingIsOfflineAndExact:
    def test_it_copies_the_running_code_byte_for_byte(self, tmp_path):
        """The whole point: the installed copy and the code this process runs
        can no longer disagree, because one is a byte copy of the other."""
        from hivepilot.services.plugin_installer import fetch_plugin

        written = fetch_plugin("herdr", dest_dir=tmp_path)

        assert written.read_bytes() == (BUNDLED_PLUGINS / "herdr.py").read_bytes()

    def test_it_makes_no_network_call(self, tmp_path, monkeypatch):
        """Pinned at the call, because a fetch that silently reached the
        network would still produce the right file today -- and start failing
        the day the box is offline or the URL moves again."""
        import requests

        from hivepilot.services.plugin_installer import fetch_plugin

        def _boom(*a, **k):  # pragma: no cover - must never run
            raise AssertionError("fetch_plugin reached the network for a bundled plugin")

        monkeypatch.setattr(requests, "get", _boom)

        assert fetch_plugin("herdr", dest_dir=tmp_path).is_file()

    def test_an_explicit_repo_override_still_uses_the_network(self, tmp_path, monkeypatch):
        """A fork or config repo must keep working, and keeps the
        `plugins/<name>.py` layout -- that is the config-repo convention, and
        it did not move."""
        import requests

        from hivepilot.services.plugin_installer import fetch_plugin

        seen: list[str] = []

        def _fake_get(url, **kwargs):
            seen.append(url)
            raise requests.RequestException("offline")

        monkeypatch.setattr(requests, "get", _fake_get)

        with pytest.raises(RuntimeError):
            fetch_plugin("herdr", repo="https://example.test/x", ref="v1", dest_dir=tmp_path)

        assert seen == ["https://example.test/x/v1/plugins/herdr.py"]


@pytest.mark.bundled_plugins
class TestItIsScannedLast:
    """`_no_ambient_bundled_plugins` in conftest hides this tier from tests
    that pinned `base_dir` elsewhere. These opt back in -- they exist to prove
    the tier is real."""

    def test_the_loader_scans_it(self):
        assert BUNDLED_PLUGINS in plugins_mod.plugin_scan_dirs()

    def test_it_is_the_last_entry(self):
        """Order is the design. An earlier position would let a wheel upgrade
        silently replace a plugin the operator edited on purpose."""
        assert plugins_mod.plugin_scan_dirs()[-1] == BUNDLED_PLUGINS

    def test_every_editable_tier_outranks_it(self, tmp_path, monkeypatch):
        """The concrete guarantee for already-deployed hosts: their
        `$XDG_DATA_HOME/plugins/*.py` keep winning, so nothing they run
        changes -- only `plugins check` gains something to compare against."""
        installed = tmp_path / "plugins"
        installed.mkdir()
        monkeypatch.setattr(plugins_mod, "_installed_plugins_dir", lambda: installed)

        dirs = plugins_mod.plugin_scan_dirs()

        assert dirs.index(installed) < dirs.index(BUNDLED_PLUGINS)

    def test_the_master_kill_switch_still_wins(self, monkeypatch):
        """A new tier must not become a way for plugins to load on a host that
        switched them off."""
        monkeypatch.setattr(plugins_mod.settings, "plugins_enabled", False)

        assert plugins_mod.plugin_scan_dirs() == []


class TestTheWheelActuallyShipsThem:
    def test_pyproject_declares_the_glob(self):
        """The files being on disk proves nothing about the artefact. This is
        the declaration that puts them in it -- and `"hivepilot"` must appear
        ONCE in that table: a duplicate TOML key would silently drop
        `prompts/*.md` from the wheel instead of erroring."""
        import pathlib

        import tomllib

        data = tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))
        package_data = data["tool"]["setuptools"]["package-data"]

        assert "bundled_plugins/*.py" in package_data["hivepilot"]
        assert "prompts/*.md" in package_data["hivepilot"]
