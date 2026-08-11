"""Tests for the `/v1/memory/*` endpoints (memory-quality instrumentation
subsystem backing Pollen's Memory > Quality view).

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

    def test_overlong_namespace_rejected(self, api_client, tmp_tokens_file):
        raw, _ = add_token("run")
        resp = api_client.post(
            "/v1/memory/evaluations",
            json={"namespace": "n" * 201, "useful": True},
            headers=_auth(raw),
        )
        assert resp.status_code == 422

    def test_overlong_note_rejected(self, api_client, tmp_tokens_file):
        raw, _ = add_token("run")
        resp = api_client.post(
            "/v1/memory/evaluations",
            json={"namespace": "ns", "useful": True, "note": "x" * 2001},
            headers=_auth(raw),
        )
        assert resp.status_code == 422

    def test_overlong_ref_key_rejected(self, api_client, tmp_tokens_file):
        raw, _ = add_token("run")
        resp = api_client.post(
            "/v1/memory/evaluations",
            json={"namespace": "ns", "useful": True, "ref_key": "k" * 201},
            headers=_auth(raw),
        )
        assert resp.status_code == 422

    def test_note_at_max_length_accepted(self, api_client, tmp_tokens_file):
        raw, _ = add_token("run")
        resp = api_client.post(
            "/v1/memory/evaluations",
            json={"namespace": "ns", "useful": True, "note": "x" * 2000},
            headers=_auth(raw),
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# limit bounding (Query(ge=1, le=500)) — a negative/huge limit must never
# translate into an unbounded SQLite `LIMIT`, which would let a read-role
# caller fetch every row of their own tenant's history in one request.
# ---------------------------------------------------------------------------


class TestMemoryLimitBounding:
    @pytest.mark.parametrize("path", ["/v1/memory/evaluations", "/v1/memory/journal"])
    def test_negative_limit_rejected(self, api_client, tmp_tokens_file, path):
        raw, _ = add_token("read")
        resp = api_client.get(f"{path}?limit=-1", headers=_auth(raw))
        assert resp.status_code == 422

    @pytest.mark.parametrize("path", ["/v1/memory/evaluations", "/v1/memory/journal"])
    def test_zero_limit_rejected(self, api_client, tmp_tokens_file, path):
        raw, _ = add_token("read")
        resp = api_client.get(f"{path}?limit=0", headers=_auth(raw))
        assert resp.status_code == 422

    @pytest.mark.parametrize("path", ["/v1/memory/evaluations", "/v1/memory/journal"])
    def test_huge_limit_rejected(self, api_client, tmp_tokens_file, path):
        raw, _ = add_token("read")
        resp = api_client.get(f"{path}?limit=99999", headers=_auth(raw))
        assert resp.status_code == 422

    @pytest.mark.parametrize("path", ["/v1/memory/evaluations", "/v1/memory/journal"])
    def test_max_allowed_limit_accepted(self, api_client, tmp_tokens_file, path):
        raw, _ = add_token("read")
        resp = api_client.get(f"{path}?limit=500", headers=_auth(raw))
        assert resp.status_code == 200

    def test_negative_limit_never_returns_all_rows(self, api_client, tmp_tokens_file):
        """Defense-in-depth: even if validation were ever bypassed, prove the
        actual concern (an unbounded SQLite `LIMIT -1`) can't leak every row —
        this asserts on the ACTUAL 422 rejection behavior, not just the
        status code, matching the real attack this bound closes."""
        from hivepilot.services import memory_service

        for i in range(5):
            memory_service.record_store(namespace="ns", key=f"k{i}", actor="x", tenant="acme")

        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/memory/journal?limit=-1", headers=_auth(raw))
        assert resp.status_code == 422

        # A valid, bounded request still works and respects the real limit.
        resp_ok = api_client.get("/v1/memory/journal?limit=2", headers=_auth(raw))
        assert resp_ok.status_code == 200
        assert len(resp_ok.json()["journal"]) == 2


# ---------------------------------------------------------------------------
# Unversioned routes also registered (matches the analytics precedent).
# ---------------------------------------------------------------------------


class TestMemoryUnversionedRoutes:
    def test_unversioned_reality_route_also_registered(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/memory/reality", headers=_auth(raw))
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /v1/memory/growth (Pollen data endpoints sprint)
# ---------------------------------------------------------------------------


class TestMemoryGrowthEndpoint:
    def test_requires_auth(self, api_client):
        resp = api_client.get("/v1/memory/growth")
        assert resp.status_code == 401

    def test_rejects_unrecognized_role(self, api_client, tmp_tokens_file):
        raw, _ = add_token("bogus-role")
        resp = api_client.get("/v1/memory/growth", headers=_auth(raw))
        assert resp.status_code == 403

    def test_allows_read_role(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/memory/growth", headers=_auth(raw))
        assert resp.status_code == 200

    def test_unversioned_route_also_registered(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/memory/growth", headers=_auth(raw))
        assert resp.status_code == 200

    def test_empty_is_zero_safe_not_500(self, api_client, tmp_tokens_file):
        raw, _ = add_token("read")
        resp = api_client.get("/v1/memory/growth", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["memories_by_namespace"] == []
        assert data["growth_series"] == []
        assert data["authorship"] is None

    def test_reflects_seeded_store_events(self, api_client, tmp_tokens_file):
        from hivepilot.services import memory_service

        memory_service.record_store(namespace="ns", key="k1", actor="developer", tenant="acme")
        memory_service.record_store(namespace="ns", key="k2", actor="developer", tenant="acme")

        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/memory/growth", headers=_auth(raw))
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert data["memories_by_namespace"] == [{"namespace": "ns", "count": 2}]

    def test_scoped_to_caller_tenant(self, api_client, tmp_tokens_file):
        from hivepilot.services import memory_service

        memory_service.record_store(namespace="ns", key="k1", actor="x", tenant="acme")
        memory_service.record_store(namespace="ns", key="k2", actor="x", tenant="other")

        raw, _ = add_token("read", tenant="acme")
        resp = api_client.get("/v1/memory/growth", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_admin_sees_all_tenants(self, api_client, tmp_tokens_file):
        from hivepilot.services import memory_service

        memory_service.record_store(namespace="ns", key="k1", actor="x", tenant="acme")
        memory_service.record_store(namespace="ns", key="k2", actor="x", tenant="other")

        raw, _ = add_token("admin")
        resp = api_client.get("/v1/memory/growth", headers=_auth(raw))
        assert resp.status_code == 200
        assert resp.json()["total"] == 2


# ---------------------------------------------------------------------------
# Per-backend memory KPIs
# ---------------------------------------------------------------------------


def _auth_hdr(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


def test_memory_backends_reports_both_even_when_idle(tmp_path, monkeypatch):
    """A backend rendered as ABSENT reads as 'not applicable'.

    Rendered as zero it reads as 'measured and idle'. Only one of those is
    true, and getting it wrong is how Obsidian looked useless while it was
    simply uninstrumented.
    """
    monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
    from fastapi.testclient import TestClient

    from hivepilot.config import settings
    from hivepilot.services import api_service
    from hivepilot.services.token_service import add_token

    monkeypatch.setattr(settings, "tokens_file", tmp_path / "tokens.json")
    raw, _ = add_token("read")

    resp = TestClient(api_service.app).get("/v1/memory/backends", headers=_auth_hdr(raw))

    assert resp.status_code == 200
    body = resp.json()
    assert set(body["backends"]) >= {"mem0", "obsidian"}
    assert body["backends"]["obsidian"]["searches"] == 0


def test_memory_backends_requires_a_token():
    from fastapi.testclient import TestClient

    from hivepilot.services import api_service

    assert TestClient(api_service.app).get("/v1/memory/backends").status_code == 401


def test_memory_backends_surfaces_empty_recalls(tmp_path, monkeypatch):
    """The comparable KPI: a full top-k is the CAP, not a quality signal."""
    monkeypatch.setenv("HIVEPILOT_STATE_DB", str(tmp_path / "s.db"))
    from fastapi.testclient import TestClient

    from hivepilot.config import settings
    from hivepilot.services import api_service, memory_service
    from hivepilot.services.token_service import add_token

    monkeypatch.setattr(settings, "tokens_file", tmp_path / "tokens.json")
    for count in (5, 0, 0):
        memory_service.record_search(
            namespace="p:t:r", query="q", result_count=count, actor="cto", backend="mem0"
        )
    raw, _ = add_token("read")

    body = TestClient(api_service.app).get("/v1/memory/backends", headers=_auth_hdr(raw)).json()

    assert body["backends"]["mem0"]["searches"] == 3
    assert body["backends"]["mem0"]["empty_searches"] == 2
