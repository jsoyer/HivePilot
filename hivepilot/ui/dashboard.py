from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header

from hivepilot.services import analytics_service, state_service
from hivepilot.ui.formatting import INTERACTION_COLUMNS, interaction_rows

_SUCCEEDED = analytics_service.Outcome.SUCCEEDED.value
_FAILED = analytics_service.Outcome.FAILED.value


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
        self.steps_table: DataTable = DataTable(id="steps")
        self.steps_table.add_columns("Run ID", "Step", "Status", "Detail", "Timestamp")
        self.interactions_table: DataTable = DataTable(id="interactions")
        self.interactions_table.add_columns(*INTERACTION_COLUMNS)
        yield self.metrics_table
        yield self.runs_table
        yield self.steps_table
        yield self.interactions_table
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_runs()
        self.set_interval(10, self.refresh_runs)
        self.refresh_interactions()
        self.set_interval(10, self.refresh_interactions)
        self.runs_table.focus()

    def action_refresh(self) -> None:
        self.refresh_runs()
        self.refresh_interactions()

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
