"""Shared Prometheus metrics registry for HivePilot.

Import from here (not from api_service) to avoid circular imports
and to ensure all trigger paths (API, Telegram, CLI, orchestrator)
are counted on the same registry.
"""

from prometheus_client import CollectorRegistry, Counter, Histogram

registry = CollectorRegistry()

runs_total = Counter(
    "hivepilot_runs_total",
    "Total runs by outcome status",
    ["status"],
    registry=registry,
)

run_duration_seconds = Histogram(
    "hivepilot_run_duration_seconds",
    "Run wall-clock duration in seconds",
    registry=registry,
)

steps_total = Counter(
    "hivepilot_steps_total",
    "Total pipeline steps by status",
    ["status"],
    registry=registry,
)

quota_fallbacks_total = Counter(
    "hivepilot_quota_fallbacks_total",
    "Times quota was exhausted and execution fell back to another runner",
    ["to_runner"],
    registry=registry,
)

deferred_total = Counter(
    "hivepilot_deferred_total",
    "Total runs that were deferred for later execution",
    registry=registry,
)

challenges_total = Counter(
    "hivepilot_challenges_total",
    "Total challenges emitted to users",
    registry=registry,
)
