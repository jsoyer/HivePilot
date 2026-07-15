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
