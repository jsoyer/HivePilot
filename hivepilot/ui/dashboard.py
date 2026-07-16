from __future__ import annotations

import importlib.util
from typing import Any

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane

from hivepilot.config import settings
from hivepilot.orchestrator import Orchestrator
from hivepilot.plugins import HealthStatus
from hivepilot.services import analytics_service, state_service
from hivepilot.ui.formatting import INTERACTION_COLUMNS, interaction_rows

# Cost table columns (Phase 24 follow-up — TUI cost analytics). "Scope" is
# either "overall", "provider:<name>", or "model:<name>" so tests/operators
# can identify a row without relying on table order.
COST_COLUMNS = ("Scope", "Steps", "Input Tokens", "Output Tokens", "Cost (USD)", "Unpriced")

# Step-failure-hotspots table columns (Analytics tab) — mirrors
# `analytics_service.step_failure_hotspots()`'s per-(step, status) rows,
# highest-failure-count combinations first.
HOTSPOT_COLUMNS = ("Step", "Status", "Count")

# Plugin Health table columns (Health tab) — mirrors `hivepilot.cli`'s
# `_print_health_table` (name/status/detail), read via
# `PluginManager.check_all()` (never-raise).
HEALTH_COLUMNS = ("Name", "Status", "Detail")

# Mem0 table columns (Mem0 tab) — typed provenance metadata (see
# `plugins/mem0.py::_provenance_metadata`) alongside a content snippet.
MEM0_COLUMNS = ("Category", "Project", "Task", "Timestamp", "Memory")

_SUCCEEDED = analytics_service.Outcome.SUCCEEDED.value
_FAILED = analytics_service.Outcome.FAILED.value

# ok=green / degraded=yellow / error=red — mirrors `hivepilot.cli._health_badge`,
# adapted for a Textual DataTable cell (a Rich `Text` renderable, not console
# markup — DataTable does not interpret `[color]...[/color]` strings as
# markup, it renders them literally).
_HEALTH_STATUS_COLORS = {"ok": "green", "degraded": "yellow", "error": "red"}


def _cost_row(scope: str, data: dict[str, Any]) -> tuple[str, ...]:
    """Format one `analytics_service.cost_summary()` scope (the "overall"
    dict, or one `by_provider`/`by_model` entry) as a display-ready row."""
    return (
        scope,
        str(data["total_steps"]),
        str(data["input_tokens"]),
        str(data["output_tokens"]),
        str(data["cost_usd"]),
        str(data["unpriced_steps"]),
    )


def _health_status_cell(status: str) -> Text:
    """Colored status badge for one Health table row — falls back to plain
    (uncolored) text for any status value outside `_HEALTH_STATUS_COLORS`
    (defensive; `_normalize_health_result` never actually produces one)."""
    color = _HEALTH_STATUS_COLORS.get(status)
    return Text(status, style=color) if color else Text(status)


def _load_mem0_plugin_module() -> Any | None:
    """Load `plugins/mem0.py` by file path so the Mem0 tab can reuse the
    plugin's OWN client-building logic (`_get_client`) without requiring
    `plugins` to be an importable package on `sys.path`.

    Mirrors `hivepilot.plugins._scan_local_plugins`'s loading mechanism
    exactly (same `settings.base_dir / "plugins"` resolution, same
    `importlib.util.spec_from_file_location` load-by-path — see that
    function's docstring: "the installed `hivepilot` binary ... doesn't have
    the project root on sys.path"). Deliberately does NOT call
    `module.register()` — the dashboard only wants the module's helpers, not
    a second registration of its lifecycle hooks (those are already
    registered once, by the real `PluginManager`, if plugins are enabled).
    Returns ``None`` on any failure (file missing, load error) — never
    raises.
    """
    try:
        plugin_path = settings.base_dir / "plugins" / "mem0.py"
        if not plugin_path.exists():
            return None
        spec = importlib.util.spec_from_file_location("hivepilot_dashboard_mem0", plugin_path)
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except Exception:  # noqa: BLE001 — the Mem0 tab must never crash the dashboard
        return None


