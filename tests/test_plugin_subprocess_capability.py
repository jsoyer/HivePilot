"""`plugins/gh.py` and `plugins/rtk.py` declare the `subprocess` capability.

Both plugins genuinely shell out (`subprocess.run` at `plugins/gh.py`'s
`GhRunner.run` and `plugins/rtk.py`'s `RtkRunner.run`), yet before this module
existed neither declared a `capabilities` manifest at all — which per
`docs/PLUGINS.md` means "completely unaffected, regardless of policy". They
spawned child processes while sitting entirely OUTSIDE the capability
admission gate. That exemption is deliberate backward-compat for plugins
shipped before the manifest existed, but for these two it was a knowingly
accepted hole.

What this module pins:

(a) each plugin declares EXACTLY `["subprocess"]` — not more (over-declaring
    forces every operator to allowlist tokens the plugin does not need), not
    less (under-declaring is the bug being fixed);
(b) the BEHAVIOUR CHANGE, deliberately: with the default empty
    `plugins_capability_policy`, each plugin is now DENIED at load and its
    other contributions (runner kind + health check) are atomically rolled
    back. This is not merely tolerated — it is the point of the change, so it
    is asserted directly;
(c) with `subprocess` allowlisted, both load and contribute exactly what they
    contributed before;
(d) a MALFORMED policy value denies rather than widens (fail-closed).

(d) is worth pinning precisely because of a real deployment trap: the policy
env var is decoded as JSON-array OR CSV OR a plain single value, and the
JSON form `HIVEPILOT_PLUGINS_CAPABILITY_POLICY=["subprocess"]` has its quotes
stripped by both shell `source` and systemd's `EnvironmentFile`, yielding the
bare token `[subprocess]`. That is not valid JSON, so it falls through to CSV
parsing and becomes the single nonsense token `"[subprocess]"`, which matches
no declared capability and therefore DENIES. Fail-closed and correct, but
silent — hence the tests below, and the `.env.example`/`docs/PLUGINS.md`
guidance to use the plain `=subprocess` form which survives both.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from types import ModuleType

import pytest

from hivepilot.config import Settings
from hivepilot.plugin_capabilities import (
    PLUGIN_CAPABILITIES,
    PluginCapabilityDeniedError,
    validate_capabilities,
)

REPO_ROOT = Path(__file__).parent.parent

# The exact manifest each plugin is expected to declare. A single source of
# truth so a future token addition has to be made deliberately, in one place.
EXPECTED_CAPABILITIES = ["subprocess"]


def _load_plugin_module(name: str) -> ModuleType:
    """Load `plugins/<name>.py` by file path — the same mechanism
    `hivepilot.plugins._scan_local_plugins` uses (no dependency on `plugins`
    being an importable package on `sys.path`)."""
    path = REPO_ROOT / "plugins" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"hivepilot_plugin_{name}_captest", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _enable(name: str, module: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `register()` take its full contribution path.

    `gh` is PATH-gated (`register()` returns `{}` when the `gh` binary is
    absent), so its `shutil.which` is stubbed rather than skipping the test —
    the declaration under test is a property of the source, not of the host.
    `rtk` is not PATH-gated at registration time.
    """
    from hivepilot.config import settings

    monkeypatch.setattr(settings, f"{name}_enabled", True, raising=False)
    if name == "gh":
        monkeypatch.setattr(module.shutil, "which", lambda _binary: f"/usr/bin/{name}")


def _isolated_plugin_dir(name: str, tmp_path: Path) -> Path:
    """Copy ONE real plugin file into a `tmp_path/plugins/` dir and return
    `tmp_path` for use as `settings.base_dir`.

    Isolation matters here: pointing `base_dir` at the repo root would load
    every bundled plugin, and with an empty policy whichever of `gh`/`rtk`
    happened to be scanned first would raise — so a per-plugin assertion
    could pass for the wrong plugin. Copying exactly one file makes the
    denial attributable.
    """
    pdir = tmp_path / "plugins"
    pdir.mkdir(exist_ok=True)
    shutil.copy(REPO_ROOT / "plugins" / f"{name}.py", pdir / f"{name}.py")
    return tmp_path


@pytest.fixture()
def _restore_runner_map():
    """`RUNNER_MAP` is process-global mutable state — snapshot/restore around
    each test so a real plugin registered from disk never leaks into other
    test modules sharing the pytest session (same pattern as `test_rtk.py`
    and `test_gh.py`)."""
    from hivepilot.registry import RUNNER_MAP

    snapshot = dict(RUNNER_MAP)
    yield
    RUNNER_MAP.clear()
    RUNNER_MAP.update(snapshot)


