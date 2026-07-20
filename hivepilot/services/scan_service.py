"""Read-only supply-chain security scanning (Phase 21 Sprint 1).

Two capabilities, both wrapping a locally installed CLI tool via a plain
`subprocess.run` (never `shell=True`):

* `scan_vulnerabilities` — dependency CVE scan via `grype` (default) or
  `osv-scanner`, requested with JSON output.
* `generate_sbom` — Software Bill of Materials generation via `syft`
  (CycloneDX or SPDX JSON).

This is a **service**, not a runner: it is invoked directly by
`hivepilot scan vulns`/`hivepilot scan sbom`, never through
`Orchestrator`/`RunResult`. That distinction matters for the anti-leak
guarantee below.

Anti-leak guarantee
--------------------
A vulnerability scanner's raw JSON stdout can echo fragments of the scanned
tree (package names, file paths, occasionally embedded strings from a
lockfile or vendored source). `scan_vulnerabilities` parses that JSON
**inside this module** and returns only a structured `ScanResult` — a
`tool`/`total`/`by_severity` dict/`findings` list of `Finding` records. The
raw stdout string is never stored on the returned object, never logged, and
never propagated to a caller. This mirrors the discipline the `kubectl`/IaC
runners use for `RunResult.detail` (see `hivepilot.runners.kubectl_runner`),
except here there is no `RunResult` in the path at all to leak through in
the first place — the CLI (`hivepilot.cli`) only ever sees the parsed
`ScanResult`.

`generate_sbom` is different in kind: the SBOM *is* the deliverable (a
CycloneDX/SPDX document meant for external consumption), so its content is
returned/written verbatim — there is nothing to redact there, it's the
intentional output of the command.

A pipeline-level CVE policy gate (failing a *pipeline stage* on a severity
threshold) is a separate follow-up sprint; this sprint only wires a manual
`--fail-on` gate at the CLI layer (see `hivepilot.cli`).

License compliance (Phase 21, license-compliance sprint)
----------------------------------------------------------
`check_licenses` derives per-component license data from the **same**
`generate_sbom` CycloneDX-JSON output rather than invoking a second scanner
binary — the SBOM already carries each component's `licenses` field, so this
reuses the exact anti-leak-safe subprocess path `generate_sbom` follows (no
new binary beyond `syft`, which SBOM generation already requires). The SBOM
string is parsed **in-memory** (never written to disk unless the caller of
`generate_sbom` itself asked for that) and only structured `ComponentLicense`
records are returned. A JSON-parse failure never echoes the raw SBOM text —
`LicenseResult.error` is a generic, fixed message.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_SCAN_TIMEOUT = 300

# Canonical severity vocabulary shared by both grype and osv-scanner parsing,
# and by the CLI's `--fail-on` gate. Ordered most -> least severe; "unknown"
# is deliberately last/lowest-ranked so an unrecognized/unparseable severity
# never silently trips a `--fail-on` gate set to a real severity level.
SEVERITY_LEVELS: tuple[str, ...] = ("critical", "high", "medium", "low", "negligible", "unknown")

SEVERITY_RANK: dict[str, int] = {
    level: rank for rank, level in enumerate(reversed(SEVERITY_LEVELS))
}

_VULN_TOOLS = frozenset({"grype", "osv-scanner"})
_SBOM_FORMATS: dict[str, str] = {
    "cyclonedx": "cyclonedx-json",
    "spdx": "spdx-json",
}


def empty_severity_counts() -> dict[str, int]:
    """A fresh `{severity: 0}` dict covering every level in `SEVERITY_LEVELS`."""
    return {level: 0 for level in SEVERITY_LEVELS}


@dataclass(frozen=True)
class Finding:
    """One parsed vulnerability finding — structured fields only, nothing
    that echoes raw scanner stdout."""

    id: str
    package: str
    version: str
    severity: str
    fixed_version: str | None = None


@dataclass(frozen=True)
class ScanResult:
    """Structured, parsed vulnerability-scan outcome.

    `error` is populated (with `total`/`by_severity`/`findings` left at their
    empty defaults) only for the "tool ran but produced something we could
    not make sense of" case internal callers choose not to raise on; the
    public `scan_vulnerabilities` entry point in this module always raises
    `RuntimeError` instead (a missing tool or an unexpected exit code is
    always an exception, never a silently-populated `error` field) — kept as
    a field so a future caller that prefers non-raising error handling has
    somewhere to put it without changing the shape of this dataclass.
    """

    tool: str
    total: int
    by_severity: dict[str, int] = field(default_factory=empty_severity_counts)
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None


def exceeds_severity(result: ScanResult, threshold: str) -> bool:
    """True if any finding in *result* is at or above *threshold* severity."""
    threshold_rank = SEVERITY_RANK[threshold]
    return any(
        SEVERITY_RANK.get(f.severity, SEVERITY_RANK["unknown"]) >= threshold_rank
        for f in result.findings
    )


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------


def _run_tool(
    cmd: list[str],
    *,
    cwd: str,
    timeout: int,
    tool_name: str,
    allowed_returncodes: frozenset[int],
) -> str:
    """Run *cmd*, returning stdout. Raises `RuntimeError` (tool name + exit
    code only — never stdout/stderr content, which can echo scanned-tree
    material) on timeout or an unexpected exit code."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"{tool_name} timed out after {timeout}s") from exc

    if proc.returncode not in allowed_returncodes:
        raise RuntimeError(f"{tool_name} exited with code {proc.returncode}")
    return proc.stdout


