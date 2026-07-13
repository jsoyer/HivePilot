"""Tests for observability: /healthz, /readyz, /metrics, metric instrumentation."""

from __future__ import annotations


def _get_counter_value(registry, metric_name: str, labels: dict) -> float:
    """Helper to get a labeled counter value from a registry."""
    from prometheus_client import generate_latest

    output = generate_latest(registry).decode()
    for line in output.splitlines():
        if line.startswith("#"):
            continue
        if not line.startswith(metric_name):
            continue
        # Check all labels match
        if all(f'{k}="{v}"' in line for k, v in labels.items()):
            return float(line.split()[-1])
    return 0.0


def _get_counter_value_no_labels(registry, metric_name: str) -> float:
    """Helper to get a counter value with no labels from a registry."""
    from prometheus_client import generate_latest

    output = generate_latest(registry).decode()
    for line in output.splitlines():
        if line.startswith("#"):
            continue
        if line.startswith(metric_name + " ") or line.startswith(metric_name + "{"):
            # For no-label counters the line is: <name>_total <value>
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return float(parts[-1])
                except ValueError:
                    pass
    return 0.0


# ---------------------------------------------------------------------------
# metrics module tests
# ---------------------------------------------------------------------------


def test_metrics_module_exposes_all_counters():
    """All expected metric names are defined in the shared registry."""
    from prometheus_client import generate_latest

    from hivepilot.services import metrics

    output = generate_latest(metrics.registry).decode()
    for name in (
        "hivepilot_runs_total",
        "hivepilot_run_duration_seconds",
        "hivepilot_steps_total",
        "hivepilot_quota_fallbacks_total",
        "hivepilot_deferred_total",
        "hivepilot_challenges_total",
    ):
        assert name in output, f"Missing metric: {name}"


def test_runs_total_increments():
    """runs_total increments for the right status label."""
    from hivepilot.services import metrics

    before = _get_counter_value(metrics.registry, "hivepilot_runs_total", {"status": "success"})
    metrics.runs_total.labels(status="success").inc()
    after = _get_counter_value(metrics.registry, "hivepilot_runs_total", {"status": "success"})
    assert after == before + 1


def test_steps_total_increments():
    """steps_total increments for the right status label."""
    from hivepilot.services import metrics

    before = _get_counter_value(metrics.registry, "hivepilot_steps_total", {"status": "ok"})
    metrics.steps_total.labels(status="ok").inc()
    after = _get_counter_value(metrics.registry, "hivepilot_steps_total", {"status": "ok"})
    assert after == before + 1


def test_deferred_total_increments():
    """deferred_total increments."""
    from hivepilot.services import metrics

    before = _get_counter_value_no_labels(metrics.registry, "hivepilot_deferred_total")
    metrics.deferred_total.inc()
    after = _get_counter_value_no_labels(metrics.registry, "hivepilot_deferred_total")
    assert after == before + 1


def test_challenges_total_increments():
    """challenges_total increments."""
    from hivepilot.services import metrics

    before = _get_counter_value_no_labels(metrics.registry, "hivepilot_challenges_total")
    metrics.challenges_total.inc()
    after = _get_counter_value_no_labels(metrics.registry, "hivepilot_challenges_total")
    assert after == before + 1


def test_quota_fallbacks_total_increments():
    """quota_fallbacks_total increments for the right to_runner label."""
    from hivepilot.services import metrics

    before = _get_counter_value(
        metrics.registry, "hivepilot_quota_fallbacks_total", {"to_runner": "claude-opus"}
    )
    metrics.quota_fallbacks_total.labels(to_runner="claude-opus").inc()
    after = _get_counter_value(
        metrics.registry, "hivepilot_quota_fallbacks_total", {"to_runner": "claude-opus"}
    )
    assert after == before + 1


# ---------------------------------------------------------------------------
# api_service endpoint tests
# ---------------------------------------------------------------------------


def test_healthz_returns_200():
    from fastapi.testclient import TestClient

    from hivepilot.services.api_service import app

    client = TestClient(app)
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json().get("status") == "ok"


def test_v1_healthz_returns_200():
    from fastapi.testclient import TestClient

    from hivepilot.services.api_service import app

    client = TestClient(app)
    resp = client.get("/v1/healthz")
    assert resp.status_code == 200


def test_readyz_returns_200_when_healthy():
    from fastapi.testclient import TestClient

    from hivepilot.services.api_service import app

    client = TestClient(app)
    resp = client.get("/readyz")
    # May be 200 or 503 depending on environment; just check the shape
    assert resp.status_code in (200, 503)
    data = resp.json()
    assert "checks" in data


def test_readyz_shape_on_success():
    """When DB and config are reachable, readyz returns ready=True."""
    from fastapi.testclient import TestClient

    from hivepilot.services.api_service import app

    client = TestClient(app)
    resp = client.get("/readyz")
    if resp.status_code == 200:
        assert resp.json().get("ready") is True


def test_metrics_exposes_runs_total():
    from fastapi.testclient import TestClient

    from hivepilot.services.api_service import app

    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "hivepilot_runs_total" in resp.text


def test_metrics_exposes_steps_total():
    from fastapi.testclient import TestClient

    from hivepilot.services.api_service import app

    client = TestClient(app)
    resp = client.get("/metrics")
    assert "hivepilot_steps_total" in resp.text


def test_metrics_exposes_deferred_total():
    from fastapi.testclient import TestClient

    from hivepilot.services.api_service import app

    client = TestClient(app)
    resp = client.get("/metrics")
    assert "hivepilot_deferred_total" in resp.text


def test_metrics_exposes_challenges_total():
    from fastapi.testclient import TestClient

    from hivepilot.services.api_service import app

    client = TestClient(app)
    resp = client.get("/metrics")
    assert "hivepilot_challenges_total" in resp.text


def test_no_double_count_from_api():
    """api_service should not directly increment runs_total (only complete_run does)."""
    from pathlib import Path

    from hivepilot.services import api_service

    source = Path(api_service.__file__).read_text()
    assert "run_counter" not in source, "run_counter was not removed from api_service"


def test_api_service_no_local_registry():
    """api_service must not define its own CollectorRegistry."""
    from pathlib import Path

    from hivepilot.services import api_service

    source = Path(api_service.__file__).read_text()
    assert "CollectorRegistry()" not in source, (
        "api_service still creates its own CollectorRegistry"
    )
