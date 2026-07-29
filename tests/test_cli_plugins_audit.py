"""Tests for `hivepilot plugins audit` (Phase 26b) — the read-only static
security scan of local-file plugin SOURCE TEXT.

Covers: enumerates EVERY root the loader can load from (`plugin_scan_dirs`:
`base_dir/plugins`, the config repo clone's `plugins/`, the managed
`plugins install` destination, then `plugins_extra_dirs`), never
imports/execs a plugin, flags under-declared capabilities via
`hivepilot.plugin_capabilities.audit_plugin_source`, and `--strict` exits 1
on an under-declaration OR on a scan that examined zero files.

Every test pins all four roots inside `tmp_path` (see the `audit_env`
fixture) — the real `~/.local/share/hivepilot/plugins` and the real config
repo clone are never touched, and a developer who happens to have installed
plugins cannot change the outcome.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hivepilot.cli import app

UNDER_DECLARING_SOURCE = "import subprocess\n\n\ndef register():\n    return {}\n"


@dataclass
class AuditEnv:
    """The four directories `plugin_scan_dirs` can return, all under `tmp_path`.

    None of them exist on disk until a test calls `write_plugin`, so the
    default state is "no plugins anywhere" regardless of the host.
    """

    base_plugins: Path
    config_repo_plugins: Path
    installed_plugins: Path
    extra: Path

    @staticmethod
    def write_plugin(pdir: Path, name: str, source: str = UNDER_DECLARING_SOURCE) -> Path:
        pdir.mkdir(parents=True, exist_ok=True)
        target = pdir / f"{name}.py"
        target.write_text(source, encoding="utf-8")
        return target


@pytest.fixture
def audit_env(tmp_path, monkeypatch) -> AuditEnv:
    """Isolate every root `plugin_scan_dirs` consults.

    `config_repo` is left SET (with `_config_dir` redirected into `tmp_path`)
    so the config-repo root is reachable; it is still existence-gated, so it
    contributes nothing until a test writes a file into it.
    """
    from hivepilot.config import settings
    from hivepilot.services import config_service as config_service_mod

    base_dir = tmp_path / "base"
    base_dir.mkdir()
    clone_dir = tmp_path / "config-clone"
    xdg_data = tmp_path / "xdg-data"

    monkeypatch.setattr(settings, "base_dir", base_dir, raising=False)
    monkeypatch.setattr(settings, "plugins_enabled", True, raising=False)
    monkeypatch.setattr(settings, "plugins_disabled", [], raising=False)
    monkeypatch.setattr(settings, "plugins_extra_dirs", [], raising=False)
    monkeypatch.setattr(settings, "config_repo", "https://example.com/cfg.git", raising=False)
    monkeypatch.setattr(settings, "config_repo_load_plugins", True, raising=False)
    monkeypatch.setattr(config_service_mod, "_config_dir", lambda: clone_dir, raising=False)
    # `Settings.xdg_data_home` reads $XDG_DATA_HOME at call time, so setenv is
    # both sufficient and production-faithful (no property monkeypatching).
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))

    return AuditEnv(
        base_plugins=base_dir / "plugins",
        config_repo_plugins=clone_dir / "plugins",
        installed_plugins=xdg_data / "hivepilot" / "plugins",
        extra=tmp_path / "extra",
    )


# ---------------------------------------------------------------------------
# Baseline behaviour (pre-existing tests, re-pointed at the isolated fixture
# so a developer's real installed-plugins dir can no longer influence them).
# ---------------------------------------------------------------------------


def test_audit_exits_zero_with_no_plugin_dir(audit_env) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "audit"])

    assert result.exit_code == 0, result.output


def test_audit_flags_under_declared_subprocess_usage(audit_env) -> None:
    audit_env.write_plugin(audit_env.base_plugins, "shelly")

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "audit"])

    assert result.exit_code == 0, result.output
    assert "shelly" in result.output
    assert "subprocess" in result.output


def test_audit_does_not_flag_declared_capability(audit_env) -> None:
    audit_env.write_plugin(
        audit_env.base_plugins,
        "honest",
        "import subprocess\n\n\ndef register():\n    return {'capabilities': ['subprocess']}\n",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "audit", "--strict"])

    assert result.exit_code == 0, result.output


def test_audit_strict_exits_nonzero_on_under_declaration(audit_env) -> None:
    audit_env.write_plugin(audit_env.base_plugins, "shelly")

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "audit", "--strict"])

    assert result.exit_code == 1, result.output


def test_audit_default_non_strict_exits_zero_despite_under_declaration(audit_env) -> None:
    audit_env.write_plugin(audit_env.base_plugins, "shelly")

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "audit"])

    assert result.exit_code == 0, result.output


def test_audit_escapes_rich_markup_in_declared_capabilities(audit_env) -> None:
    """`plugins audit` is the tool an operator uses to VET an untrusted
    plugin's source before enabling it — a malicious author embedding Rich
    markup inside a `"capabilities": [...]` string literal (picked up
    verbatim by `_extract_declared_capabilities`, which is NOT filtered to
    the closed vocabulary) must not be able to spoof the rendered report
    (e.g. hiding an under-declared warning, or faking a clean styled
    label). The literal markup text must survive escaped, never
    interpreted as a Rich style tag."""
    audit_env.write_plugin(
        audit_env.base_plugins,
        "spoofer",
        "def register():\n    return {'capabilities': ['[red]FAKE-CLEAN[/red]']}\n",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "audit"])

    assert result.exit_code == 0, result.output
    # The injected markup must render as literal, escaped text — the "[" must
    # survive (rich.markup.escape turns "[" into "\[") rather than being
    # consumed by Rich's Table renderer as a real `[red]...[/red]` style tag.
    assert "FAKE-CLEAN" in result.output
    assert "[red]" in result.output or "\\[red]" in result.output


def test_audit_never_imports_plugin_module(audit_env) -> None:
    """A plugin whose module body raises on import (outside `register()`)
    must not break the audit — pure `ast` parsing never executes it."""
    audit_env.write_plugin(
        audit_env.base_plugins,
        "exploding",
        "raise RuntimeError('should never be imported by plugins audit')\n\n\n"
        "def register():\n    return {}\n",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "audit"])

    assert result.exit_code == 0, result.output
    assert "exploding" in result.output


# ---------------------------------------------------------------------------
# The live bug: the auditor scanned a strictly SMALLER set of roots than the
# loader loads from, and reported the shortfall as "nothing to audit".
# ---------------------------------------------------------------------------


class TestAuditCoversEveryLoaderRoot:
    """Observed on a production host: six plugins in
    `$XDG_DATA_HOME/hivepilot/plugins` (exactly where `hivepilot plugins
    install` writes them) and `hivepilot plugins audit` printed "No plugin
    source files found to audit."

    `_collect_plugin_source_files` hand-rolled `(base_dir/plugins,
    *plugins_extra_dirs)` while the loader iterated `plugin_scan_dirs`,
    which had since grown two more roots.
    """

    def test_plugin_in_installed_plugins_dir_is_audited(self, audit_env) -> None:
        """THE live bug. A plugin HivePilot's own installer placed in the
        managed dir must be vettable by HivePilot's own auditor."""
        audit_env.write_plugin(audit_env.installed_plugins, "rtk")

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "audit"])

        assert result.exit_code == 0, result.output
        assert "rtk" in result.output
        assert "No plugin source files found" not in result.output

    def test_plugin_in_config_repo_plugins_dir_is_audited(self, audit_env) -> None:
        """The config repo clone's own `plugins/` dir auto-loads (see
        `plugins._config_repo_plugins_dir`), so it is attack surface and
        must be audited too."""
        audit_env.write_plugin(audit_env.config_repo_plugins, "vendored_thing")

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "audit"])

        assert result.exit_code == 0, result.output
        assert "vendored_thing" in result.output

    def test_installed_dir_plugin_under_declaration_fails_strict(self, audit_env) -> None:
        """The CI consequence of the bug: `--strict` passed green because
        the under-declaring plugin lived in a directory the scanner never
        opened."""
        audit_env.write_plugin(audit_env.installed_plugins, "sneaky")

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "audit", "--strict"])

        assert result.exit_code == 1, result.output
        assert "sneaky" in result.output

    def test_extra_dirs_still_audited(self, audit_env, monkeypatch) -> None:
        """Regression guard on the root the OLD hand-rolled list did cover —
        deriving from `plugin_scan_dirs` must not drop it."""
        from hivepilot.config import settings

        audit_env.write_plugin(audit_env.extra, "from_extra")
        monkeypatch.setattr(settings, "plugins_extra_dirs", [audit_env.extra], raising=False)

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "audit"])

        assert result.exit_code == 0, result.output
        assert "from_extra" in result.output