def _mem0_memory_rows(results: Any, limit: int = 20) -> list[tuple[str, str, str, str, str]]:
    """Best-effort extraction of (category, project, task, ts, text) rows
    from a mem0 `get_all()`/`search()`-shaped result.

    Tolerant of the same result-shape variance `plugins/mem0.py::
    _extract_memory_texts` documents (a bare list of dicts, or
    ``{"results": [...]}``); degrades to an empty list rather than raising.
    Typed provenance fields (``category``/``project``/``task``/``ts`` — see
    `plugins/mem0.py::_provenance_metadata`) are read from each item's
    ``metadata`` dict when present, defaulting to ``""`` otherwise — never
    fabricated.
    """
    items: Any = results
    if isinstance(results, dict):
        items = results.get("results", results.get("memories", []))
    if not isinstance(items, list):
        return []
    rows: list[tuple[str, str, str, str, str]] = []
    for item in items[:limit]:
        if isinstance(item, dict):
            meta = item.get("metadata") or {}
            text = item.get("memory") or item.get("text") or item.get("content") or ""
            rows.append(
                (
                    str(meta.get("category", "")),
                    str(meta.get("project", "")),
                    str(meta.get("task", "")),
                    str(meta.get("ts", "")),
                    str(text)[:80],
                )
            )
        elif isinstance(item, str) and item:
            rows.append(("", "", "", "", item[:80]))
    return rows


