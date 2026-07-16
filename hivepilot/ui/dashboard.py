from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header

from hivepilot.services import analytics_service, state_service
from hivepilot.ui.formatting import INTERACTION_COLUMNS, interaction_rows

# Cost table columns (Phase 24 follow-up — TUI cost analytics). "Scope" is
# either "overall", "provider:<name>", or "model:<name>" so tests/operators
# can identify a row without relying on table order.
COST_COLUMNS = ("Scope", "Steps", "Input Tokens", "Output Tokens", "Cost (USD)", "Unpriced")

_SUCCEEDED = analytics_service.Outcome.SUCCEEDED.value
_FAILED = analytics_service.Outcome.FAILED.value


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


class RunDashboard(App):
    CSS = """
    #runs {
        height: 40%;
    }
    #steps {
        height: 30%;
    }
    #interactions {
        height: 30%;
    }
    """

    BINDINGS = [("r", "refresh", "Refresh"), ("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header()
        self.runs_table: DataTable = DataTable(id="runs")
        self.runs_table.add_columns("ID", "Project", "Task", "Status", "Started", "Finished")
        self.metrics_table: DataTable = DataTable(id="metrics")
        self.metrics_table.add_columns("Metric", "Value")
        self.cost_table: DataTable = DataTable(id="cost")
        self.cost_table.add_columns(*COST_COLUMNS)
        self.steps_table: DataTable = DataTable(id="steps")
        self.steps_table.add_columns("Run ID", "Step", "Status", "Detail", "Timestamp")
        self.interactions_table: DataTable = DataTable(id="interactions")
        self.interactions_table.add_columns(*INTERACTION_COLUMNS)
        yield self.metrics_table
        yield self.cost_table
        yield self.runs_table
        yield self.steps_table
        yield self.interactions_table
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_runs()
        self.set_interval(10, self.refresh_runs)
        self.refresh_interactions()
        self.set_interval(10, self.refresh_interactions)
        self.refresh_cost()
        self.set_interval(10, self.refresh_cost)
        self.runs_table.focus()

    def action_refresh(self) -> None:
        self.refresh_runs()
        self.refresh_interactions()
        self.refresh_cost()

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
        if runs:
            self.runs_table.cursor_type = "row"
            self.runs_table.move_cursor(row=0, column=0)
            self.refresh_steps(int(runs[0]["id"]))

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
