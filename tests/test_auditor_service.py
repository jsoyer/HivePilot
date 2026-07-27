"""Tests for auditor_service — Henri, the external auditor (Mistral/vibe)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hivepilot.models import ProjectConfig
from hivepilot.services import auditor_service


@pytest.fixture
def _vault(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # point the vault at a real dir so _write is exercised (dry_run keeps it a no-op)
    monkeypatch.setattr(auditor_service.settings, "obsidian_vault", tmp_path, raising=False)
    return tmp_path


def _registry(output: str) -> MagicMock:
    reg = MagicMock()
    reg.capture_definition.return_value = output
    return reg


def test_observe_runs_henri_via_vibe(_vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auditor_service.state_service,
        "list_recent_interactions",
        lambda **k: [{"actor": "Aliénor", "target": "Jules", "summary": "plan set"}],
    )
    reg = _registry("Observation: clean hand-offs.")
    out = auditor_service.observe(
        project=ProjectConfig(path=_vault), run_id=5, registry=reg, dry_run=True
    )
    assert "clean hand-offs" in out
    reg.capture_definition.assert_called_once()
    rdef = reg.capture_definition.call_args.args[0]
    assert rdef.kind == "vibe"  # Henri runs on Mistral via the vibe runner


def test_observe_passes_interactions_as_context(_vault: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        auditor_service.state_service,
        "list_recent_interactions",
        lambda **k: [{"actor": "Blaise", "target": "Hugo", "summary": "arch ready"}],
    )
    reg = _registry("ok")
    auditor_service.observe(
        project=ProjectConfig(path=_vault), run_id=1, registry=reg, dry_run=True
    )
    payload = reg.capture_definition.call_args.args[1]
    assert "Blaise" in payload.metadata["prior_context"]


def test_audit_proposes_and_returns_text(_vault: Path, monkeypatch) -> None:
    monkeypatch.setattr(auditor_service.state_service, "list_recent_interactions", lambda **k: [])
    reg = _registry("Proposal: tighten ciso.md.")
    out = auditor_service.audit(project=ProjectConfig(path=_vault), registry=reg, dry_run=True)
    assert "Proposal" in out
    rdef = reg.capture_definition.call_args.args[0]
    assert rdef.kind == "vibe"


# ---------------------------------------------------------------------------
# Bug 2 (run 243, live incident): "Prompt file not found:
# /opt/hivepilot/venv/lib/python3.14/site-packages/prompts/agents/auditor.md"
#
# The OLD `AUDITOR_PROMPT` constant was a hardcoded
# `Path(__file__).resolve().parent.parent.parent / "prompts" / "agents" /
# "auditor.md"` -- correct in a source checkout, but `.parent.parent.parent`
# from a pip-installed `site-packages/hivepilot/services/auditor_service.py`
# lands at `site-packages/` (one level ABOVE the `hivepilot` package), not
# the repo root, so the file is never found. Every OTHER role prompt (e.g.
# developer.md, which DID resolve on the same box, since the pipeline
# actually dispatched to `claude`) resolves through
# `hivepilot.roles._resolve_prompt_path` (XDG config home -> config repo ->
# base_dir/cwd -> packaged copy). The auditor must use that SAME mechanism.
# ---------------------------------------------------------------------------


class TestAuditorPromptResolvesLikeRolePrompts:
    def test_config_repo_override_is_picked_up(self, tmp_path: Path, monkeypatch) -> None:
        """A prompts/agents/auditor.md override placed in the config repo
        (the exact mechanism that makes developer.md resolve on the
        operator's box) must be picked up by the auditor too -- proving it
        no longer relies on the broken `Path(__file__)`-relative guess."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

        from hivepilot.config import Settings

        config_repo = tmp_path / "config_repo"
        agents_dir = config_repo / "prompts" / "agents"
        agents_dir.mkdir(parents=True)
        override_file = agents_dir / "auditor.md"
        override_file.write_text("# Overridden Henri prompt\n", encoding="utf-8")

        test_settings = Settings(config_repo=str(config_repo), base_dir=tmp_path)
        monkeypatch.setattr(auditor_service, "settings", test_settings)

        resolved = auditor_service._auditor_prompt_path()

        assert resolved == override_file, (
            f"Expected config-repo override {override_file}, got {resolved}"
        )

    def test_missing_override_falls_back_to_packaged_copy(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """No config-repo/XDG override -> falls back to the packaged copy
        (identical fallback semantics to a role's prompt_file)."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

        from hivepilot.config import Settings
        from hivepilot.roles import _PROMPTS_DIR

        config_repo = tmp_path / "config_repo"
        config_repo.mkdir()
        test_settings = Settings(config_repo=str(config_repo), base_dir=tmp_path)
        monkeypatch.setattr(auditor_service, "settings", test_settings)

        resolved = auditor_service._auditor_prompt_path()

        assert resolved == _PROMPTS_DIR / "auditor.md"
        assert resolved.exists()

    def test_missing_everywhere_raises_actionable_error(self, tmp_path: Path, monkeypatch) -> None:
        """When the prompt truly cannot be found anywhere (source checkout
        gone too), the failure must be actionable -- name the missing
        prompt and say how to fix it -- not a bare, unexplained
        FileNotFoundError surfaced from deep inside a runner."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))

        from hivepilot.config import Settings

        test_settings = Settings(config_repo=None, base_dir=tmp_path)
        monkeypatch.setattr(auditor_service, "settings", test_settings)
        # Force the packaged-copy fallback to also miss (simulates a pip
        # install with no source tree at all).
        monkeypatch.setattr(
            auditor_service, "_resolve_prompt_path", lambda name, s: tmp_path / "nope.md"
        )

        with pytest.raises(FileNotFoundError) as excinfo:
            auditor_service._auditor_prompt_path()

        message = str(excinfo.value).lower()
        assert "auditor.md" in message
        assert "config" in message  # points at the config-repo fix
        assert "sync" in message or "prompts/agents" in message

    def test_run_henri_uses_the_resolved_prompt_path(
        self, _vault: Path, tmp_path: Path, monkeypatch
    ) -> None:
        """`observe()`/`audit()` must dispatch Henri with the SAME resolved
        path `_auditor_prompt_path()` returns -- not the old hardcoded
        constant."""
        sentinel = tmp_path / "sentinel-auditor.md"
        sentinel.write_text("sentinel", encoding="utf-8")
        monkeypatch.setattr(auditor_service, "_auditor_prompt_path", lambda: sentinel)
        monkeypatch.setattr(
            auditor_service.state_service, "list_recent_interactions", lambda **k: []
        )

        reg = _registry("ok")
        auditor_service.observe(
            project=ProjectConfig(path=_vault), run_id=9, registry=reg, dry_run=True
        )

        step = reg.capture_definition.call_args.args[1].step
        assert step.prompt_file == str(sentinel)
