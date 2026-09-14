"""HP-118: local task-trace ZIP (redaction, critical-block, no upload)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from hivepilot import trace_export as export_module
from hivepilot.cli import app
from hivepilot.evidence import ingest_ref
from hivepilot.services import state_service
from hivepilot.services.config_provenance import REDACTED, register_secret_value
from hivepilot.services.structured_verdict import FileLineFinding, StructuredVerdict
from hivepilot.skill_events import record_skill_event
from hivepilot.trace_export import (
    ARCHIVE_MEMBERS,
    CRITICAL,
    EXPORT_KIND,
    REDACTION_STATUS,
    UPLOAD,
    CriticalFindingsBlock,
    TraceExportError,
    export_zip,
    project_run,
    resolve_local_zip_path,
)

_SOURCE = Path(__file__).resolve().parents[1] / "hivepilot" / "trace_export.py"

_FORBIDDEN = (
    "cloud_reporter",
    "upload_traces",
    "upload_file",
    "openspace-mcp",
    "OPENSPACE_CLOUD",
    "urllib.request",
    "httpx",
    "import requests",
    "boto3",
    "import pickle",
)


def _seed_run(*, tenant: str = "default", detail: str = "ok") -> int:
    run_id = state_service.record_run_start("example-api", "docs", tenant=tenant)
    state_service.record_step(
        run_id,
        "review",
        "success",
        detail=detail,
        provider="claude",
        model="haiku",
        role="reviewer",
    )
    state_service.record_interaction(
        actor="reviewer",
        action="note",
        target="docs",
        summary=detail,
        run_id=run_id,
    )
    record_skill_event(
        event_type="selected",
        revision_id="rev-docs",
        run_id=run_id,
        step="review",
        skill_name="docs",
        tenant=tenant,
    )
    record_skill_event(
        event_type="completed",
        revision_id="rev-docs",
        run_id=run_id,
        step="review",
        skill_name="docs",
        tenant=tenant,
    )
    ingest_ref(
        ref_id=f"tool-{run_id}",
        preview=detail,
        metadata={"run_id": run_id, "token": "ReadFile"},
        ref_type="tool_event",
        tenant=tenant,
    )
    state_service.complete_run(run_id, "success", detail=detail)
    return run_id


def _read_zip(path: Path) -> dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        return {name: json.loads(archive.read(name).decode("utf-8")) for name in archive.namelist()}


class TestSurfaceIsLocal:
    def test_source_omits_upload_and_reporter(self) -> None:
        source = _SOURCE.read_text(encoding="utf-8")
        for token in _FORBIDDEN:
            assert token not in source
        assert EXPORT_KIND == "local"
        assert UPLOAD is False
        assert not hasattr(export_module, "upload")
        assert not hasattr(export_module, "report")
        assert not hasattr(export_module, "reporter")

    def test_remote_destination_is_refused(self) -> None:
        for dest in (
            "https://example.invalid/traces.zip",
            "s3://bucket/traces.zip",
            "gs://bucket/traces.zip",
        ):
            with pytest.raises(TraceExportError, match="local path"):
                resolve_local_zip_path(dest)


class TestProjectionAndZip:
    def test_zip_contains_redacted_projection(self, tmp_path: Path) -> None:
        run_id = _seed_run()
        dest = tmp_path / "run-traces.zip"
        written = export_zip(run_id, dest)
        assert written == dest
        members = _read_zip(dest)
        assert set(members) == set(ARCHIVE_MEMBERS)
        manifest = members["manifest.json"]
        assert manifest["kind"] == "local"
        assert manifest["upload"] is False
        assert manifest["redaction_status"] == REDACTION_STATUS
        assert members["metadata.json"]["run"]["project"] == "example-api"
        assert members["metadata.json"]["run"]["task"] == "docs"
        tools = members["tools.json"]
        assert tools["steps"][0]["step"] == "review"
        assert tools["evidence"][0]["ref_type"] == "tool_event"
        skills = members["skills.json"]["events"]
        assert {row["event_type"] for row in skills} == {"selected", "completed"}
        assert members["redaction.json"]["status"] == REDACTION_STATUS
        assert members["redaction.json"]["packet"]["redaction_status"] == "redacted"

    def test_secrets_are_redacted_in_zip(self, tmp_path: Path) -> None:
        secret = "super-secret-trace-token-value"
        register_secret_value(secret)
        run_id = _seed_run(detail=f"stderr contained {secret}")
        dest = tmp_path / "redacted.zip"
        members = _read_zip(export_zip(run_id, dest))
        dumped = json.dumps(members)
        assert secret not in dumped
        assert REDACTED in dumped
        assert secret not in dest.read_bytes().decode("utf-8", errors="ignore")

    def test_tenant_mismatch_is_missing(self, tmp_path: Path) -> None:
        run_id = _seed_run(tenant="acme")
        with pytest.raises(TraceExportError, match="not found"):
            project_run(run_id, tenant="beta")
        dest = tmp_path / "missing.zip"
        with pytest.raises(TraceExportError, match="not found"):
            export_zip(run_id, dest, tenant="beta")
        assert not dest.exists()


class TestCriticalFindingsBlock:
    def test_critical_finding_refuses_zip(self, tmp_path: Path) -> None:
        run_id = _seed_run()
        verdict = StructuredVerdict(
            decision="BLOCK",
            source="review",
            owner_role="reviewer",
            block_if=("critical leak",),
            findings=(
                FileLineFinding(
                    path="hivepilot/cli.py",
                    line=10,
                    message="token leaked in prompt",
                    severity=CRITICAL,
                ),
            ),
            summary="blocked",
        )
        state_service.record_verdict(
            run_id=run_id,
            project="example-api",
            task="docs",
            role="reviewer",
            kind="review",
            decision="BLOCK",
            confidence=0.99,
            summary=verdict.summary,
            findings_json=json.dumps(verdict.to_payload()),
        )
        dest = tmp_path / "blocked.zip"
        with pytest.raises(CriticalFindingsBlock, match="critical findings block export"):
            export_zip(run_id, dest)
        assert not dest.exists()
        projection = project_run(run_id)
        assert projection.blocked() is True
        assert projection.critical_findings[0]["severity"] == CRITICAL
        assert "token leaked" in projection.critical_findings[0]["message"]

    def test_non_critical_finding_allows_export(self, tmp_path: Path) -> None:
        run_id = _seed_run()
        verdict = StructuredVerdict(
            decision="REQUEST_CHANGES",
            source="review",
            owner_role="reviewer",
            block_if=(),
            findings=(
                FileLineFinding(
                    path="README.md",
                    line=1,
                    message="typo",
                    severity="low",
                ),
            ),
            summary="nits",
        )
        state_service.record_verdict(
            run_id=run_id,
            project="example-api",
            task="docs",
            role="reviewer",
            kind="review",
            decision="REQUEST_CHANGES",
            confidence=0.4,
            summary=verdict.summary,
            findings_json=json.dumps(verdict.to_payload()),
        )
        dest = tmp_path / "ok.zip"
        export_zip(run_id, dest)
        assert dest.is_file()
        assert project_run(run_id).blocked() is False


class TestCliExport:
    def test_cli_writes_local_zip(self, tmp_path: Path) -> None:
        run_id = _seed_run()
        dest = tmp_path / "cli-traces.zip"
        result = CliRunner().invoke(
            app,
            ["traces", "export", str(run_id), "--output", str(dest)],
        )
        assert result.exit_code == 0, result.output
        assert dest.is_file()
        assert "local" in result.output
        assert str(dest) in result.output

    def test_cli_blocks_on_critical(self, tmp_path: Path) -> None:
        run_id = _seed_run()
        state_service.record_verdict(
            run_id=run_id,
            project="example-api",
            task="docs",
            role="reviewer",
            kind="review",
            decision="BLOCK",
            confidence=1.0,
            findings_json=json.dumps(
                {
                    "findings": [
                        {
                            "path": "x.py",
                            "line": 1,
                            "message": "secret in log",
                            "severity": "critical",
                        }
                    ]
                }
            ),
        )
        dest = tmp_path / "cli-blocked.zip"
        result = CliRunner().invoke(
            app,
            ["traces", "export", str(run_id), "-o", str(dest)],
        )
        assert result.exit_code == 1
        text = f"{result.output}{result.stderr or ''}"
        assert "critical findings block export" in text
        assert not dest.exists()
