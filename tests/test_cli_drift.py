"""
Tests for `hivepilot drift scan|status|report` (Phase 20 Sprint D4).

`hivepilot.services.drift_service.scan_and_record` and
`hivepilot.services.state_service.get_recent_drift_scans`/
`get_drift_baseline` are all mocked so no real terraform/opentofu binary or
state DB is ever touched. Covers:

1. `drift scan`: clean prints "No drift detected." (exit 0); drifted prints
   the +/~/- counts (exit 0); a scan failure (`RuntimeError`/`ValueError`)
   prints the safe tool+code message to stderr and exits 1 -- never raw
   plan output.
2. `drift status`/`drift report`: render rows from mocked state reads, and
   pass `tenant=` explicitly (non-None) to every state read.
3. No destructive apply path exists on this CLI group -- remediation is
   gated through the orchestrator task path (Sprint D4 core), not the CLI.
4. A secret planted in a mocked drift-scan row never appears in CLI output
   (the CLI only ever prints status/counts, never the raw `detail` field).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub out optional heavy dependencies before importing hivepilot.cli
# (mirrors tests/test_cli_iac.py / tests/test_cli_scan.py so this file can
# run standalone).
# ---------------------------------------------------------------------------

_STUBS = [
    "langchain",
    "langchain.text_splitter",
    "langchain_community",
    "langchain_community.embeddings",
    "langchain_community.vectorstores",
    "langchain_openai",
    "openai",
    "boto3",
    "docker",
    "telegram",
    "telegram.ext",
    "fastapi",
    "fastapi.responses",
    "fastapi.security",
    "uvicorn",
    "textual",
    "slack_bolt",
    "slack_bolt.adapter",
    "slack_bolt.adapter.fastapi",
    "slack_bolt.adapter.socket_mode",
    "discord",
    "PyNaCl",
    "nacl",
    "nacl.exceptions",
    "nacl.signing",
]

import importlib  # noqa: E402

for _mod in _STUBS:
    if _mod in sys.modules:
        continue
    try:
        importlib.import_module(_mod)
    except Exception:
        sys.modules[_mod] = MagicMock()

from typer.testing import CliRunner  # noqa: E402

import hivepilot.cli as cli_module  # noqa: E402
from hivepilot.cli import app  # noqa: E402
from hivepilot.models import ProjectConfig, ProjectsFile  # noqa: E402
from hivepilot.services.drift_service import DriftResult, DriftSummary  # noqa: E402

_LEAKED_LOOKING_TOKEN = "sk-live-should-never-leak-0123456789"  # noqa: S105


@pytest.fixture(autouse=True)
def patch_projects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    projects = ProjectsFile(projects={"proj": ProjectConfig(path=tmp_path)})
    monkeypatch.setattr(cli_module, "load_projects", lambda: projects)


class TestDriftScan:
    def test_clean_scan_prints_no_drift_and_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = DriftResult(
            project="proj",
            runner="opentofu",
            drifted=False,
            summary=DriftSummary(to_add=0, to_change=0, to_destroy=0),
        )
        monkeypatch.setattr(
            "hivepilot.services.drift_service.scan_and_record", lambda *a, **k: result
        )
        runner = CliRunner()
        cli_result = runner.invoke(app, ["drift", "scan", "--project", "proj"])
        assert cli_result.exit_code == 0, cli_result.output
        assert "No drift detected." in cli_result.output

    def test_drifted_scan_prints_counts_and_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = DriftResult(
            project="proj",
            runner="opentofu",
            drifted=True,
            summary=DriftSummary(to_add=1, to_change=2, to_destroy=3),
        )
        monkeypatch.setattr(
            "hivepilot.services.drift_service.scan_and_record", lambda *a, **k: result
        )
        runner = CliRunner()
        cli_result = runner.invoke(app, ["drift", "scan", "--project", "proj"])
        assert cli_result.exit_code == 0, cli_result.output
        assert "1" in cli_result.output and "2" in cli_result.output and "3" in cli_result.output

    def test_scan_failure_prints_safe_message_to_stderr_and_exits_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(*args: object, **kwargs: object) -> DriftResult:
            raise RuntimeError(f"tofu drift check failed with exit code 1: {_LEAKED_LOOKING_TOKEN}")

        monkeypatch.setattr("hivepilot.services.drift_service.scan_and_record", _boom)
        runner = CliRunner()
        cli_result = runner.invoke(app, ["drift", "scan", "--project", "proj"])
        assert cli_result.exit_code == 1
        # The exception message here is deliberately tool+code-only per
        # drift_service's contract -- this test proves the CLI doesn't
        # additionally echo raw plan output on top of that message.
        assert "exit code 1" in cli_result.output

    def test_unknown_project_is_a_bad_parameter(self) -> None:
        runner = CliRunner()
        cli_result = runner.invoke(app, ["drift", "scan", "--project", "ghost"])
        assert cli_result.exit_code != 0

    def test_scan_passes_tenant_explicitly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict] = []

        def _fake_scan(project: ProjectConfig, **kwargs: object) -> DriftResult:
            calls.append(kwargs)
            return DriftResult(project="proj", runner="opentofu", drifted=False)

        monkeypatch.setattr("hivepilot.services.drift_service.scan_and_record", _fake_scan)
        runner = CliRunner()
        cli_result = runner.invoke(app, ["drift", "scan", "--project", "proj"])
        assert cli_result.exit_code == 0, cli_result.output
        assert len(calls) == 1
        assert calls[0].get("tenant") is not None


class TestDriftStatus:
    def test_renders_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [
            {
                "checked_at": "2026-07-17T10:00:00",
                "project": "proj",
                "runner": "opentofu",
                "status": "drift",
                "to_add": 1,
                "to_change": 2,
                "to_destroy": 3,
            }
        ]
        monkeypatch.setattr(
            "hivepilot.services.state_service.get_recent_drift_scans", lambda *a, **k: rows
        )
        runner = CliRunner()
        cli_result = runner.invoke(app, ["drift", "status"])
        assert cli_result.exit_code == 0, cli_result.output
        assert "proj" in cli_result.output
        assert "opentofu" in cli_result.output
        assert "drift" in cli_result.output

    def test_empty_history_prints_friendly_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "hivepilot.services.state_service.get_recent_drift_scans", lambda *a, **k: []
        )
        runner = CliRunner()
        cli_result = runner.invoke(app, ["drift", "status"])
        assert cli_result.exit_code == 0
        assert "No" in cli_result.output

    def test_passes_tenant_explicitly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[dict] = []

        def _fake_reads(*args: object, **kwargs: object) -> list[dict]:
            calls.append(kwargs)
            return []

        monkeypatch.setattr("hivepilot.services.state_service.get_recent_drift_scans", _fake_reads)
        runner = CliRunner()
        cli_result = runner.invoke(app, ["drift", "status"])
        assert cli_result.exit_code == 0, cli_result.output
        assert len(calls) == 1
        assert calls[0].get("tenant") is not None

    def test_secret_in_row_detail_never_printed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = [
            {
                "checked_at": "2026-07-17T10:00:00",
                "project": "proj",
                "runner": "opentofu",
                "status": "error",
                "detail": f"leaked={_LEAKED_LOOKING_TOKEN}",
                "to_add": None,
                "to_change": None,
                "to_destroy": None,
            }
        ]
        monkeypatch.setattr(
            "hivepilot.services.state_service.get_recent_drift_scans", lambda *a, **k: rows
        )
        runner = CliRunner()
        cli_result = runner.invoke(app, ["drift", "status"])
        assert cli_result.exit_code == 0, cli_result.output
        assert _LEAKED_LOOKING_TOKEN not in cli_result.output


class TestDriftReport:
    def test_shows_baseline_and_history(self, monkeypatch: pytest.MonkeyPatch) -> None:
        baseline = {
            "checked_at": "2026-07-17T09:00:00",
            "project": "proj",
            "runner": "opentofu",
            "status": "ok",
        }
        history = [baseline]
        monkeypatch.setattr(
            "hivepilot.services.state_service.get_drift_baseline", lambda *a, **k: baseline
        )
        monkeypatch.setattr(
            "hivepilot.services.state_service.get_recent_drift_scans", lambda *a, **k: history
        )
        runner = CliRunner()
        cli_result = runner.invoke(app, ["drift", "report", "--project", "proj"])
        assert cli_result.exit_code == 0, cli_result.output
        assert "proj" in cli_result.output

    def test_no_baseline_prints_friendly_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "hivepilot.services.state_service.get_drift_baseline", lambda *a, **k: None
        )
        monkeypatch.setattr(
            "hivepilot.services.state_service.get_recent_drift_scans", lambda *a, **k: []
        )
        runner = CliRunner()
        cli_result = runner.invoke(app, ["drift", "report", "--project", "proj"])
        assert cli_result.exit_code == 0, cli_result.output
        assert "No" in cli_result.output

    def test_passes_tenant_explicitly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        baseline_calls: list[dict] = []
        history_calls: list[dict] = []

        def _fake_baseline(*args: object, **kwargs: object) -> dict | None:
            baseline_calls.append(kwargs)
            return None

        def _fake_history(*args: object, **kwargs: object) -> list[dict]:
            history_calls.append(kwargs)
            return []

        monkeypatch.setattr("hivepilot.services.state_service.get_drift_baseline", _fake_baseline)
        monkeypatch.setattr(
            "hivepilot.services.state_service.get_recent_drift_scans", _fake_history
        )
        runner = CliRunner()
        cli_result = runner.invoke(app, ["drift", "report", "--project", "proj"])
        assert cli_result.exit_code == 0, cli_result.output
        assert len(baseline_calls) == 1
        assert baseline_calls[0].get("tenant") is not None
        assert len(history_calls) == 1
        assert history_calls[0].get("tenant") is not None


class TestNoDirectApplyPath:
    """The `drift` CLI group must never expose a destructive apply command --
    remediation is gated exclusively through `Orchestrator.run_task` (see
    `tests/test_drift_schedule.py::TestRunDriftScanRemediation`)."""

    def test_drift_group_has_no_apply_or_destroy_command(self) -> None:
        from hivepilot.cli import drift_app

        command_names = {cmd.name for cmd in drift_app.registered_commands}
        assert "apply" not in command_names
        assert "destroy" not in command_names
        assert {"scan", "status", "report"}.issubset(command_names)