def _require_tool(tool: str, *, purpose: str) -> None:
    if not shutil.which(tool):
        raise RuntimeError(f"{tool} not found on PATH. Install it before {purpose}.")


def _normalize_severity(raw: str | None) -> str:
    if not raw:
        return "unknown"
    lowered = raw.strip().lower()
    return lowered if lowered in SEVERITY_LEVELS else "unknown"


# ---------------------------------------------------------------------------
# grype JSON parsing
# ---------------------------------------------------------------------------


def _parse_grype(stdout: str) -> tuple[dict[str, int], list[Finding]]:
    try:
        data = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError("grype produced invalid JSON output") from exc

    counts = empty_severity_counts()
    findings: list[Finding] = []
    for match in data.get("matches", []) or []:
        vuln = match.get("vulnerability", {}) or {}
        artifact = match.get("artifact", {}) or {}
        severity = _normalize_severity(vuln.get("severity"))
        counts[severity] += 1
        fix = vuln.get("fix", {}) or {}
        fix_versions = fix.get("versions") or []
        findings.append(
            Finding(
                id=vuln.get("id", "unknown"),
                package=artifact.get("name", "unknown"),
                version=artifact.get("version", "unknown"),
                severity=severity,
                fixed_version=fix_versions[0] if fix_versions else None,
            )
        )
    return counts, findings


# ---------------------------------------------------------------------------
# osv-scanner JSON parsing
#
# osv-scanner groups vulnerability ids that share the same fix under one
# `groups` entry per package, exposing a single `max_severity` (a numeric
# CVSS base-score string, e.g. "9.8") for the whole group rather than a
# categorical label like grype's. Each id in a group is treated as one
# finding sharing that group's severity bucket (bucketed via
# `_CVSS_SEVERITY_THRESHOLDS`, the standard CVSS v3 qualitative ranges). A
# fixed version, when available, is read from the matching vulnerability's
# `affected[].ranges[].events[].fixed` entry.
# ---------------------------------------------------------------------------

_CVSS_SEVERITY_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (9.0, "critical"),
    (7.0, "high"),
    (4.0, "medium"),
    (0.1, "low"),
)


def _osv_severity_from_score(raw_score: str | None) -> str:
    if not raw_score:
        return "unknown"
    try:
        score = float(raw_score)
    except ValueError:
        return "unknown"
    for threshold, label in _CVSS_SEVERITY_THRESHOLDS:
        if score >= threshold:
            return label
    return "negligible" if score >= 0 else "unknown"


def _osv_preferred_id(ids: list[str], aliases: list[str]) -> str:
    """Prefer a CVE alias (more universally recognized) over the native
    GHSA/OSV id; falls back to the first id if no CVE alias exists."""
    for alias in aliases:
        if alias.startswith("CVE-"):
            return alias
    return ids[0] if ids else "unknown"


def _osv_fixed_version(vulnerabilities: list[dict[str, Any]], vuln_id: str) -> str | None:
    for vuln in vulnerabilities:
        known_ids = {vuln.get("id")} | set(vuln.get("aliases") or [])
        if vuln_id not in known_ids:
            continue
        for affected in vuln.get("affected", []) or []:
            for rng in affected.get("ranges", []) or []:
                for event in rng.get("events", []) or []:
                    fixed = event.get("fixed")
                    if fixed:
                        return str(fixed)
    return None