# ---------------------------------------------------------------------------
# Per-directory file filter — mirrors `_scan_plugin_dir`, EXCEPT that
# `plugins_disabled` is deliberately not honoured.
# ---------------------------------------------------------------------------


class TestAuditFileFilter:
    def test_dedup_across_roots_is_first_directory_wins(self, audit_env, monkeypatch) -> None:
        """Same stem in two roots -> audited exactly once, and it must be the
        copy the LOADER would actually load. Auditing the shadowed copy
        instead vets source that never runs — a false clean bill of health
        on the very file the operator cares about.

        Deliberately pairs the config-repo root (2nd in `plugin_scan_dirs`
        order) against `plugins_extra_dirs` (last), because that ordering
        only exists once the roots come from `plugin_scan_dirs`: the old
        hand-rolled list omitted the config-repo dir entirely and therefore
        audited the WRONG copy.
        """
        from hivepilot.config import settings

        audit_env.write_plugin(
            audit_env.config_repo_plugins,
            "twin",
            "def register():\n    return {'capabilities': ['WINNER-CONFIGREPO']}\n",
        )
        audit_env.write_plugin(
            audit_env.extra,
            "twin",
            "def register():\n    return {'capabilities': ['LOSER-EXTRA']}\n",
        )
        monkeypatch.setattr(settings, "plugins_extra_dirs", [audit_env.extra], raising=False)

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "audit"])

        assert result.exit_code == 0, result.output
        assert "WINNER-CONFIGREPO" in result.output
        assert "LOSER-EXTRA" not in result.output
        assert result.output.count("twin") == 1, result.output

    def test_dedup_base_dir_still_beats_installed_dir(self, audit_env) -> None:
        """Non-regression on the precedence that already held: `base_dir/
        plugins` outranks the managed installed dir, so a shipped plugin
        shadowed by an installed one of the same stem is audited as the
        shipped copy."""
        audit_env.write_plugin(
            audit_env.base_plugins,
            "twin",
            "def register():\n    return {'capabilities': ['WINNER-BASE']}\n",
        )
        audit_env.write_plugin(
            audit_env.installed_plugins,
            "twin",
            "def register():\n    return {'capabilities': ['LOSER-INSTALLED']}\n",
        )

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "audit"])

        assert result.exit_code == 0, result.output
        assert "WINNER-BASE" in result.output
        assert "LOSER-INSTALLED" not in result.output
        assert result.output.count("twin") == 1, result.output

    def test_disabled_plugin_is_still_audited(self, audit_env, monkeypatch) -> None:
        """`plugins_disabled` is a LOADER gate, not an audit gate: auditing
        is precisely what you do before deciding to re-enable something."""
        from hivepilot.config import settings

        audit_env.write_plugin(audit_env.installed_plugins, "quarantined")
        monkeypatch.setattr(settings, "plugins_disabled", ["quarantined"], raising=False)

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "audit"])

        assert result.exit_code == 0, result.output
        assert "quarantined" in result.output

    def test_underscore_prefixed_files_are_skipped(self, audit_env) -> None:
        """`_helper.py` is not a plugin — the loader never treats it as one,
        so neither does the auditor."""
        audit_env.write_plugin(audit_env.installed_plugins, "_private")
        audit_env.write_plugin(audit_env.installed_plugins, "public")

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "audit"])

        assert result.exit_code == 0, result.output
        assert "public" in result.output
        assert "_private" not in result.output

    def test_missing_root_directories_are_silently_skipped(self, audit_env, monkeypatch) -> None:
        """A configured-but-absent extra dir must not raise; the real roots
        are still scanned."""
        from hivepilot.config import settings

        audit_env.write_plugin(audit_env.installed_plugins, "survivor")
        monkeypatch.setattr(
            settings,
            "plugins_extra_dirs",
            [audit_env.extra / "does-not-exist"],
            raising=False,
        )

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "audit"])

        assert result.exit_code == 0, result.output
        assert "survivor" in result.output

    def test_source_is_only_ever_read_never_executed(self, audit_env, monkeypatch) -> None:
        """Hard pin on the auditor's core safety property: vetting untrusted
        source must not run it. Fails loudly if any import machinery is
        reached for a plugin file."""
        import importlib.util

        audit_env.write_plugin(audit_env.installed_plugins, "hostile")

        def _explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("plugins audit must never import a plugin module")

        monkeypatch.setattr(importlib.util, "spec_from_file_location", _explode)

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "audit"])

        assert result.exit_code == 0, result.output
        assert "hostile" in result.output


