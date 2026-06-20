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