def _parse_osv_scanner(stdout: str) -> tuple[dict[str, int], list[Finding]]:
    try:
        data = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError("osv-scanner produced invalid JSON output") from exc

    counts = empty_severity_counts()
    findings: list[Finding] = []
    for result in data.get("results", []) or []:
        for pkg_entry in result.get("packages", []) or []:
            package = pkg_entry.get("package", {}) or {}
            pkg_name = package.get("name", "unknown")
            pkg_version = package.get("version", "unknown")
            vulnerabilities = pkg_entry.get("vulnerabilities", []) or []
            for group in pkg_entry.get("groups", []) or []:
                ids = group.get("ids") or []
                aliases = group.get("aliases") or []
                finding_id = _osv_preferred_id(ids, aliases)
                severity = _osv_severity_from_score(group.get("max_severity"))
                counts[severity] += 1
                fixed_version = _osv_fixed_version(vulnerabilities, ids[0] if ids else finding_id)
                findings.append(
                    Finding(
                        id=finding_id,
                        package=pkg_name,
                        version=pkg_version,
                        severity=severity,
                        fixed_version=fixed_version,
                    )
                )
    return counts, findings


# ---------------------------------------------------------------------------
# Public API — vulnerability scanning
# ---------------------------------------------------------------------------


def scan_vulnerabilities(
    project_path: str | Path,
    *,
    tool: str = "grype",
    severity_threshold: str | None = None,
    timeout: int = _DEFAULT_SCAN_TIMEOUT,
) -> ScanResult:
    """Run a dependency vulnerability scan against *project_path*.

    `tool` selects the scanner: `"grype"` (default) or `"osv-scanner"`.
    `severity_threshold`, when given, filters the returned `findings` list to
    only entries at or above that severity (`by_severity`/`total` always
    reflect the FULL breakdown, so a caller can still see the complete
    picture even when only asking for the high-severity subset).

    Raises `ValueError` for an unsupported `tool`, `RuntimeError` if the tool
    binary isn't on `PATH`, the scan times out, or the scanner exits with an
    unexpected code. A scan that completes with zero findings returns a
    normal (non-error) `ScanResult` with `total=0`.
    """
    if tool not in _VULN_TOOLS:
        raise ValueError(
            f"Unsupported vulnerability scan tool: {tool!r}. Supported: {sorted(_VULN_TOOLS)}"
        )
    if severity_threshold is not None and severity_threshold not in SEVERITY_LEVELS:
        raise ValueError(
            f"Unsupported severity_threshold: {severity_threshold!r}. Supported: {SEVERITY_LEVELS}"
        )

    _require_tool(tool, purpose="running a vulnerability scan")

    resolved_path = Path(project_path)
    logger.info("scan.vulnerabilities.start", tool=tool, project_path=str(resolved_path))

    if tool == "grype":
        cmd = ["grype", f"dir:{resolved_path}", "-o", "json"]
        allowed_returncodes = frozenset({0})
        stdout = _run_tool(
            cmd,
            cwd=str(resolved_path),
            timeout=timeout,
            tool_name=tool,
            allowed_returncodes=allowed_returncodes,
        )
        counts, findings = _parse_grype(stdout)
    else:
        # osv-scanner exits 1 (not an error) when vulnerabilities are found.
        cmd = ["osv-scanner", "--format", "json", "--recursive", str(resolved_path)]
        allowed_returncodes = frozenset({0, 1})
        stdout = _run_tool(
            cmd,
            cwd=str(resolved_path),
            timeout=timeout,
            tool_name=tool,
            allowed_returncodes=allowed_returncodes,
        )
        counts, findings = _parse_osv_scanner(stdout)

    if severity_threshold is not None:
        threshold_rank = SEVERITY_RANK[severity_threshold]
        findings = [
            f
            for f in findings
            if SEVERITY_RANK.get(f.severity, SEVERITY_RANK["unknown"]) >= threshold_rank
        ]

    total = sum(counts.values())
    logger.info("scan.vulnerabilities.end", tool=tool, total=total)
    return ScanResult(tool=tool, total=total, by_severity=counts, findings=findings)


# ---------------------------------------------------------------------------
# Public API — SBOM generation
# ---------------------------------------------------------------------------


