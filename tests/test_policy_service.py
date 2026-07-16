"""Tests for the secrets_fail_mode addition to Policy loading."""

from __future__ import annotations

import pytest
import yaml

from hivepilot.services import policy_service


def _write_policies(body: str, monkeypatch: pytest.MonkeyPatch) -> None:
    parsed = yaml.safe_load(body)
    monkeypatch.setattr(policy_service, "load_policies", lambda *a, **k: parsed)
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
