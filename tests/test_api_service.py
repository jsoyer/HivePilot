"""Tests for api_service: /healthz, /readyz, /metrics endpoints.

More comprehensive observability tests live in test_observability.py.
This file exists so the TDD hook allows editing api_service.py.
"""

from __future__ import annotations


def test_healthz_ok():
    from fastapi.testclient import TestClient

    from hivepilot.services.api_service import app

    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json().get("status") == "ok"


def test_v1_healthz_ok():
    from fastapi.testclient import TestClient

    from hivepilot.services.api_service import app

    client = TestClient(app)
    resp = client.get("/v1/healthz")
    assert resp.status_code == 200


def test_readyz_shape():
    from fastapi.testclient import TestClient

    from hivepilot.services.api_service import app

    client = TestClient(app)
    resp = client.get("/readyz")
    assert resp.status_code in (200, 503)
    data = resp.json()
    # On 200 the response is {"ready": True, "checks": {...}}
    # On 503 FastAPI wraps it: {"detail": {"ready": False, "checks": {...}}}
    payload = data.get("detail", data)
    assert "checks" in payload


def test_metrics_content_type():
    from fastapi.testclient import TestClient

    from hivepilot.services.api_service import app

    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]


def test_metrics_no_local_registry():
    """api_service must not define its own CollectorRegistry — uses shared one."""
    with open(
        "/home/jeromesoyer/Documents/Github/jsoyer/HivePilot/hivepilot/services/api_service.py"
    ) as f:
        source = f.read()
    assert "CollectorRegistry()" not in source


def test_no_run_counter_in_api_service():
    """run_counter was removed; only complete_run increments runs_total."""
    with open(
        "/home/jeromesoyer/Documents/Github/jsoyer/HivePilot/hivepilot/services/api_service.py"
    ) as f:
        source = f.read()
    assert "run_counter" not in source