def generate_sbom(
    project_path: str | Path,
    *,
    format: str = "cyclonedx",
    output_path: str | Path | None = None,
    timeout: int = _DEFAULT_SCAN_TIMEOUT,
) -> str:
    """Generate a Software Bill of Materials for *project_path* via `syft`.

    `format` is `"cyclonedx"` (default, CycloneDX JSON) or `"spdx"` (SPDX
    JSON). Always returns the SBOM document as a string; additionally writes
    it to `output_path` when given. Raises `ValueError` for an unsupported
    `format`, `RuntimeError` if `syft` isn't on `PATH` or exits unexpectedly.
    """
    if format not in _SBOM_FORMATS:
        raise ValueError(f"Unsupported SBOM format: {format!r}. Supported: {sorted(_SBOM_FORMATS)}")

    _require_tool("syft", purpose="generating an SBOM")

    resolved_path = Path(project_path)
    syft_format = _SBOM_FORMATS[format]
    logger.info("scan.sbom.start", format=format, project_path=str(resolved_path))

    cmd = ["syft", f"dir:{resolved_path}", "-o", syft_format]
    sbom = _run_tool(
        cmd,
        cwd=str(resolved_path),
        timeout=timeout,
        tool_name="syft",
        allowed_returncodes=frozenset({0}),
    )

    if output_path is not None:
        resolved_output = Path(output_path)
        resolved_output.parent.mkdir(parents=True, exist_ok=True)
        try:
            resolved_output.write_text(sbom)
        except OSError as exc:
            raise RuntimeError(
                f"failed to write SBOM to {output_path}: {type(exc).__name__}"
            ) from exc

    logger.info(
        "scan.sbom.end", format=format, output_path=str(output_path) if output_path else None
    )
    return sbom


# ---------------------------------------------------------------------------
# Public API — license compliance
# ---------------------------------------------------------------------------

_LICENSE_TOOLS = frozenset({"syft"})


@dataclass(frozen=True)
class ComponentLicense:
    """One component's parsed license data — structured fields only.

    `licenses` is a tuple (not a list) so this dataclass stays hashable given
    `frozen=True`. A component with no license data attached in the SBOM
    reports `("UNKNOWN",)`, never an empty tuple, so downstream allowlist
    checks always have something concrete to compare against.
    """

    package: str
    version: str
    licenses: tuple[str, ...]


@dataclass(frozen=True)
class LicenseResult:
    """Structured license-compliance outcome for a project's dependency tree.

    `components` is the full inventory (always populated, even when no
    `allowed`/`denied` gate is configured — useful for `hivepilot scan
    licenses` with no gate at all). `violations` is the subset of
    `components` that violate the configured gate; empty when no gate is
    configured. `error` is populated only for the "SBOM produced but could
    not be parsed" case, deliberately generic (never the raw SBOM text).
    """

    tool: str
    total: int
    components: list[ComponentLicense] = field(default_factory=list)
    violations: list[ComponentLicense] = field(default_factory=list)
    error: str | None = None


_SPDX_EXPR_SPLIT_RE = re.compile(r"\s+(?:OR|AND|WITH)\s+")


def _tokenize_spdx_expression(expression: str) -> tuple[str, ...]:
    """Split a (possibly compound) SPDX license expression into individual
    license-id tokens, stripping enclosing parentheses from each token.

    `"MIT OR GPL-3.0"` -> `("MIT", "GPL-3.0")`. `"(MIT AND Apache-2.0) WITH
    Classpath-exception-2.0"` -> `("MIT", "Apache-2.0",
    "Classpath-exception-2.0")`. A simple (non-compound) expression returns
    a single-element tuple.

    This exists so DENY-mode matching (`_license_matches_any`) catches a
    denied license hidden inside an OR/AND/WITH expression -- e.g.
    `denied=["GPL-3.0"]` must flag `"MIT OR GPL-3.0"` even though the whole
    expression is not itself `"GPL-3.0"`. The fail-closed consequence for
    ALLOW-mode (`_license_all_allowed`) is that every operand of a compound
    expression must be individually allowlisted -- an `OR` is NOT treated
    as "any operand suffices" (see `check_licenses` docstring).
    """
    tokens = [
        stripped
        for raw_token in _SPDX_EXPR_SPLIT_RE.split(expression)
        if (stripped := raw_token.strip().strip("()").strip())
    ]
    return tuple(tokens) if tokens else (expression,)


def _extract_component_licenses(raw_licenses: Any) -> tuple[str, ...]:
    """Parse a CycloneDX component's `licenses` array.

    Each entry is either `{"license": {"id": "MIT"}}`,
    `{"license": {"name": "Some License"}}`, or `{"expression": "MIT OR
    Apache-2.0"}` (a compound SPDX expression, tokenized via
    `_tokenize_spdx_expression` into one entry per operand). A component
    with no usable license entries reports `("UNKNOWN",)`.
    """
    if not isinstance(raw_licenses, list):
        return ("UNKNOWN",)

    ids: list[str] = []
    for entry in raw_licenses:
        if not isinstance(entry, dict):
            continue
        license_obj = entry.get("license")
        if isinstance(license_obj, dict):
            value = license_obj.get("id") or license_obj.get("name")
            if value:
                ids.append(str(value))
            continue
        expression = entry.get("expression")
        if expression:
            ids.extend(_tokenize_spdx_expression(str(expression)))

    return tuple(ids) if ids else ("UNKNOWN",)


