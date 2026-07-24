"""Tests for the `/v1/memory/*` endpoints (memory-quality instrumentation
subsystem backing Mirador's "Réalité" view).

Mirrors the auth/tenant-isolation/empty-state patterns established for the
`/v1/analytics/*` endpoints in `test_api_service.py` — every read endpoint
requires `Depends(require_role("read"))` and is scoped to the caller's
tenant (admin: unfiltered); the one write endpoint
(`POST /v1/memory/evaluations`) requires `Depends(require_role("run"))`
and always records for the caller's OWN tenant.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hivepilot.services.token_service import add_token


@pytest.fixture()
def tmp_tokens_file(tmp_path, monkeypatch):
    import yaml

    tokens_file = tmp_path / "tokens.yaml"
    tokens_file.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "tokens_file", tokens_file)
    return tokens_file


@pytest.fixture()
def api_client():
    from hivepilot.services.api_service import app

    return TestClient(app, raise_server_exceptions=True)


def _auth(raw_token: str) -> dict:
    return {"Authorization": f"Bearer {raw_token}"}


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestMemoryEndpointsAuth:
    @pytest.mark.parametrize(
        "path",
        [
            "/v1/memory/reality",
            "/v1/memory/gaps",
            "/v1/memory/evaluations",
            "/v1/memory/journal",
        ],
    )
    def test_get_endpoints_require_auth(self, api_client, path):
        resp = api_client.get(path)
        assert resp.status_code == 401

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/memory/reality",
            "/v1/memory/gaps",
            "/v1/memory/evaluations",
            "/v1/memory/journal",
        ],
    )
    def test_get_endpoints_reject_unrecognized_role(self, api_client, tmp_tokens_file, path):
        raw, _ = add_token("bogus-role")
        resp = api_client.get(path, headers=_auth(raw))
        assert resp.status_code == 403

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/memory/reality",
            "/v1/memory/gaps",
            "/v1/memory/evaluations",
            "/v1/memory/journal",
        ],
    )
    def test_get_endpoints_allow_read_role(self, api_client, tmp_tokens_file, path):
        raw, _ = add_token("read")
        resp = api_client.get(path, headers=_auth(raw))
        assert resp.status_code == 200

    def test_post_evaluation_requires_auth(self, api_client):
        resp = api_client.post("/v1/memory/evaluations", json={"namespace": "ns", "useful": True})
        assert resp.status_code == 401

    def test_post_evaluation_rejects_read_role(self, api_client, tmp_tokens_file):
        """Read-only role must not be able to write an evaluation."""
        raw, _ = add_token("read")
        resp = api_client.post(
            "/v1/memory/evaluations",
            json={"namespace": "ns", "useful": True},
            headers=_auth(raw),
        )
        assert resp.status_code == 403

    def test_post_evaluation_allows_run_role(self, api_client, tmp_tokens_file):
        raw, _ = add_token("run")
        resp = api_client.post(
            "/v1/memory/evaluations",
            json={"namespace": "ns", "useful": True},
            headers=_auth(raw),
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Empty state — zeros/[] not 500.
# ---------------------------------------------------------------------------


class TestMemoryEndpointsEmptyState:
    def test_reality_empty_is_zeros(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/memory/reality", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_searches"] == 0
        assert data["search_success_rate"] == 0.0
        assert data["declared_reliability"] == 0.0
        assert data["total_evaluations"] == 0

    def test_gaps_empty_is_empty_list(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/memory/gaps", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["gaps"] == []

    def test_evaluations_empty_is_empty_list(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/memory/evaluations", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["evaluations"] == []

    def test_journal_empty_is_empty_list(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/memory/journal", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["journal"] == []


# ---------------------------------------------------------------------------
# Tenant isolation — the security-critical invariant.
# ---------------------------------------------------------------------------


class TestMemoryEndpointsTenantIsolation:
    def test_reality_scoped_to_caller_tenant(self, api_client, tmp_tokens_file):
        from hivepilot.services import memory_service

        memory_service.record_search(
            namespace="ns", query="q", result_count=1, actor="x", tenant="acme"
        )
        memory_service.record_search(
            namespace="ns", query="q", result_count=1, actor="x", tenant="other"
        )

        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/memory/reality", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["total_searches"] == 1

    def test_reality_admin_sees_all_tenants(self, api_client, tmp_tokens_file):
        from hivepilot.services import memory_service

        memory_service.record_search(
            namespace="ns", query="q", result_count=1, actor="x", tenant="acme"
        )
        memory_service.record_search(
            namespace="ns", query="q", result_count=1, actor="x", tenant="other"
        )

        raw, _ = add_token("admin")
        resp = api_client.get("/v1/memory/reality", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["total_searches"] == 2

    def test_gaps_scoped_to_caller_tenant(self, api_client, tmp_tokens_file):
        from hivepilot.services import memory_service

        memory_service.record_search(
            namespace="ns", query="q", result_count=0, actor="x", tenant="acme"
        )
        memory_service.record_search(
            namespace="ns", query="q", result_count=0, actor="x", tenant="other"
        )

        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/memory/gaps", headers=_auth(raw))
        assert resp.status_code == 200
        gaps = resp.json()["gaps"]
        assert len(gaps) == 1
        assert gaps[0]["no_result_count"] == 1

    def test_journal_scoped_to_caller_tenant(self, api_client, tmp_tokens_file):
        from hivepilot.services import memory_service

        memory_service.record_store(namespace="ns", key="k", actor="x", tenant="acme")
        memory_service.record_store(namespace="ns", key="k", actor="x", tenant="other")

        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/memory/journal", headers=_auth(raw))
        assert resp.status_code == 200
        assert len(resp.json()["journal"]) == 1

    def test_evaluations_scoped_to_caller_tenant(self, api_client, tmp_tokens_file):
        from hivepilot.services import memory_service

        memory_service.record_evaluation(namespace="ns", useful=True, actor="h", tenant="acme")
        memory_service.record_evaluation(namespace="ns", useful=True, actor="h", tenant="other")

        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/memory/evaluations", headers=_auth(raw))
        assert resp.status_code == 200
        assert len(resp.json()["evaluations"]) == 1

    def test_post_evaluation_never_lets_caller_choose_another_tenant(
        self, api_client, tmp_tokens_file
    ):
        """The request body has no `tenant` field at all — an evaluation is
        ALWAYS recorded for the caller's own token tenant, never a
        caller-supplied one."""
        from hivepilot.services import memory_service

        raw, _ = add_token("run", tenant="acme")
        resp = api_client.post(
            "/v1/memory/evaluations",
            json={"namespace": "ns", "useful": True},
            headers=_auth(raw),
        )
        assert resp.status_code == 200

        acme_evals = memory_service.recent_evaluations(tenant="acme", limit=10)
        other_evals = memory_service.recent_evaluations(tenant="other", limit=10)
        assert len(acme_evals) == 1
        assert other_evals == []


# ---------------------------------------------------------------------------
# POST /v1/memory/evaluations — validation
# ---------------------------------------------------------------------------


class TestPostMemoryEvaluationValidation:
    def test_empty_namespace_rejected(self, api_client, tmp_tokens_file):
        raw, _ = add_token("run")
        resp = api_client.post(
            "/v1/memory/evaluations",
            json={"namespace": "", "useful": True},
            headers=_auth(raw),
        )
        assert resp.status_code == 422

    def test_missing_useful_rejected(self, api_client, tmp_tokens_file):
        raw, _ = add_token("run")
        resp = api_client.post(
            "/v1/memory/evaluations",
            json={"namespace": "ns"},
            headers=_auth(raw),
        )
        assert resp.status_code == 422

    def test_valid_body_with_optional_fields_recorded(self, api_client, tmp_tokens_file):
        from hivepilot.services import memory_service

        raw, _ = add_token("run", tenant="acme")
        resp = api_client.post(
            "/v1/memory/evaluations",
            json={"namespace": "ns", "useful": False, "ref_key": "k1", "note": "stale"},
            headers=_auth(raw),
        )
        assert resp.status_code == 200

        evals = memory_service.recent_evaluations(tenant="acme", limit=10)
        assert len(evals) == 1
        assert evals[0]["namespace"] == "ns"
        assert evals[0]["useful"] is False
        assert evals[0]["ref_key"] == "k1"
        assert evals[0]["note"] == "stale"


# ---------------------------------------------------------------------------
# Unversioned routes also registered (matches the analytics precedent).
# ---------------------------------------------------------------------------


class TestMemoryUnversionedRoutes:
    def test_unversioned_reality_route_also_registered(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/memory/reality", headers=_auth(raw))
        assert resp.status_code == 200
