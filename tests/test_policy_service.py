"""Tests for the secrets_fail_mode addition to Policy loading."""

from __future__ import annotations

import pytest
import yaml

from hivepilot.services import policy_service


def _write_policies(body: str, monkeypatch: pytest.MonkeyPatch) -> None:
    parsed = yaml.safe_load(body)
    monkeypatch.setattr(policy_service, "load_policies", lambda *a, **k: parsed)
    policy_service.reload_policies()


@pytest.fixture(autouse=True)
def _reset_policy_cache_after_test():
    """`policy_service._cache` is a process-global dict populated lazily by
    `_get_policies()`; a test that mocks `load_policies` (via
    `_write_policies` above, including a "raises at load" test that leaves a
    deliberately-invalid policy cached the moment before it raises) would
    otherwise leak that cached data into whichever test runs next in the
    same process -- including tests in OTHER modules that call the real,
    unmocked `get_policy()` -- if this module happens to run before them.
    Clearing the cache after every test here guarantees the next caller
    (in this module or any other) re-reads from the real config."""
    yield
    policy_service.reload_policies()


def test_default_secrets_fail_mode_is_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _write_policies(
        "policies:\n  default:\n    allow_auto_git: true\n  projects:\n    p: {}\n",
        monkeypatch,
    )
    policy = policy_service.get_policy("p")
    assert policy.secrets_fail_mode == "closed"


def test_project_can_opt_into_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _write_policies(
        (
            "policies:\n"
            "  default:\n    allow_auto_git: true\n"
            "  projects:\n"
            "    p:\n      secrets_fail_mode: fallback\n"
        ),
        monkeypatch,
    )
    policy = policy_service.get_policy("p")
    assert policy.secrets_fail_mode == "fallback"


def test_dataclass_default_is_closed() -> None:
    assert policy_service.Policy().secrets_fail_mode == "closed"


# ---------------------------------------------------------------------------
# Phase 21 Sprint 2 -- pipeline CVE gate (block_on_severity / scan_tool)
# ---------------------------------------------------------------------------


def test_block_on_severity_defaults_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opt-in gate: unset in policies.yaml means no gate at all (byte-identical
    behaviour to before this sprint)."""
    _write_policies(
        "policies:\n  default:\n    allow_auto_git: true\n  projects:\n    p: {}\n",
        monkeypatch,
    )
    policy = policy_service.get_policy("p")
    assert policy.block_on_severity is None
    assert policy.scan_tool == "grype"


def test_project_can_set_block_on_severity(monkeypatch: pytest.MonkeyPatch) -> None:
    _write_policies(
        (
            "policies:\n"
            "  default:\n    allow_auto_git: true\n"
            "  projects:\n"
            "    p:\n      block_on_severity: critical\n      scan_tool: osv-scanner\n"
        ),
        monkeypatch,
    )
    policy = policy_service.get_policy("p")
    assert policy.block_on_severity == "critical"
    assert policy.scan_tool == "osv-scanner"


def test_invalid_block_on_severity_raises_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail-closed: a typo'd severity must error loudly at load time, not be
    silently ignored (which would leave the CVE gate un-enforced)."""
    _write_policies(
        (
            "policies:\n"
            "  default:\n    allow_auto_git: true\n"
            "  projects:\n"
            "    p:\n      block_on_severity: super-critical\n"
        ),
        monkeypatch,
    )
    with pytest.raises(ValueError, match="block_on_severity"):
        policy_service.get_policy("p")


def test_dataclass_defaults_for_cve_gate_fields() -> None:
    policy = policy_service.Policy()
    assert policy.block_on_severity is None
    assert policy.scan_tool == "grype"


# ---------------------------------------------------------------------------
# License compliance (Phase 21 -- license-compliance sprint):
# denied_licenses / allowed_licenses / license_scan_tool
# ---------------------------------------------------------------------------


def test_license_fields_default_to_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _write_policies(
        "policies:\n  default:\n    allow_auto_git: true\n  projects:\n    p: {}\n",
        monkeypatch,
    )
    policy = policy_service.get_policy("p")
    assert policy.denied_licenses is None
    assert policy.allowed_licenses is None
    assert policy.license_scan_tool == "syft"


def test_project_can_set_license_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _write_policies(
        (
            "policies:\n"
            "  default:\n    allow_auto_git: true\n"
            "  projects:\n"
            "    p:\n"
            "      denied_licenses: [GPL-3.0]\n"
            "      allowed_licenses: [MIT, Apache-2.0]\n"
            "      license_scan_tool: syft\n"
        ),
        monkeypatch,
    )
    policy = policy_service.get_policy("p")
    assert policy.denied_licenses == ["GPL-3.0"]
    assert policy.allowed_licenses == ["MIT", "Apache-2.0"]
    assert policy.license_scan_tool == "syft"


def test_invalid_denied_licenses_entry_raises_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    _write_policies(
        (
            "policies:\n"
            "  default:\n    allow_auto_git: true\n"
            "  projects:\n"
            "    p:\n      denied_licenses: ['']\n"
        ),
        monkeypatch,
    )
    with pytest.raises(ValueError, match="denied_licenses"):
        policy_service.get_policy("p")


def test_invalid_allowed_licenses_non_list_raises_at_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_policies(
        (
            "policies:\n"
            "  default:\n    allow_auto_git: true\n"
            "  projects:\n"
            "    p:\n      allowed_licenses: MIT\n"
        ),
        monkeypatch,
    )
    with pytest.raises(ValueError, match="allowed_licenses"):
        policy_service.get_policy("p")


def test_empty_denied_licenses_list_raises_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty list is ambiguous/dangerous, not merely "no entries" -- it
    must be rejected the same as a malformed entry, never silently accepted
    as a no-op gate."""
    _write_policies(
        (
            "policies:\n"
            "  default:\n    allow_auto_git: true\n"
            "  projects:\n"
            "    p:\n      denied_licenses: []\n"
        ),
        monkeypatch,
    )
    with pytest.raises(ValueError, match="denied_licenses"):
        policy_service.get_policy("p")


def test_empty_allowed_licenses_list_raises_at_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty `allowed_licenses: []` is especially dangerous: `[]` is
    falsy, so if it were accepted the orchestrator's gate-enable check
    (`denied_licenses or allowed_licenses`) would silently treat "allow
    nothing" as "gate disabled" -- the opposite of the intended meaning."""
    _write_policies(
        (
            "policies:\n"
            "  default:\n    allow_auto_git: true\n"
            "  projects:\n"
            "    p:\n      allowed_licenses: []\n"
        ),
        monkeypatch,
    )
    with pytest.raises(ValueError, match="allowed_licenses"):
        policy_service.get_policy("p")


def test_dataclass_defaults_for_license_gate_fields() -> None:
    policy = policy_service.Policy()
    assert policy.denied_licenses is None
    assert policy.allowed_licenses is None
    assert policy.license_scan_tool == "syft"