class TestDeclaredManifest:
    """(a) Each plugin declares exactly `["subprocess"]`."""

    @pytest.mark.parametrize("name", ["gh", "rtk"])
    def test_register_declares_exactly_subprocess(
        self, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = _load_plugin_module(name)
        _enable(name, module, monkeypatch)

        hooks = module.register()

        assert hooks["capabilities"] == EXPECTED_CAPABILITIES

    @pytest.mark.parametrize("name", ["gh", "rtk"])
    def test_declared_tokens_are_in_the_closed_vocabulary(
        self, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A token outside `PLUGIN_CAPABILITIES` would raise
        `PluginCapabilityInvalidError` at load for every operator regardless
        of policy — a hard break, so guard it independently of the exact
        token list above."""
        module = _load_plugin_module(name)
        _enable(name, module, monkeypatch)

        declared = module.register()["capabilities"]

        assert set(declared) <= set(PLUGIN_CAPABILITIES)

    @pytest.mark.parametrize("name", ["gh", "rtk"])
    def test_declaration_is_backed_by_a_real_subprocess_call(self, name: str) -> None:
        """Evidence, not vibes: the declared token must correspond to an
        actual `subprocess.run(` in the plugin's own source. Guards against a
        future refactor dropping the shell-out while leaving a now-overstated
        manifest that operators must still allowlist."""
        source = (REPO_ROOT / "plugins" / f"{name}.py").read_text(encoding="utf-8")

        assert "import subprocess" in source
        assert "subprocess.run(" in source

    @pytest.mark.parametrize("name", ["gh", "rtk"])
    def test_disabled_plugin_declares_nothing(
        self, name: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The enablement flag still short-circuits `register()` to `{}`. A
        plugin the operator turned off must not demand a capability
        allowlist entry to keep the rest of the plugin set loading."""
        from hivepilot.config import settings

        module = _load_plugin_module(name)
        monkeypatch.setattr(settings, f"{name}_enabled", False, raising=False)

        assert module.register() == {}


class TestDeniedUnderDefaultPolicy:
    """(b) The deliberate behaviour change: default empty policy now DENIES
    these two plugins and rolls back their other contributions."""

    @pytest.mark.parametrize("name", ["gh", "rtk"])
    def test_empty_policy_denies_and_rolls_back_contributions(
        self,
        name: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        _restore_runner_map,
    ) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.registry import RUNNER_MAP

        base_dir = _isolated_plugin_dir(name, tmp_path)
        monkeypatch.setattr(plugins_mod.settings, "base_dir", base_dir, raising=False)
        monkeypatch.setattr(plugins_mod.settings, f"{name}_enabled", True, raising=False)
        monkeypatch.setattr(plugins_mod.settings, "plugins_capability_policy", [], raising=False)
        RUNNER_MAP.pop(name, None)  # clean baseline (fixture restores after)

        # UPDATED for #377: the denial is now logged and SKIPPED per plugin
        # rather than raised out of `PluginManager()` construction. The verdict
        # is unchanged — this plugin is still denied and still contributes
        # nothing — so every assertion about the VERDICT below is the original
        # one. Only the delivery mechanism moved.
        pm = plugins_mod.PluginManager()

        reasons = {record.name: reason for record, reason in pm.denied}
        assert name in reasons, f"{name} was not recorded as denied"

        # The denial names the plugin and the offending token, and nothing else.
        assert name in reasons[name]
        assert "subprocess" in reasons[name]

        assert name not in {record.name for record in pm.loaded}

        # Atomic rollback: the runner kind staged BEFORE the capability gate
        # ran must not leak into the live map.
        assert name not in RUNNER_MAP

    @pytest.mark.parametrize("name", ["gh", "rtk"])
    def test_allowlisting_a_different_capability_still_denies(
        self,
        name: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        _restore_runner_map,
    ) -> None:
        """An operator who allowlisted some OTHER token has not allowlisted
        `subprocess` — the gate is per-token, not "any policy at all"."""
        from hivepilot import plugins as plugins_mod
        from hivepilot.registry import RUNNER_MAP

        base_dir = _isolated_plugin_dir(name, tmp_path)
        monkeypatch.setattr(plugins_mod.settings, "base_dir", base_dir, raising=False)
        monkeypatch.setattr(plugins_mod.settings, f"{name}_enabled", True, raising=False)
        monkeypatch.setattr(
            plugins_mod.settings, "plugins_capability_policy", ["network"], raising=False
        )
        RUNNER_MAP.pop(name, None)

        # UPDATED for #377 — see the sibling test above.
        pm = plugins_mod.PluginManager()

        assert name in {record.name for record, _ in pm.denied}
        assert name not in RUNNER_MAP


class TestAllowedWhenSubprocessAllowlisted:
    """(c) With `subprocess` allowlisted, both load and contribute exactly
    what they contributed before the manifest was added."""

    @pytest.mark.parametrize("name", ["gh", "rtk"])
    def test_policy_allows_load_and_runner_registration(
        self,
        name: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        _restore_runner_map,
    ) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.registry import RUNNER_MAP

        if name == "gh" and shutil.which("gh") is None:
            pytest.skip("gh CLI not installed — PATH-gated register() cannot contribute here")

        base_dir = _isolated_plugin_dir(name, tmp_path)
        monkeypatch.setattr(plugins_mod.settings, "base_dir", base_dir, raising=False)
        monkeypatch.setattr(plugins_mod.settings, f"{name}_enabled", True, raising=False)
        monkeypatch.setattr(
            plugins_mod.settings, "plugins_capability_policy", ["subprocess"], raising=False
        )
        RUNNER_MAP.pop(name, None)

        pm = plugins_mod.PluginManager()

        # Unchanged contributions: the runner kind and the health check.
        assert name in RUNNER_MAP
        assert name in pm.health

        record = next(r for r in pm.loaded if r.name == name)
        assert record.contributions["runners"] == [name]
        assert record.contributions["capabilities"] == EXPECTED_CAPABILITIES

    @pytest.mark.parametrize("name", ["gh", "rtk"])
    def test_broader_policy_containing_subprocess_also_allows(
        self,
        name: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        _restore_runner_map,
    ) -> None:
        """A policy is an allow-LIST: extra unrelated tokens are harmless as
        long as `subprocess` is present (the CSV form operators will use)."""
        from hivepilot import plugins as plugins_mod
        from hivepilot.registry import RUNNER_MAP

        if name == "gh" and shutil.which("gh") is None:
            pytest.skip("gh CLI not installed — PATH-gated register() cannot contribute here")

        base_dir = _isolated_plugin_dir(name, tmp_path)
        monkeypatch.setattr(plugins_mod.settings, "base_dir", base_dir, raising=False)
        monkeypatch.setattr(plugins_mod.settings, f"{name}_enabled", True, raising=False)
        monkeypatch.setattr(
            plugins_mod.settings,
            "plugins_capability_policy",
            ["network", "subprocess", "env"],
            raising=False,
        )
        RUNNER_MAP.pop(name, None)

        plugins_mod.PluginManager()

        assert name in RUNNER_MAP


class TestMalformedPolicyFailsClosed:
    """(d) A malformed policy value must DENY, never widen access."""

    # Every form an operator might realistically end up with, and what the
    # `_parse_env_list` validator in `hivepilot/config.py` turns it into.
    @pytest.mark.parametrize(
        ("raw", "expected_tokens", "allows_subprocess"),
        [
            # Recommended plain form — survives shell `source` AND systemd
            # `EnvironmentFile` (no quotes to strip).
            ("subprocess", ["subprocess"], True),
            # CSV form, also quote-free and safe.
            ("subprocess,network", ["subprocess", "network"], True),
            # JSON form WITH quotes intact (only when nothing strips them).
            ('["subprocess"]', ["subprocess"], True),
            # THE TRAP: the JSON form after shell/systemd quote-stripping.
            # Not valid JSON -> falls through to CSV -> one nonsense token.
            ("[subprocess]", ["[subprocess]"], False),
            # Same trap for the multi-token JSON form.
            ("[subprocess,network]", ["[subprocess", "network]"], False),
            # Unset/blank -> fail-closed default.
            ("", [], False),
        ],
    )
    def test_policy_env_parsing_never_widens(
        self,
        raw: str,
        expected_tokens: list[str],
        allows_subprocess: bool,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("HIVEPILOT_PLUGINS_CAPABILITY_POLICY", raw)

        parsed = Settings().plugins_capability_policy
        assert parsed == expected_tokens

        if allows_subprocess:
            assert validate_capabilities("gh", ["subprocess"], frozenset(parsed)) == frozenset(
                {"subprocess"}
            )
        else:
            with pytest.raises(PluginCapabilityDeniedError):
                validate_capabilities("gh", ["subprocess"], frozenset(parsed))

    def test_malformed_token_is_not_validated_against_the_vocabulary(self) -> None:
        """The POLICY side is intentionally not checked against
        `PLUGIN_CAPABILITIES`: an unrecognized allow-entry simply matches no
        declared token. That is why a malformed value can only ever DENY —
        widening requires a genuine token, spelled correctly. Pinning this
        asserts the absence of any warn-and-continue path that could turn a
        typo into an allow.
        """
        garbage = frozenset({"[subprocess]", "SUBPROCESS", "sub process", "subprocess "})

        with pytest.raises(PluginCapabilityDeniedError):
            validate_capabilities("gh", ["subprocess"], garbage)

    @pytest.mark.parametrize("name", ["gh", "rtk"])
    def test_quote_stripped_json_policy_denies_the_real_plugin(
        self,
        name: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        _restore_runner_map,
    ) -> None:
        """End-to-end version of the trap: the mangled `[subprocess]` token
        as an operator's policy leaves the real plugin denied."""
        from hivepilot import plugins as plugins_mod
        from hivepilot.registry import RUNNER_MAP

        base_dir = _isolated_plugin_dir(name, tmp_path)
        monkeypatch.setattr(plugins_mod.settings, "base_dir", base_dir, raising=False)
        monkeypatch.setattr(plugins_mod.settings, f"{name}_enabled", True, raising=False)
        monkeypatch.setattr(
            plugins_mod.settings, "plugins_capability_policy", ["[subprocess]"], raising=False
        )
        RUNNER_MAP.pop(name, None)

        # UPDATED for #377: skipped, not raised. The two direct
        # `validate_capabilities` tests above still assert `pytest.raises` —
        # the GATE still raises; what changed is that `_load_into` no longer
        # lets that escape `PluginManager()`.
        pm = plugins_mod.PluginManager()

        assert name in {record.name for record, _ in pm.denied}

        assert name not in RUNNER_MAP