def _license_matches_any(licenses: tuple[str, ...], candidates: set[str]) -> bool:
    """True if ANY of *licenses* matches an entry in *candidates* (deny-mode
    check: any denied license present is a violation)."""
    return any(lic.upper() in candidates for lic in licenses)


def _license_all_allowed(licenses: tuple[str, ...], allowed: set[str]) -> bool:
    """True only if EVERY one of *licenses* is in *allowed* (allowlist-mode
    check: a single disallowed/unlisted license — including "UNKNOWN" when
    it isn't itself in `allowed` — makes the whole component a violation)."""
    return all(lic.upper() in allowed for lic in licenses)


def check_licenses(
    project_path: str | Path,
    *,
    allowed: list[str] | None = None,
    denied: list[str] | None = None,
    tool: str = "syft",
    timeout: int = _DEFAULT_SCAN_TIMEOUT,
) -> LicenseResult:
    """Check *project_path*'s dependency licenses against an optional
    allow/deny gate, deriving license data from the existing
    `generate_sbom` CycloneDX-JSON output (no separate scanner tool).

    Matching is case-insensitive (`.upper()` on both sides), but the
    original license string is always preserved in the returned
    `ComponentLicense.licenses`. Gate semantics:

    * `denied` set → a component violates if ANY of its licenses matches a
      denied id.
    * `allowed` set (and not denied-matched) → a component violates if ANY
      of its licenses is NOT in `allowed` (an unlisted license — including
      `"UNKNOWN"` — is a violation).
    * Both set → deny takes precedence (a denied license is always a
      violation, even if it's also in `allowed`).
    * Neither set → no violations; `components` still reports the full
      inventory.

    SPDX compound expressions (`{"expression": "MIT OR GPL-3.0"}`) are
    tokenized into one entry per operand (see `_tokenize_spdx_expression`)
    rather than kept as a single literal string. This means DENY matches
    any operand (fail-closed: a denied license hidden inside an OR/AND/WITH
    expression is still caught), while ALLOW requires EVERY operand to be
    individually allowlisted — an `OR` is deliberately NOT treated as "any
    operand suffices", which is the more conservative (also fail-closed)
    reading for an allowlist gate.

    Raises `ValueError` for an unsupported `tool`. Propagates whatever
    `generate_sbom` raises for a missing `syft` binary, a timeout, or an
    unexpected exit code (fail-closed, matching `scan_vulnerabilities`). A
    malformed SBOM (JSON parse failure) does not raise — it returns a
    `LicenseResult` with `error` set to a generic message, never the raw SBOM
    text. Callers that gate on this result (the orchestrator's license gate)
    MUST check `error` before `violations` — an unparseable SBOM always has
    an empty `violations` list and must never be read as "no violations
    found".
    """
    if tool not in _LICENSE_TOOLS:
        raise ValueError(
            f"Unsupported license scan tool: {tool!r}. Supported: {sorted(_LICENSE_TOOLS)}"
        )

    resolved_path = Path(project_path)
    logger.info("scan.licenses.start", tool=tool, project_path=str(resolved_path))

    sbom = generate_sbom(resolved_path, format="cyclonedx", timeout=timeout)

    try:
        data = json.loads(sbom) if sbom.strip() else {}
    except json.JSONDecodeError:
        logger.error("scan.licenses.sbom_parse_failed", tool=tool)
        return LicenseResult(tool=tool, total=0, error="SBOM parse failed")

    components: list[ComponentLicense] = []
    for raw_component in data.get("components", []) or []:
        if not isinstance(raw_component, dict):
            continue
        components.append(
            ComponentLicense(
                package=raw_component.get("name", "unknown"),
                version=raw_component.get("version", "unknown"),
                licenses=_extract_component_licenses(raw_component.get("licenses")),
            )
        )

    denied_upper = {d.upper() for d in denied} if denied else None
    allowed_upper = {a.upper() for a in allowed} if allowed else None

    violations: list[ComponentLicense] = []
    for component in components:
        if denied_upper and _license_matches_any(component.licenses, denied_upper):
            violations.append(component)
        elif allowed_upper and not _license_all_allowed(component.licenses, allowed_upper):
            violations.append(component)

    logger.info("scan.licenses.end", tool=tool, total=len(components), violations=len(violations))
    return LicenseResult(
        tool=tool, total=len(components), components=components, violations=violations
    )


def has_license_violations(result: LicenseResult) -> bool:
    """True if *result* has at least one component violating the gate."""
    return bool(result.violations)
