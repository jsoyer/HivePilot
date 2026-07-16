"""
Tests for `hivepilot.services.scan_service` (Phase 21 Sprint 1 — supply-chain
scanning: vulnerability scan via grype/osv-scanner + SBOM generation via
syft).

Every scanner subprocess is mocked via `subprocess.run` (monkeypatched) with
realistic fixture JSON — no real grype/osv-scanner/syft binary is ever
invoked. Assertions cover:

1. `scan_vulnerabilities` parses `by_severity` counts + `findings` correctly
   for both grype and osv-scanner fixture JSON.
2. A clean scan (no vulnerabilities) returns an empty/zero `ScanResult`, not
   an error.
3. A missing tool (`shutil.which` returns None) raises a clear `RuntimeError`
   — the scan is never attempted.
4. The raw scanner stdout text is NEVER present anywhere in the returned
   `ScanResult` (only parsed/structured fields) — this is the core
   anti-leak guarantee the sprint spec calls out.
5. `generate_sbom` invokes `syft` with the right output-format flag for
   cyclonedx vs spdx, handles `output_path` writing, and raises a clear
   error when `syft` is missing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from hivepilot.services import scan_service
from hivepilot.services.scan_service import Finding, ScanResult

# ---------------------------------------------------------------------------
# Fixture JSON payloads (realistic shapes for grype / osv-scanner)
# ---------------------------------------------------------------------------

GRYPE_FIXTURE: dict[str, Any] = {
    "matches": [
        {
            "vulnerability": {
                "id": "CVE-2023-1111",
                "severity": "Critical",
                "fix": {"versions": ["2.1.0"], "state": "fixed"},
                # Unextracted field — must never end up in the parsed
                # ScanResult even though it's present in the raw JSON.
                "description": "See LEAKED_LOOKING_TOKEN_PLACEHOLDER for details",
            },
            "artifact": {"name": "libfoo", "version": "2.0.0", "type": "python"},
        },
        {
            "vulnerability": {
                "id": "CVE-2023-2222",
                "severity": "High",
                "fix": {"versions": [], "state": "not-fixed"},
            },
            "artifact": {"name": "libbar", "version": "1.5.0", "type": "python"},
        },
        {
            "vulnerability": {
                "id": "CVE-2023-3333",
                "severity": "Medium",
                "fix": {"versions": ["3.0.1"], "state": "fixed"},
            },
            "artifact": {"name": "libbaz", "version": "3.0.0", "type": "python"},
        },
    ]
}

GRYPE_EMPTY_FIXTURE: dict[str, Any] = {"matches": []}

OSV_FIXTURE: dict[str, Any] = {
    "results": [
        {
            "source": {"path": "requirements.txt", "type": "lockfile"},
            "packages": [
                {
                    "package": {"name": "django", "version": "3.2.0", "ecosystem": "PyPI"},
                    "vulnerabilities": [
                        {
                            "id": "GHSA-aaaa-bbbb-cccc",
                            "aliases": ["CVE-2023-9999"],
                            "affected": [
                                {
                                    "ranges": [
                                        {
                                            "type": "ECOSYSTEM",
                                            "events": [
                                                {"introduced": "0"},
                                                {"fixed": "3.2.18"},
                                            ],
                                        }
                                    ]
                                }
                            ],
                        }
                    ],
                    "groups": [
                        {
                            "ids": ["GHSA-aaaa-bbbb-cccc"],
                            "aliases": ["CVE-2023-9999"],
                            "max_severity": "9.8",
                        }
                    ],
                }
            ],
        }
    ]
}

# A secret-looking token that must never appear in the parsed ScanResult even
# though it's embedded (via the "description" field) in the raw scanner
# stdout fixture below.
_LEAKED_LOOKING_TOKEN = "sk-live-should-never-leak-0123456789"  # noqa: S105


def _fake_completed_process(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["fake"], returncode=returncode, stdout=stdout, stderr=""
    )


def _grype_stdout_with_leak_marker() -> str:
    return json.dumps(GRYPE_FIXTURE).replace(
        "LEAKED_LOOKING_TOKEN_PLACEHOLDER", _LEAKED_LOOKING_TOKEN
    )


class TestScanVulnerabilitiesGrype:
    def test_parses_by_severity_and_findings(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/grype")
        raw_stdout = _grype_stdout_with_leak_marker()
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed_process(raw_stdout))

        result = scan_service.scan_vulnerabilities(tmp_path, tool="grype")

        assert result.tool == "grype"
        assert result.total == 3
        assert result.by_severity["critical"] == 1
        assert result.by_severity["high"] == 1
        assert result.by_severity["medium"] == 1
        assert result.by_severity["low"] == 0
        assert result.error is None

        ids = {f.id for f in result.findings}
        assert ids == {"CVE-2023-1111", "CVE-2023-2222", "CVE-2023-3333"}

        critical = next(f for f in result.findings if f.id == "CVE-2023-1111")
        assert critical.package == "libfoo"
        assert critical.version == "2.0.0"
        assert critical.severity == "critical"
        assert critical.fixed_version == "2.1.0"

        no_fix = next(f for f in result.findings if f.id == "CVE-2023-2222")
        assert no_fix.fixed_version is None

    def test_raw_stdout_never_present_in_structured_result(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/grype")
        raw_stdout = _grype_stdout_with_leak_marker()
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _fake_completed_process(raw_stdout))

        result = scan_service.scan_vulnerabilities(tmp_path, tool="grype")

        serialized = repr(result)
        assert _LEAKED_LOOKING_TOKEN not in serialized
        assert not hasattr(result, "raw_stdout")
        assert not hasattr(result, "stdout")

    def test_clean_scan_returns_empty_result_not_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/grype")
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: _fake_completed_process(json.dumps(GRYPE_EMPTY_FIXTURE)),
        )

        result = scan_service.scan_vulnerabilities(tmp_path, tool="grype")

        assert result.error is None
        assert result.total == 0
        assert result.findings == []
        assert all(count == 0 for count in result.by_severity.values())

    def test_severity_threshold_filters_findings_but_keeps_full_breakdown(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/grype")
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _fake_completed_process(json.dumps(GRYPE_FIXTURE))
        )

        result = scan_service.scan_vulnerabilities(
            tmp_path, tool="grype", severity_threshold="high"
        )

        # Full breakdown always reflects everything found.
        assert result.by_severity["medium"] == 1
        # But the findings list only surfaces >= threshold.
        assert {f.severity for f in result.findings} == {"critical", "high"}


class TestScanVulnerabilitiesOsv:
    def test_parses_osv_scanner_output(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/osv-scanner")
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: _fake_completed_process(json.dumps(OSV_FIXTURE), returncode=1),
        )

        result = scan_service.scan_vulnerabilities(tmp_path, tool="osv-scanner")

        assert result.tool == "osv-scanner"
        assert result.total == 1
        assert result.by_severity["critical"] == 1
        finding = result.findings[0]
        assert finding.id == "CVE-2023-9999"
        assert finding.package == "django"
        assert finding.version == "3.2.0"
        assert finding.severity == "critical"
        assert finding.fixed_version == "3.2.18"

    def test_unexpected_exit_code_raises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/osv-scanner")
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda *a, **k: _fake_completed_process("", returncode=127),
        )

        with pytest.raises(RuntimeError):
            scan_service.scan_vulnerabilities(tmp_path, tool="osv-scanner")


class TestScanVulnerabilitiesMissingTool:
    def test_missing_grype_raises_clear_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(RuntimeError, match="grype"):
            scan_service.scan_vulnerabilities(tmp_path, tool="grype")

    def test_missing_osv_scanner_raises_clear_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(RuntimeError, match="osv-scanner"):
            scan_service.scan_vulnerabilities(tmp_path, tool="osv-scanner")

    def test_unsupported_tool_raises_value_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            scan_service.scan_vulnerabilities(tmp_path, tool="not-a-real-tool")


class TestExceedsSeverity:
    def test_exceeds_when_finding_at_or_above_threshold(self) -> None:
        result = ScanResult(
            tool="grype",
            total=1,
            by_severity={**scan_service.empty_severity_counts(), "critical": 1},
            findings=[Finding(id="CVE-1", package="p", version="1", severity="critical")],
        )
        assert scan_service.exceeds_severity(result, "critical") is True
        assert scan_service.exceeds_severity(result, "high") is True

    def test_does_not_exceed_when_below_threshold(self) -> None:
        result = ScanResult(
            tool="grype",
            total=1,
            by_severity={**scan_service.empty_severity_counts(), "low": 1},
            findings=[Finding(id="CVE-1", package="p", version="1", severity="low")],
        )
        assert scan_service.exceeds_severity(result, "high") is False


class TestGenerateSbom:
    def test_cyclonedx_format_invokes_syft_with_cyclonedx_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/syft")
        captured: dict[str, Any] = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _fake_completed_process('{"bomFormat": "CycloneDX"}')

        monkeypatch.setattr(subprocess, "run", _fake_run)

        sbom = scan_service.generate_sbom(tmp_path, format="cyclonedx")

        assert "cyclonedx-json" in captured["cmd"]
        assert "CycloneDX" in sbom

    def test_spdx_format_invokes_syft_with_spdx_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/syft")
        captured: dict[str, Any] = {}

        def _fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            return _fake_completed_process('{"spdxVersion": "SPDX-2.3"}')

        monkeypatch.setattr(subprocess, "run", _fake_run)

        sbom = scan_service.generate_sbom(tmp_path, format="spdx")

        assert "spdx-json" in captured["cmd"]
        assert "SPDX-2.3" in sbom

    def test_writes_to_output_path_when_given(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/syft")
        monkeypatch.setattr(
            subprocess, "run", lambda *a, **k: _fake_completed_process('{"bomFormat": "CycloneDX"}')
        )

        out_file = tmp_path / "sbom.json"
        sbom = scan_service.generate_sbom(tmp_path, format="cyclonedx", output_path=out_file)

        assert out_file.exists()
        assert out_file.read_text() == sbom

    def test_missing_syft_raises_clear_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: None)
        with pytest.raises(RuntimeError, match="syft"):
            scan_service.generate_sbom(tmp_path)

    def test_unsupported_format_raises_value_error(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            scan_service.generate_sbom(tmp_path, format="not-a-real-format")
