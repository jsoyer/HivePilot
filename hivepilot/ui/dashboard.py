from __future__ import annotations

from typing import Any

from textual import events
from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header

from hivepilot.services import state_service


class RunDashboard(App):
    CSS = """
    #runs {
        height: 60%;
    }
    #steps {
        height: 40%;
    }
    """

    BINDINGS = [("r", "refresh", "Refresh"), ("q", "quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header()
        self.runs_table = DataTable(id="runs")
        self.runs_table.add_columns("ID", "Project", "Task", "Status", "Started", "Finished")
        self.metrics_table = DataTable(id="metrics")
        self.metrics_table.add_columns("Metric", "Value")
        self.steps_table = DataTable(id="steps")
        self.steps_table.add_columns("Run ID", "Step", "Status", "Detail", "Timestamp")
        yield self.metrics_table
        yield self.runs_table
        yield self.steps_table
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_runs()
        self.set_interval(10, self.refresh_runs)
        self.runs_table.focus()

    def action_refresh(self) -> None:
        self.refresh_runs()

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
        success = sum(1 for run in runs if run["status"] == "success")
        failure = sum(1 for run in runs if run["status"] not in ("success", "pending", "running"))
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

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:  # type: ignore[override]
        if event.table.id != "runs":
            return
        row = event.row_key
        try:
            run_id = int(self.runs_table.get_row(row)[0])
            self.refresh_steps(run_id)
        except (ValueError, IndexError):
            return
