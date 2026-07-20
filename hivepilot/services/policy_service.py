from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from hivepilot.config import settings
from hivepilot.services import scan_service
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)


@dataclass
class Policy:
    allow_auto_git: bool = True
    require_approval: bool = False
    allow_containers: bool = True
    role_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    allowed_runners: list[str] | None = None
    # Behaviour when a ${secret:NAME} reference cannot be resolved:
    #   "closed"   — abort the step/run (default, fail-safe).
    #   "fallback" — try env/file providers keyed by NAME, else abort.
    # Any value other than "fallback" is treated as "closed".
    secrets_fail_mode: str = "closed"
    # Phase 21 Sprint 2 — pipeline CVE gate. `None` (default) means no gate:
    # `Orchestrator._run_task_body` never calls `scan_service.scan_vulnerabilities`
    # and behaviour is byte-identical to before this sprint. When set, it must
    # be one of `scan_service.SEVERITY_LEVELS` — validated eagerly in
    # `get_policy` below so a typo in policies.yaml fails loudly at load time
    # instead of silently never gating (fail-closed).
    block_on_severity: str | None = None
    # Scanner backend for the CVE gate: "grype" (default) or "osv-scanner".
    # Not eagerly validated here — an unsupported value surfaces as a
    # `ValueError` from `scan_service.scan_vulnerabilities` itself, which the
    # orchestrator's CVE gate treats the same as any other scan failure
    # (fail-closed: block the run, never fail-open).
    scan_tool: str = "grype"
    # License-compliance gate (Phase 21). `None`/`None` (both unset, the
    # default) means no gate: `Orchestrator._run_task_body` never calls
    # `scan_service.check_licenses` and behaviour is byte-identical to
    # before this sprint. When either is set, each entry is validated
    # eagerly in `get_policy` below (must be a non-empty string) so a typo
    # in policies.yaml fails loudly at load time instead of silently never
    # gating (fail-closed) — mirrors `block_on_severity`'s validation.
    denied_licenses: list[str] | None = None
    allowed_licenses: list[str] | None = None
    # Scanner backend for the license gate: "syft" is the only supported
    # value today (license data is derived from the SBOM `generate_sbom`
    # already produces). Not eagerly validated here — an unsupported value
    # surfaces as a `ValueError` from `scan_service.check_licenses` itself,
    # which the orchestrator's license gate treats like any other scan
    # failure (fail-closed).
    license_scan_tool: str = "syft"


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def load_policies(path: Path | None = None) -> dict:
    resolved = settings.resolve_config_path(path or settings.policies_file)
    return _load_yaml(resolved)


def reload_policies() -> None:
    """Invalidate the policies cache so the next call re-reads the file."""
    _cache.clear()


# Internal cache — populated on first use, cleared by reload_policies()
_cache: dict = {}


def _get_policies() -> dict:
    if "data" not in _cache:
        raw = load_policies(settings.policies_file)
        # policies.yaml nests default/projects under a top-level "policies" key.
        _cache["data"] = raw.get("policies", raw)
    return _cache["data"]


def _validate_license_list(value: object, *, field_name: str, project_name: str) -> None:
    """Fail-closed at load time: `value` must be a NON-EMPTY list of
    non-empty strings, or unset entirely — mirrors `block_on_severity`'s
    eager validation so a typo in policies.yaml never silently leaves the
    license gate un-enforced.

    An empty list (`[]`) is deliberately REJECTED rather than treated as
    "no entries" — it is ambiguous and, for `allowed_licenses`, actively
    dangerous: `[]` is falsy, so the orchestrator's gate-enable check
    (`policy.denied_licenses or policy.allowed_licenses`) would silently
    treat it as "gate disabled" instead of its literal meaning ("nothing is
    allowed -> block every run"). Forcing the operator to omit the key
    entirely to disable the gate removes that ambiguity.
    """
    if value is None:
        return
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(entry, str) and entry.strip() for entry in value)
    ):
        raise ValueError(
            f"Invalid policy {field_name!r} for project {project_name!r}: {value!r}. "
            "Must be a NON-EMPTY list of non-empty strings, or omit the key to disable "
            "the gate (an empty list is ambiguous: an empty allowlist would block all "
            "runs, an empty denylist is a no-op)."
        )


def get_policy(project_name: str) -> Policy:
    policies = _get_policies()
    project_rules = policies.get("projects", {}).get(project_name) if policies else None
    default = policies.get("default", {}) if policies else {}
    rules = {**default, **(project_rules or {})}
    block_on_severity = rules.get("block_on_severity")
    if block_on_severity is not None and block_on_severity not in scan_service.SEVERITY_LEVELS:
        # Fail-closed at load time: a mistyped severity must never be
        # silently ignored (which would leave the CVE gate un-enforced).
        raise ValueError(
            f"Invalid policy 'block_on_severity' for project {project_name!r}: "
            f"{block_on_severity!r}. Must be one of {scan_service.SEVERITY_LEVELS} or unset."
        )
    denied_licenses = rules.get("denied_licenses")
    _validate_license_list(denied_licenses, field_name="denied_licenses", project_name=project_name)
    allowed_licenses = rules.get("allowed_licenses")
    _validate_license_list(
        allowed_licenses, field_name="allowed_licenses", project_name=project_name
    )
    return Policy(
        allow_auto_git=rules.get("allow_auto_git", True),
        require_approval=rules.get("require_approval", False),
        allow_containers=rules.get("allow_containers", True),
        role_overrides=rules.get("role_overrides", {}) or {},
        allowed_runners=rules.get("allowed_runners"),
        secrets_fail_mode=rules.get("secrets_fail_mode", "closed"),
        block_on_severity=block_on_severity,
        scan_tool=rules.get("scan_tool", "grype"),
        denied_licenses=denied_licenses,
        allowed_licenses=allowed_licenses,
        license_scan_tool=rules.get("license_scan_tool", "syft"),
    )


def enforce_policy(project_name: str, *, auto_git: bool) -> Policy:
    policy = get_policy(project_name)
    if auto_git and not policy.allow_auto_git:
        raise RuntimeError(f"Auto-git is disabled by policy for project {project_name}")
    return policy
