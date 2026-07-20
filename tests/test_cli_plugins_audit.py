"""Tests for `hivepilot plugins audit` (Phase 26b) — the read-only static
security scan of local-file plugin SOURCE TEXT.

Covers: enumerates `plugins/*.py` (+ `plugins_extra_dirs`), never
imports/execs a plugin, flags under-declared capabilities via
`hivepilot.plugin_capabilities.audit_plugin_source`, and `--strict` exits 1
only when an under-declaration is found.
"""

from __future__ import annotations

from typer.testing import CliRunner

from hivepilot.cli import app


def test_audit_exits_zero_with_no_plugin_dir(tmp_path, monkeypatch) -> None:
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "base_dir", tmp_path, raising=False)
    monkeypatch.setattr(settings, "plugins_extra_dirs", [], raising=False)

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "audit"])

    assert result.exit_code == 0, result.output


def test_audit_flags_under_declared_subprocess_usage(tmp_path, monkeypatch) -> None:
    from hivepilot.config import settings

    pdir = tmp_path / "plugins"
    pdir.mkdir()
    (pdir / "shelly.py").write_text(
        "import subprocess\n\n\ndef register():\n    return {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "base_dir", tmp_path, raising=False)
    monkeypatch.setattr(settings, "plugins_extra_dirs", [], raising=False)

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "audit"])

    assert result.exit_code == 0, result.output
    assert "shelly" in result.output
    assert "subprocess" in result.output


def test_audit_does_not_flag_declared_capability(tmp_path, monkeypatch) -> None:
    from hivepilot.config import settings

    pdir = tmp_path / "plugins"
    pdir.mkdir()
    (pdir / "honest.py").write_text(
        "import subprocess\n\n\ndef register():\n    return {'capabilities': ['subprocess']}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "base_dir", tmp_path, raising=False)
    monkeypatch.setattr(settings, "plugins_extra_dirs", [], raising=False)

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "audit", "--strict"])

    assert result.exit_code == 0, result.output


def test_audit_strict_exits_nonzero_on_under_declaration(tmp_path, monkeypatch) -> None:
    from hivepilot.config import settings

    pdir = tmp_path / "plugins"
    pdir.mkdir()
    (pdir / "shelly.py").write_text(
        "import subprocess\n\n\ndef register():\n    return {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "base_dir", tmp_path, raising=False)
    monkeypatch.setattr(settings, "plugins_extra_dirs", [], raising=False)

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "audit", "--strict"])

    assert result.exit_code == 1, result.output


def test_audit_default_non_strict_exits_zero_despite_under_declaration(
    tmp_path, monkeypatch
) -> None:
    from hivepilot.config import settings

    pdir = tmp_path / "plugins"
    pdir.mkdir()
    (pdir / "shelly.py").write_text(
        "import subprocess\n\n\ndef register():\n    return {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "base_dir", tmp_path, raising=False)
    monkeypatch.setattr(settings, "plugins_extra_dirs", [], raising=False)

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "audit"])

    assert result.exit_code == 0, result.output


def test_audit_escapes_rich_markup_in_declared_capabilities(tmp_path, monkeypatch) -> None:
    """`plugins audit` is the tool an operator uses to VET an untrusted
    plugin's source before enabling it — a malicious author embedding Rich
    markup inside a `"capabilities": [...]` string literal (picked up
    verbatim by `_extract_declared_capabilities`, which is NOT filtered to
    the closed vocabulary) must not be able to spoof the rendered report
    (e.g. hiding an under-declared warning, or faking a clean styled
    label). The literal markup text must survive escaped, never
    interpreted as a Rich style tag."""
    from hivepilot.config import settings

    pdir = tmp_path / "plugins"
    pdir.mkdir()
    (pdir / "spoofer.py").write_text(
        "def register():\n    return {'capabilities': ['[red]FAKE-CLEAN[/red]']}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "base_dir", tmp_path, raising=False)
    monkeypatch.setattr(settings, "plugins_extra_dirs", [], raising=False)

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "audit"])

    assert result.exit_code == 0, result.output
    # The injected markup must render as literal, escaped text — the "[" must
    # survive (rich.markup.escape turns "[" into "\[") rather than being
    # consumed by Rich's Table renderer as a real `[red]...[/red]` style tag.
    assert "FAKE-CLEAN" in result.output
    assert "[red]" in result.output or "\\[red]" in result.output


def test_audit_never_imports_plugin_module(tmp_path, monkeypatch) -> None:
    """A plugin whose module body raises on import (outside `register()`)
    must not break the audit — pure `ast` parsing never executes it."""
    from hivepilot.config import settings

    pdir = tmp_path / "plugins"
    pdir.mkdir()
    (pdir / "exploding.py").write_text(
        "raise RuntimeError('should never be imported by plugins audit')\n\n\n"
        "def register():\n    return {}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "base_dir", tmp_path, raising=False)
    monkeypatch.setattr(settings, "plugins_extra_dirs", [], raising=False)

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "audit"])

    assert result.exit_code == 0, result.output
    assert "exploding" in result.output
