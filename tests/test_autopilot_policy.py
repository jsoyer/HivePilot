"""Tests for hivepilot.services.autopilot_policy — the auto_dispatch
allowlist + budget_daily_usd policy surface, and its disabled-by-default /
fail-closed merge behavior.
"""

from __future__ import annotations

from typing import Any

import pytest

from hivepilot.services import autopilot_policy, policy_service


@pytest.fixture(autouse=True)
def _reset_policy_cache():
    """policy_service.get_policy() is backed by a module-level cache that
    only clears via reload_policies() — without this, monkeypatching
    load_policies in one test would leak into the next test's assertions."""
    policy_service.reload_policies()
    yield
    policy_service.reload_policies()


def _patch_policies(monkeypatch: pytest.MonkeyPatch, data: dict[str, Any]) -> None:
    monkeypatch.setattr(policy_service, "load_policies", lambda *a, **kw: {"policies": data})


class TestGetAutopilotPolicy:
    def test_no_policies_file_disabled_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_policies(monkeypatch, {})
        result = autopilot_policy.get_autopilot_policy("acme-api")
        assert result.auto_dispatch == []
        assert result.budget_daily_usd is None
        # require_approval falls back to policy_service.Policy's own
        # default (False) when no project/default block exists at all —
        # the autopilot gate itself is what turns "no config" into a deny,
        # not this loader.
        assert result.require_approval is False

    def test_project_auto_dispatch_and_budget_resolved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_policies(
            monkeypatch,
            {
                "default": {"require_approval": False},
                "projects": {
                    "acme-api": {
                        "auto_dispatch": ["groomer-pipeline"],
                        "budget_daily_usd": 5.0,
                        "require_approval": False,
                    }
                },
            },
        )
        result = autopilot_policy.get_autopilot_policy("acme-api")
        assert result.auto_dispatch == ["groomer-pipeline"]
        assert result.budget_daily_usd == 5.0
        assert result.require_approval is False

    def test_project_without_auto_dispatch_block_is_empty_allowlist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_policies(
            monkeypatch,
            {
                "default": {},
                "projects": {"acme-api": {"require_approval": False}},
            },
        )
        result = autopilot_policy.get_autopilot_policy("acme-api")
        assert result.auto_dispatch == []
        assert result.budget_daily_usd is None

    def test_require_approval_true_is_carried_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_policies(
            monkeypatch,
            {
                "projects": {
                    "guarded-project": {
                        "auto_dispatch": ["p"],
                        "budget_daily_usd": 10.0,
                        "require_approval": True,
                    }
                }
            },
        )
        result = autopilot_policy.get_autopilot_policy("guarded-project")
        assert result.require_approval is True

    def test_malformed_auto_dispatch_becomes_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_policies(
            monkeypatch,
            {"projects": {"acme-api": {"auto_dispatch": "not-a-list", "budget_daily_usd": 5.0}}},
        )
        result = autopilot_policy.get_autopilot_policy("acme-api")
        assert result.auto_dispatch == []

    def test_malformed_budget_becomes_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_policies(
            monkeypatch,
            {
                "projects": {
                    "acme-api": {"auto_dispatch": ["p"], "budget_daily_usd": "not-a-number"}
                }
            },
        )
        result = autopilot_policy.get_autopilot_policy("acme-api")
        assert result.budget_daily_usd is None

    def test_default_block_merges_under_project_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_policies(
            monkeypatch,
            {
                "default": {"budget_daily_usd": 1.0, "auto_dispatch": ["default-pipeline"]},
                "projects": {"acme-api": {"budget_daily_usd": 9.0}},
            },
        )
        result = autopilot_policy.get_autopilot_policy("acme-api")
        # Project override wins for budget_daily_usd, but auto_dispatch is
        # inherited from default since the project block doesn't set it.
        assert result.budget_daily_usd == 9.0
        assert result.auto_dispatch == ["default-pipeline"]

    def test_does_not_mutate_policy_service_module(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Sanity: this module must never monkeypatch/replace anything on
        policy_service itself — it only reads from it."""
        original_get_policy = policy_service.get_policy
        _patch_policies(monkeypatch, {"projects": {"acme-api": {"auto_dispatch": ["p"]}}})
        autopilot_policy.get_autopilot_policy("acme-api")
        assert policy_service.get_policy is original_get_policy
