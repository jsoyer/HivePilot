"""Tests for the shared metrics registry (hivepilot/services/metrics.py).

Full observability tests (endpoints + instrumentation) live in test_observability.py.
This file covers the metrics module itself so the TDD hook is satisfied before
metrics.py is written.
"""

from __future__ import annotations


def _get_labeled_value(registry, metric_name: str, labels: dict) -> float:
    from prometheus_client import generate_latest

    output = generate_latest(registry).decode()
    for line in output.splitlines():
        if line.startswith("#"):
            continue
        if not line.startswith(metric_name):
            continue
        if all(f'{k}="{v}"' in line for k, v in labels.items()):
            return float(line.split()[-1])
    return 0.0


def _get_unlabeled_value(registry, metric_name: str) -> float:
    from prometheus_client import generate_latest

    output = generate_latest(registry).decode()
    for line in output.splitlines():
        if line.startswith("#"):
            continue
        if line.startswith(metric_name + " ") or (
            line.startswith(metric_name) and "{" not in line.split()[0]
        ):
            return float(line.split()[-1])
    return 0.0


def test_registry_is_isolated():
    """Metrics registry is not the default global registry."""
    from prometheus_client import REGISTRY

    from hivepilot.services.metrics import registry

    assert registry is not REGISTRY


def test_all_metrics_registered():
    """All expected metric names appear in the shared registry output."""
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
        assert name in output, f"Metric not in registry output: {name}"


def test_runs_total_labeled_increment():
    from hivepilot.services import metrics

    before = _get_labeled_value(metrics.registry, "hivepilot_runs_total", {"status": "success"})
    metrics.runs_total.labels(status="success").inc()
    after = _get_labeled_value(metrics.registry, "hivepilot_runs_total", {"status": "success"})
    assert after == before + 1


def test_runs_total_failure_label():
    from hivepilot.services import metrics

    before = _get_labeled_value(metrics.registry, "hivepilot_runs_total", {"status": "failure"})
    metrics.runs_total.labels(status="failure").inc()
    after = _get_labeled_value(metrics.registry, "hivepilot_runs_total", {"status": "failure"})
    assert after == before + 1


def test_steps_total_labeled_increment():
    from hivepilot.services import metrics

    before = _get_labeled_value(metrics.registry, "hivepilot_steps_total", {"status": "ok"})
    metrics.steps_total.labels(status="ok").inc()
    after = _get_labeled_value(metrics.registry, "hivepilot_steps_total", {"status": "ok"})
    assert after == before + 1


def test_quota_fallbacks_labeled_increment():
    from hivepilot.services import metrics

    before = _get_labeled_value(
        metrics.registry, "hivepilot_quota_fallbacks_total", {"to_runner": "haiku"}
    )
    metrics.quota_fallbacks_total.labels(to_runner="haiku").inc()
    after = _get_labeled_value(
        metrics.registry, "hivepilot_quota_fallbacks_total", {"to_runner": "haiku"}
    )
    assert after == before + 1


def test_deferred_total_increment():
    from hivepilot.services import metrics

    before_output = (
        __import__("prometheus_client", fromlist=["generate_latest"])
        .generate_latest(metrics.registry)
        .decode()
    )
    metrics.deferred_total.inc()
    after_output = (
        __import__("prometheus_client", fromlist=["generate_latest"])
        .generate_latest(metrics.registry)
        .decode()
    )

    # Find the _total line for deferred_total (name ends in _total, no extra suffix)
    def _val(text: str) -> float:
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            if line.startswith("hivepilot_deferred_total "):
                return float(line.split()[-1])
        return 0.0

    assert _val(after_output) == _val(before_output) + 1


def test_challenges_total_increment():
    from hivepilot.services import metrics

    def _val(text: str) -> float:
        for line in text.splitlines():
            if line.startswith("#"):
                continue
            if line.startswith("hivepilot_challenges_total "):
                return float(line.split()[-1])
        return 0.0

    from prometheus_client import generate_latest

    before = _val(generate_latest(metrics.registry).decode())
    metrics.challenges_total.inc()
    after = _val(generate_latest(metrics.registry).decode())
    assert after == before + 1


def test_run_duration_seconds_observable():
    """run_duration_seconds histogram can be observed without error."""
    from hivepilot.services.metrics import run_duration_seconds

    run_duration_seconds.observe(0.5)
    run_duration_seconds.observe(1.2)