class RunDashboard(App):
    """Mirador — HivePilot's tabbed Textual insight dashboard.

    "Mirador" is this dashboard's name (a lookout point — fitting for an
    at-a-glance operator view); the launch command stays `hivepilot
    dashboard` (see `hivepilot/cli.py`), gated behind
    `HIVEPILOT_ENABLE_TEXTUAL_UI` exactly as before.

    Four tabs: **Analytics** (runs, metrics, step-failure hotspots, recent
    interactions), **Cost** (per-provider/model cost & token breakdown),
    **Health** (plugin health via `PluginManager.check_all()`), and **Mem0**
    (recent memories when mem0 is configured+reachable, else a clear
    "not configured" placeholder — never crashes, never shows a secret).
    """

    TITLE = "Mirador"

    CSS = """
    #metrics {
        height: 15%;
    }
    #hotspots {
        height: 15%;
    }
    #runs {
        height: 30%;
    }
    #steps {
        height: 20%;
    }
    #interactions {
        height: 20%;
    }
    #cost {
        height: 100%;
    }
    #health {
        height: 100%;
    }
    #mem0-status {
        height: 3;
    }
    #mem0-table {
        height: 1fr;
    }
    """

    BINDINGS = [("r", "refresh", "Refresh"), ("q", "quit", "Quit")]

    def __init__(
        self,
        *,
        health: dict[str, HealthStatus] | None = None,
        mem0_module: Any | None = None,
    ) -> None:
        """`health`/`mem0_module` are injectable for testing — same
        dependency-injection shape as `hivepilot.ui.plugin_manager.
        PluginManagerApp`. When omitted (real usage), the Health tab reads
        from a fresh `Orchestrator().plugins.check_all()` and the Mem0 tab
        loads `plugins/mem0.py` by file path (see `_load_mem0_plugin_module`).
        """
        super().__init__()
        self._health_override = health
        self._mem0_module_override = mem0_module

    def compose(self) -> ComposeResult:
        yield Header()
        self.metrics_table: DataTable = DataTable(id="metrics")
        self.metrics_table.add_columns("Metric", "Value")
        self.hotspots_table: DataTable = DataTable(id="hotspots")
        self.hotspots_table.add_columns(*HOTSPOT_COLUMNS)
        self.runs_table: DataTable = DataTable(id="runs")
        self.runs_table.add_columns("ID", "Project", "Task", "Status", "Started", "Finished")
        self.steps_table: DataTable = DataTable(id="steps")
        self.steps_table.add_columns("Run ID", "Step", "Status", "Detail", "Timestamp")
        self.interactions_table: DataTable = DataTable(id="interactions")
        self.interactions_table.add_columns(*INTERACTION_COLUMNS)
        self.cost_table: DataTable = DataTable(id="cost")
        self.cost_table.add_columns(*COST_COLUMNS)
        self.health_table: DataTable = DataTable(id="health")
        self.health_table.add_columns(*HEALTH_COLUMNS)
        self.mem0_status: Static = Static("Mem0: loading...", id="mem0-status")
        self.mem0_table: DataTable = DataTable(id="mem0-table")
        self.mem0_table.add_columns(*MEM0_COLUMNS)

        with TabbedContent(initial="analytics-tab"):
            with TabPane("Analytics", id="analytics-tab"):
                yield self.metrics_table
                yield self.hotspots_table
                yield self.runs_table
                yield self.steps_table
                yield self.interactions_table
            with TabPane("Cost", id="cost-tab"):
                yield self.cost_table
            with TabPane("Health", id="health-tab"):
                yield self.health_table
            with TabPane("Mem0", id="mem0-tab"):
                yield self.mem0_status
                yield self.mem0_table
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_runs()
        self.set_interval(10, self.refresh_runs)
        self.refresh_hotspots()
        self.set_interval(10, self.refresh_hotspots)
        self.refresh_interactions()
        self.set_interval(10, self.refresh_interactions)
        self.refresh_cost()
        self.set_interval(10, self.refresh_cost)
        self.refresh_health()
        self.set_interval(15, self.refresh_health)
        self.refresh_mem0()
        self.set_interval(15, self.refresh_mem0)
        self.runs_table.focus()

    def action_refresh(self) -> None:
        self.refresh_runs()
        self.refresh_hotspots()
        self.refresh_interactions()
        self.refresh_cost()
        self.refresh_health()
        self.refresh_mem0()

    def refresh_runs(self) -> None:
        runs = state_service.list_recent_runs(50)
        self.runs_table.clear()
        for run in runs:
            self.runs_table.add_row(
                str(run["id"]),
                run["project"],
                run["task"],
                run["status"],
                run["started_at"],
                run.get("finished_at") or "",
            )
        self.refresh_metrics()
        if runs:
            self.runs_table.cursor_type = "row"
            self.runs_table.move_cursor(row=0, column=0)
            self.refresh_steps(int(runs[0]["id"]))

    def refresh_metrics(self) -> None:
        runs = state_service.list_all_runs()
        total = len(runs)
        # Phase 24a: reconciled via the same canonical outcome mapping used by
        # analytics_service (and the /v1/analytics/* API) — "success" (legacy
        # literal) and "complete" (RunStatus.COMPLETE) both count as success;
        # only the formal failure states (+ "failed"/"denied") count as
        # failure. Previously `status not in ("success", "pending", "running")`
        # miscounted "complete" runs as failures.
        success = sum(
            1 for run in runs if analytics_service.canonical_outcome(run["status"]) == _SUCCEEDED
        )
        failure = sum(
            1 for run in runs if analytics_service.canonical_outcome(run["status"]) == _FAILED
        )
        stats = {
            "total_runs": total,
            "success": success,
            "failure": failure,
        }
        # Optional (Phase 24 cost-analytics follow-up): p50/p95/p99 run
        # duration, unbounded (days=None) to match the unscoped total_runs
        # count above. Cheap to compute (reuses the existing analytics_service
        # helper) and additive-only — existing keys/rows are unaffected.
        duration_stats = analytics_service.run_durations(tenant=None, days=None)["overall"]
        stats["duration_p50_s"] = duration_stats["p50"]
        stats["duration_p95_s"] = duration_stats["p95"]
        stats["duration_p99_s"] = duration_stats["p99"]
        self.metrics_table.clear()
        for key, value in stats.items():
            self.metrics_table.add_row(key, str(value))

    def refresh_hotspots(self) -> None:
        """Populate the Analytics tab's step-failure-hotspots table from
        `analytics_service.step_failure_hotspots()`. Unscoped/unbounded
        (tenant=None, days=None), mirroring `refresh_cost`'s local-operator
        convention."""
        hotspots = analytics_service.step_failure_hotspots(tenant=None, days=None, limit=20)
        self.hotspots_table.clear()
        for hotspot in hotspots:
            self.hotspots_table.add_row(hotspot["step"], hotspot["status"], str(hotspot["count"]))

    def refresh_cost(self) -> None:
        """Populate the Cost table from `analytics_service.cost_summary()`.

        Unscoped (tenant=None) and unbounded (days=None) — the dashboard is a
        local operator tool, mirroring `refresh_metrics()`'s use of
        `state_service.list_all_runs()` (also unscoped/unbounded). Read-only.
        """
        summary = analytics_service.cost_summary(tenant=None, days=None)
        self.cost_table.clear()
        self.cost_table.add_row(*_cost_row("overall", summary["overall"]))
        for row in summary["by_provider"]:
            self.cost_table.add_row(*_cost_row(f"provider:{row['provider']}", row))
        for row in summary["by_model"]:
            self.cost_table.add_row(*_cost_row(f"model:{row['model']}", row))

    def refresh_health(self) -> None:
        """Populate the Health tab from `PluginManager.check_all()` — a
        `{name: HealthStatus}` mapping that never raises per-check (a broken
        check reports `HealthStatus("error", ...)` — see `PluginManager.
        run_health_check`). Wrapped in its own try/except so even a failure
        constructing `Orchestrator()` itself degrades to a single `error` row
        instead of crashing the dashboard."""
        self.health_table.clear()
        try:
            results: dict[str, HealthStatus] = (
                self._health_override
                if self._health_override is not None
                else Orchestrator().plugins.check_all()
            )
        except Exception as exc:  # noqa: BLE001 — the Health tab must never crash the dashboard
            results = {"dashboard": HealthStatus("error", f"{type(exc).__name__} loading plugins")}
        for name in sorted(results):
            status, detail = results[name]
            self.health_table.add_row(name, _health_status_cell(status), detail)
        if not results:
            self.health_table.add_row("-", "-", "-")

    def refresh_mem0(self) -> None:
        """Populate the Mem0 tab: recent memories when mem0 is configured
        (`settings.mem0_enabled`) and reachable, else a clear "not
        configured" placeholder in `mem0_status`. Reuses `plugins/mem0.py`'s
        own `_get_client()` (via `_load_mem0_plugin_module`) rather than
        re-deriving the hosted-vs-self-host client construction here. Never
        raises, never surfaces a secret/token — only the exception TYPE name
        is shown on failure, never `str(exc)` (which could echo back
        configuration/error content)."""
        self.mem0_table.clear()
        module = (
            self._mem0_module_override
            if self._mem0_module_override is not None
            else _load_mem0_plugin_module()
        )
        if module is None:
            self.mem0_status.update("Mem0 not configured (plugin unavailable).")
            return
        try:
            if not settings.mem0_enabled:
                self.mem0_status.update("Mem0 not configured (HIVEPILOT_MEM0_ENABLED is False).")
                return
            get_client = getattr(module, "_get_client", None)
            client = get_client() if callable(get_client) else None
            if client is None:
                self.mem0_status.update("Mem0 not configured (no client could be built).")
                return
            get_all = getattr(client, "get_all", None)
            if not callable(get_all):
                self.mem0_status.update(
                    "Mem0 configured, but this client doesn't support listing memories."
                )
                return
            rows = _mem0_memory_rows(get_all())
        except Exception as exc:  # noqa: BLE001 — the Mem0 tab must never crash the dashboard
            self.mem0_status.update(f"Mem0 configured but unreachable ({type(exc).__name__}).")
            return
        for row in rows:
            self.mem0_table.add_row(*row)
        self.mem0_status.update(f"Mem0: {len(rows)} recent memories.")

    def refresh_steps(self, run_id: int) -> None:
        steps = state_service.get_steps_for_run(run_id)
        self.steps_table.clear()
        for step in steps:
            self.steps_table.add_row(
                str(step["run_id"]),
                step["step"],
                step["status"],
                (step.get("detail") or "")[:80],
                step["timestamp"],
            )

    def refresh_interactions(self) -> None:
        interactions = state_service.list_recent_interactions(50)
        self.interactions_table.clear()
        for row in interaction_rows(interactions):
            self.interactions_table.add_row(*row)

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:  # type: ignore[override]
        # `event.data_table` (not `.table`) is the actual attribute on
        # textual's DataTable.RowHighlighted message — the old `.table` name
        # raised AttributeError on every row highlight (i.e. whenever
        # refresh_runs() found any run), crashing the dashboard on real use.
        # Fixed identically in hivepilot/ui/plugin_manager.py.
        if event.data_table.id != "runs":
            return
        row = event.row_key
        try:
            run_id = int(self.runs_table.get_row(row)[0])
            self.refresh_steps(run_id)
        except (ValueError, IndexError):
            return