# ---------------------------------------------------------------------------
# Zero files is an UNSUBSTANTIATED verdict, not a clean one.
# ---------------------------------------------------------------------------


class TestZeroFilesIsNotACleanBillOfHealth:
    def test_zero_files_names_the_directories_searched(self, audit_env) -> None:
        """The failure mode that let this bug survive was silence: "No
        plugin source files found" reads as "nothing wrong". Naming the
        roots makes "found nothing" distinguishable from "looked in the
        wrong place"."""
        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "audit"])

        assert result.exit_code == 0, result.output
        assert "Searched:" in result.output
        assert str(audit_env.base_plugins) in result.output.replace("\n", "")

    def test_zero_files_under_strict_exits_nonzero(self, audit_env) -> None:
        """`--strict`'s contract is "fail CI on any under-declaration". A
        scan that examined zero files has not established that there are
        none — it has established nothing. A CI job that goes green because
        the scanner looked in the wrong place is the exact failure observed
        in production."""
        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "audit", "--strict"])

        assert result.exit_code == 1, result.output
        assert "Searched:" in result.output

    def test_zero_files_without_strict_still_exits_zero(self, audit_env) -> None:
        """Non-strict stays advisory: an operator with genuinely no local
        plugins must not get a non-zero exit from an informational command
        (the documented default contract)."""
        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "audit"])

        assert result.exit_code == 0, result.output

    def test_master_kill_switch_does_not_blank_the_audit(self, audit_env, monkeypatch) -> None:
        """`plugins_enabled=False` blanks the LOADER's roots. If the auditor
        inherited that it would report "nothing to audit" on a host whose
        disk is full of un-vetted plugin source — the same fail-open, one
        setting away."""
        from hivepilot import plugins as plugins_mod
        from hivepilot.config import settings

        audit_env.write_plugin(audit_env.installed_plugins, "dormant")
        monkeypatch.setattr(settings, "plugins_enabled", False, raising=False)

        assert plugins_mod.plugin_scan_dirs() == [], "loader roots should be blanked"

        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "audit"])

        assert result.exit_code == 0, result.output
        assert "dormant" in result.output


