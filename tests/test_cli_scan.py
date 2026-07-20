"""
Tests for `hivepilot scan vulns`/`hivepilot scan sbom` (Phase 21 Sprint 1).

`hivepilot.services.scan_service` is mocked (monkeypatched functions) so no
real grype/osv-scanner/syft binary is ever invoked. Covers:

1. `scan vulns` prints the severity summary + findings table.
2. `scan vulns --fail-on critical` exits non-zero when a critical finding
   exists, and exits 0 when it doesn't (or isn't set at all).
3. `scan sbom` writes the SBOM to `--output` when given, else prints it.
4. No secret-looking content is echoed in stdout on any path (the service is
   mocked so this really just proves the CLI doesn't do anything silly with
   its own error paths — the anti-leak guarantee itself lives in
   `tests/test_scan_service.py`).
5. A missing tool (mocked `RuntimeError` from the service) surfaces a clear
   CLI error and a non-zero exit code.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub out optional heavy dependencies before importing hivepilot.cli
# (mirrors tests/test_cli_iac.py / tests/test_cli.py so this file can run
# standalone).
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
from hivepilot.services import scan_service  # noqa: E402
from hivepilot.services.scan_service import (  # noqa: E402
    ComponentLicense,
    Finding,
    LicenseResult,
    ScanResult,
)

_SECRET_LOOKING_VALUE = "sk-live-cli-should-never-echo-this"  # noqa: S105


@pytest.fixture(autouse=True)
def patch_projects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    projects = ProjectsFile(projects={"proj": ProjectConfig(path=tmp_path)})
    monkeypatch.setattr(cli_module, "load_projects", lambda: projects)


def _result_with(severity: str) -> ScanResult:
    counts = scan_service.empty_severity_counts()
    counts[severity] = 1
    return ScanResult(
        tool="grype",
        total=1,
        by_severity=counts,
        findings=[
            Finding(
                id="CVE-2023-0001",
                package="libfoo",
                version="1.0.0",
                severity=severity,
                fixed_version="1.0.1",
            )
        ],
    )


class TestScanVulnsCommand:
    def test_prints_summary_and_findings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            scan_service, "scan_vulnerabilities", lambda *a, **k: _result_with("high")
        )
        runner = CliRunner()
        result = runner.invoke(app, ["scan", "vulns", "proj"])
        assert result.exit_code == 0, result.output
        assert "high" in result.output.lower()
        assert "CVE-2023-0001" in result.output
        assert "libfoo" in result.output

    def test_fail_on_exits_nonzero_when_severity_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            scan_service, "scan_vulnerabilities", lambda *a, **k: _result_with("critical")
        )
        runner = CliRunner()
        result = runner.invoke(app, ["scan", "vulns", "proj", "--fail-on", "critical"])
        assert result.exit_code != 0

    def test_fail_on_exits_zero_when_severity_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            scan_service, "scan_vulnerabilities", lambda *a, **k: _result_with("low")
        )
        runner = CliRunner()
        result = runner.invoke(app, ["scan", "vulns", "proj", "--fail-on", "critical"])
        assert result.exit_code == 0, result.output

    def test_no_fail_on_exits_zero_regardless_of_severity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            scan_service, "scan_vulnerabilities", lambda *a, **k: _result_with("critical")
        )
        runner = CliRunner()
        result = runner.invoke(app, ["scan", "vulns", "proj"])
        assert result.exit_code == 0, result.output

    def test_missing_tool_error_surfaces_clear_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*a, **k):
            raise RuntimeError(
                "grype not found on PATH. Install it before running a vulnerability scan."
            )

        monkeypatch.setattr(scan_service, "scan_vulnerabilities", _raise)
        runner = CliRunner()
        result = runner.invoke(app, ["scan", "vulns", "proj"])
        assert result.exit_code != 0
        assert "grype not found" in result.output

    def test_unknown_project_errors(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["scan", "vulns", "does-not-exist"])
        assert result.exit_code != 0

    def test_no_secret_looking_value_echoed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result_obj = _result_with("high")
        monkeypatch.setattr(scan_service, "scan_vulnerabilities", lambda *a, **k: result_obj)
        runner = CliRunner()
        result = runner.invoke(app, ["scan", "vulns", "proj"])
        assert _SECRET_LOOKING_VALUE not in result.output


class TestScanSbomCommand:
    def test_prints_sbom_when_no_output_given(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            scan_service, "generate_sbom", lambda *a, **k: '{"bomFormat": "CycloneDX"}'
        )
        runner = CliRunner()
        result = runner.invoke(app, ["scan", "sbom", "proj"])
        assert result.exit_code == 0, result.output
        assert "CycloneDX" in result.output

    def test_writes_to_output_file(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        # Exercises the REAL generate_sbom (only subprocess.run/syft-lookup are
        # mocked) with a nested, not-yet-existing output directory, proving
        # the CLI end-to-end writes the SBOM to disk without a traceback.
        sbom_content = '{"bomFormat": "CycloneDX"}'
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/syft")
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(
                args=["fake"], returncode=0, stdout=sbom_content, stderr=""
            ),
        )
        out_file = tmp_path / "out" / "sbom.json"
        runner = CliRunner()
        result = runner.invoke(app, ["scan", "sbom", "proj", "--output", str(out_file)])
        assert result.exit_code == 0, result.output
        assert out_file.exists()
        assert out_file.read_text() == sbom_content

    def test_missing_syft_error_surfaces_clear_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*a, **k):
            raise RuntimeError("syft not found on PATH. Install it before generating an SBOM.")

        monkeypatch.setattr(scan_service, "generate_sbom", _raise)
        runner = CliRunner()
        result = runner.invoke(app, ["scan", "sbom", "proj"])
        assert result.exit_code != 0
        assert "syft not found" in result.output


def _license_result(*, violations: bool = False) -> LicenseResult:
    components = [
        ComponentLicense(package="libfoo", version="1.0.0", licenses=("MIT",)),
        ComponentLicense(package="libgpl", version="2.0.0", licenses=("GPL-3.0",)),
    ]
    return LicenseResult(
        tool="syft",
        total=2,
        components=components,
        violations=[components[1]] if violations else [],
    )


class TestScanLicensesCommand:
    def test_prints_table_with_violation_marked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            scan_service, "check_licenses", lambda *a, **k: _license_result(violations=True)
        )
        runner = CliRunner()
        result = runner.invoke(app, ["scan", "licenses", "proj", "--deny", "GPL-3.0"])
        assert result.exit_code == 0, result.output
        assert "libfoo" in result.output
        assert "libgpl" in result.output
        assert "VIOLATION" in result.output
        assert "Violations: 1" in result.output

    def test_fail_on_violation_exits_nonzero_when_violations_present(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            scan_service, "check_licenses", lambda *a, **k: _license_result(violations=True)
        )
        runner = CliRunner()
        result = runner.invoke(
            app, ["scan", "licenses", "proj", "--deny", "GPL-3.0", "--fail-on-violation"]
        )
        assert result.exit_code != 0

    def test_no_fail_on_violation_flag_exits_zero_despite_violations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            scan_service, "check_licenses", lambda *a, **k: _license_result(violations=True)
        )
        runner = CliRunner()
        result = runner.invoke(app, ["scan", "licenses", "proj", "--deny", "GPL-3.0"])
        assert result.exit_code == 0, result.output

    def test_fail_on_violation_exits_zero_when_no_violations(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            scan_service, "check_licenses", lambda *a, **k: _license_result(violations=False)
        )
        runner = CliRunner()
        result = runner.invoke(
            app, ["scan", "licenses", "proj", "--allow", "MIT", "--fail-on-violation"]
        )
        assert result.exit_code == 0, result.output

    def test_missing_tool_error_surfaces_clear_message(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _raise(*a, **k):
            raise RuntimeError("syft not found on PATH. Install it before generating an SBOM.")

        monkeypatch.setattr(scan_service, "check_licenses", _raise)
        runner = CliRunner()
        result = runner.invoke(app, ["scan", "licenses", "proj"])
        assert result.exit_code != 0
        assert "syft not found" in result.output

    def test_unknown_project_errors(self) -> None:
        runner = CliRunner()
        result = runner.invoke(app, ["scan", "licenses", "does-not-exist"])
        assert result.exit_code != 0