# ---------------------------------------------------------------------------
# The divergence guard: the two lists must be the SAME list, not two lists
# that happen to agree today.
# ---------------------------------------------------------------------------


class TestAuditRootsCannotDivergeFromLoader:
    """The defect was never a wrong path — it was a SECOND list. These tests
    fail if anyone reintroduces one, even a currently-correct one."""

    def test_audit_roots_delegate_to_plugin_scan_dirs(self, tmp_path, monkeypatch) -> None:
        """Behavioural proof of derivation: replace the loader's root
        function with a sentinel and the auditor must follow it somewhere
        no hand-rolled list could ever point. Equality of two independently
        computed lists would NOT prove this."""
        from hivepilot import plugins as plugins_mod
        from hivepilot.cli import _collect_plugin_source_files, _plugin_audit_roots

        sentinel = tmp_path / "sentinel-root"
        AuditEnv.write_plugin(sentinel, "only_reachable_via_delegation")
        monkeypatch.setattr(
            plugins_mod, "plugin_scan_dirs", lambda *a, **kw: [sentinel], raising=True
        )

        assert _plugin_audit_roots() == [sentinel]
        assert [stem for stem, _ in _collect_plugin_source_files()] == [
            "only_reachable_via_delegation"
        ]

    def test_audit_roots_equal_loader_roots_across_configurations(
        self, audit_env, monkeypatch
    ) -> None:
        """Every root permutation: presence/absence of the config-repo dir,
        the managed installed dir, and extra dirs (including the
        already-listed dedup case) must give byte-identical lists."""
        from hivepilot import plugins as plugins_mod
        from hivepilot.cli import _plugin_audit_roots
        from hivepilot.config import settings

        for make_config_repo in (False, True):
            for make_installed in (False, True):
                for extra_dirs in ([], [audit_env.extra], [audit_env.installed_plugins]):
                    if make_config_repo:
                        audit_env.config_repo_plugins.mkdir(parents=True, exist_ok=True)
                    if make_installed:
                        audit_env.installed_plugins.mkdir(parents=True, exist_ok=True)
                    monkeypatch.setattr(settings, "plugins_extra_dirs", extra_dirs, raising=False)

                    assert _plugin_audit_roots() == plugins_mod.plugin_scan_dirs(
                        respect_enabled_gate=False
                    ), (make_config_repo, make_installed, extra_dirs)

    def test_audit_root_helper_hand_rolls_nothing(self) -> None:
        """Structural guard. `_plugin_audit_roots` must contain exactly one
        call, to `plugin_scan_dirs`, and neither it nor
        `_collect_plugin_source_files` may name any root ingredient
        (`base_dir`, `plugins_extra_dirs`, ...) directly — naming one is how
        a second list starts."""
        from hivepilot.cli import _collect_plugin_source_files, _plugin_audit_roots

        tree = ast.parse(inspect.getsource(_plugin_audit_roots))
        called = [
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        assert called == ["plugin_scan_dirs"], called

        for fn in (_plugin_audit_roots, _collect_plugin_source_files):
            fn_node = ast.parse(inspect.getsource(fn)).body[0]
            assert isinstance(fn_node, ast.FunctionDef)
            statements: list[ast.stmt] = fn_node.body
            # Drop the docstring node: prose legitimately DISCUSSES these
            # names (it explains why they must not be re-derived).
            # `ast.unparse` then also drops comments, so only real code is
            # inspected.
            first = statements[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                statements = statements[1:]
            code = "\n".join(ast.unparse(node) for node in statements)

            for forbidden in ("base_dir", "plugins_extra_dirs", "xdg_data_home", "config_repo"):
                assert forbidden not in code, f"{fn.__name__} re-derives roots via {forbidden}"
