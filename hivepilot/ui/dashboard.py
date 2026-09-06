from __future__ import annotations

import re
from typing import Any, cast

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import DataTable, Footer, Header, Static, TabbedContent, TabPane

from hivepilot.orchestrator import Orchestrator
from hivepilot.plugins import (
    HealthStatus,
    PanelData,
    PanelSpec,
    PanelStatSection,
    PanelTableSection,
    PanelTextSection,
)
from hivepilot.services import analytics_service, state_service
from hivepilot.ui.formatting import INTERACTION_COLUMNS, interaction_rows
from hivepilot.utils import display_time

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

_SUCCEEDED = analytics_service.Outcome.SUCCEEDED.value
_FAILED = analytics_service.Outcome.FAILED.value

# ok=green / degraded=yellow / error=red — mirrors `hivepilot.cli._health_badge`,
# adapted for a Textual DataTable cell (a Rich `Text` renderable, not console
# markup — DataTable does not interpret `[color]...[/color]` strings as
# markup, it renders them literally).
_HEALTH_STATUS_COLORS = {"ok": "green", "degraded": "yellow", "error": "red"}

# ok=green / warn=yellow / error=red — same coloring convention as
# `_HEALTH_STATUS_COLORS`, adapted to a panel `stat` section's own closed
# status enum (`hivepilot.plugins.PANEL_STAT_STATUSES`), which uses "warn"
# rather than health's "degraded". `None` (unset) status intentionally has no
# entry here -> falls back to a plain, uncolored value (see `_panel_stat_widget`).
_PANEL_STAT_STATUS_COLORS = {"ok": "green", "warn": "yellow", "error": "red"}

# A panel-contributed tab/content-container id must be a valid Textual DOM id
# (`[a-zA-Z_-][a-zA-Z0-9_-]*` — see `textual.css.tokenize.IDENTIFIER`). Panel
# `name`s are plugin-authored and not guaranteed to already match that shape,
# so any disallowed character is replaced with `_` and a `p_` prefix is added
# if the sanitized result would not start with a valid leading character.
_PANEL_ID_INVALID_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize_panel_id_part(name: str) -> str:
    """Coerce a plugin-authored panel `name` into a valid Textual widget id
    fragment. Not guaranteed collision-free for adversarial inputs (e.g. two
    names that sanitize to the same fragment) — panel `name`s are already
    collision-checked for uniqueness at registration (`PanelNameCollisionError`
    in `hivepilot.plugins`), and plugins are operator-installed, trusted code
    for this purpose (only rendered *section content* is untrusted — see
    `PanelData`'s docstring)."""
    sanitized = _PANEL_ID_INVALID_CHARS.sub("_", name)
    if not sanitized or not re.match(r"[a-zA-Z_-]", sanitized[0]):
        sanitized = f"p_{sanitized}"
    return sanitized


def _panel_pane_id(name: str) -> str:
    return f"panel-{_sanitize_panel_id_part(name)}-tab"


def _panel_content_id(name: str) -> str:
    return f"panel-{_sanitize_panel_id_part(name)}-content"


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


def _panel_stat_widget(section: PanelStatSection) -> Static:
    """Render one `stat` section as a labeled value with a colored
    ok/warn/error status badge (`None` status -> plain, uncolored value —
    same "no badge for unset status" convention `_health_status_cell`
    follows). Built as a `rich.text.Text` object, not a markup string: `Text`
    is a literal renderable — it never interprets `label`/`value` (plugin-
    authored, UNTRUSTED content per `PanelData`'s docstring) as
    Rich/Textual console markup, exactly like `_health_status_cell` above."""
    status = section.get("status")
    color = _PANEL_STAT_STATUS_COLORS.get(status) if status else None
    text = Text(f"{section['label']}: ")
    text.append(section["value"], style=color)
    return Static(text)


def _panel_table_widget(section: PanelTableSection) -> DataTable:
    """Render one `table` section as a fresh `DataTable` — columns/rows are
    plugin-authored/UNTRUSTED strings (`PanelData` docstring). Each header and
    cell is wrapped in `rich.text.Text(...)` — the same literal-rendering
    guarantee `_panel_stat_widget`/`_panel_text_widget` rely on — rather than
    depending on an unverified "DataTable doesn't parse markup" assumption."""
    table: DataTable = DataTable()
    table.add_columns(*(Text(column) for column in section["columns"]))
    for row in section["rows"]:
        table.add_row(*(Text(cell) for cell in row))
    return table


def _panel_text_widget(section: PanelTextSection) -> Static:
    """Render one `text` section as plain text. `content` is plugin-
    authored/UNTRUSTED (`PanelData` docstring) — wrapping it in `rich.text.
    Text(...)` (rather than passing the raw string straight to `Static`,
    which WOULD parse `[...]`-looking substrings as Rich console markup)
    guarantees it is displayed literally, never interpreted as a style/markup
    tag."""
    return Static(Text(section["content"]))


def _panel_section_widgets(data: PanelData) -> list[Widget]:
    """Render every section of one panel's (already-normalized) `PanelData`
    into a flat list of widgets, in order. An empty `sections` list (a panel
    with genuinely nothing to show) renders a single "no data" placeholder
    rather than an empty container."""
    sections = data["sections"]
    if not sections:
        return [Static("No data available.")]

    widgets: list[Widget] = []
    for section in sections:
        kind = section["kind"]
        if kind == "stat":
            widgets.append(_panel_stat_widget(cast(PanelStatSection, section)))
        elif kind == "table":
            widgets.append(_panel_table_widget(cast(PanelTableSection, section)))
        else:  # kind == "text" — the only other member of PANEL_SECTION_KINDS
            widgets.append(_panel_text_widget(cast(PanelTextSection, section)))
    return widgets


class RunDashboard(App):
    """Pollen — HivePilot's tabbed Textual insight dashboard.

    "Pollen" is this dashboard's name — identical in French and English, and
    it's what foragers gather from everywhere and bring back to one place,
    fitting for a dashboard that pulls together runs across every project
    and machine; the launch command stays `hivepilot dashboard` (see
    `hivepilot/cli.py`), gated behind `HIVEPILOT_ENABLE_TEXTUAL_UI` exactly
    as before.

    Three built-in tabs: **Analytics** (runs, metrics, step-failure hotspots,
    recent interactions), **Cost** (per-provider/model cost & token
    breakdown), and **Health** (plugin health via `PluginManager.check_all()`).
    Plus one additional tab per Pollen **panel** plugin
    (`hivepilot.plugins.PanelSpec` — see module docstring on `PanelData`),
    rendered generically from its `stat`/`table`/`text` sections (Sprint 2).
    """

    TITLE = "Pollen"

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
    .panel-content {
        height: 100%;
    }
    """

    BINDINGS = [("r", "refresh", "Refresh"), ("q", "quit", "Quit")]

    def __init__(
        self,
        *,
        health: dict[str, HealthStatus] | None = None,
        plugin_manager: Any | None = None,
    ) -> None:
        """`health`/`plugin_manager` are injectable for
        testing — same dependency-injection shape as `hivepilot.ui.
        plugin_manager.PluginManagerApp`. When omitted (real usage), the
        Health tab reads from a fresh `Orchestrator().plugins.check_all()`,
        and the panel tabs are built from a
        fresh `Orchestrator().plugins` (an object exposing `list_panels()` /
        `run_panel_fetch(name)`, same shape as `hivepilot.plugins.
        PluginManager`).

        `plugin_manager` is resolved once here (not per-refresh, unlike
        `health`): the panel *tab list* is fixed for the dashboard's
        lifetime (mirrors `PluginManager` itself only scanning/registering
        once, at construction — see `hivepilot.plugins.PluginManager`'s
        module docstring); only each panel's *data* is re-fetched, on tab
        activation (see `refresh_panel`).
        """
        super().__init__()
        self._health_override = health
        self._plugin_manager_override = plugin_manager
        self._panel_manager = self._resolve_panel_manager()
        self._panels: list[PanelSpec] = self._list_panels_safe()
        self._panel_content_ids: dict[str, str] = {}
        self._pane_id_to_panel: dict[str, str] = {}
        for panel in self._panels:
            name = panel["name"]
            self._panel_content_ids[name] = _panel_content_id(name)
            self._pane_id_to_panel[_panel_pane_id(name)] = name

    def _resolve_panel_manager(self) -> Any | None:
        """An object exposing `list_panels()` / `run_panel_fetch(name)` — the
        real `PluginManager` instance (via a fresh `Orchestrator().plugins`)
        in normal usage, or the injected `plugin_manager` override for
        testing. Wrapped in its own try/except so a failure constructing
        `Orchestrator()` itself degrades to zero panel tabs rather than
        crashing dashboard startup — mirrors `refresh_health`'s guard."""
        if self._plugin_manager_override is not None:
            return self._plugin_manager_override
        try:
            return Orchestrator().plugins
        except Exception:  # noqa: BLE001 — panel tabs must never crash the dashboard
            return None

    def _list_panels_safe(self) -> list[PanelSpec]:
        """`list_panels()` on the resolved panel manager (`self._panel_manager`
        — see `_resolve_panel_manager`), wrapped in its own try/except so a
        plugin bug raised from `list_panels()` itself (e.g. a panel plugin
        that misbehaves during registration/discovery) degrades to zero panel
        tabs rather than raising out of `__init__` — which would fail the
        *whole* dashboard's construction, taking down the 3 built-in tabs
        with it. Mirrors `_resolve_panel_manager`'s own never-raise guard for
        `Orchestrator()` construction."""
        if self._panel_manager is None:
            return []
        try:
            return self._panel_manager.list_panels()
        except Exception:  # noqa: BLE001 — panel tabs must never crash the dashboard
            return []

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
            for panel in self._panels:
                name = panel["name"]
                with TabPane(panel["title"], id=_panel_pane_id(name)):
                    yield Vertical(
                        Static("Loading…"),
                        id=self._panel_content_ids[name],
                        classes="panel-content",
                    )
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
        self.runs_table.focus()

    def action_refresh(self) -> None:
        self.refresh_runs()
        self.refresh_hotspots()
        self.refresh_interactions()
        self.refresh_cost()
        self.refresh_health()

    def refresh_runs(self) -> None:
        runs = state_service.list_recent_runs(50)
        self.runs_table.clear()
        for run in runs:
            # fix/linear-sync-display-time sweep: `started_at`/`finished_at`
            # are stored as naive-UTC SQLite CURRENT_TIMESTAMP strings --
            # the same bug class `display_time.to_display` exists to fix.
            # A still-running row's absent `finished_at` renders empty
            # (unchanged contract), not the "(unknown)" unparseable marker.
            finished_at = run.get("finished_at")
            self.runs_table.add_row(
                str(run["id"]),
                run["project"],
                run["task"],
                run["status"],
                display_time.to_display(run["started_at"]),
                display_time.to_display(finished_at) if finished_at else "",
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

    async def refresh_panel(self, name: str) -> None:
        """Fetch+render a single panel's data into its tab, replacing any
        previous content.

        Never raises: `run_panel_fetch` (on the resolved plugin-manager-like
        object — see `_resolve_panel_manager`) already normalizes a
        raising/malformed `fetch()` into a single error `stat` section
        (exception TYPE name only, never the exception message — see
        `hivepilot.plugins.PluginManager.run_panel_fetch`'s docstring); it is
        rendered here through the exact same generic `_panel_section_widgets`
        path as any other `stat` section, so there is no second place that
        could leak `str(exc)`. If a panel manager was never resolved (only
        reachable defensively — `self._panels` would already be empty and
        this method would have no tab to be called for), degrades to a plain
        placeholder instead of raising.

        The fetch+widget-build+mount step below is *additionally* wrapped in
        its own try/except — defense-in-depth on top of `run_panel_fetch`'s
        own never-raise, mirroring `refresh_health`'s guards —
        so a bug in `_panel_section_widgets` (or in the mount/remove_children
        calls themselves) degrades to a single error placeholder instead of
        crashing the dashboard. Never surfaces `str(exc)`, only the exception
        TYPE name, same "no secret leak" convention as the rest of this
        module.
        """
        content_id = self._panel_content_ids.get(name)
        if content_id is None:
            return
        try:
            container = self.query_one(f"#{content_id}", Vertical)
        except Exception:  # noqa: BLE001 — a missing/unmounted pane must not crash the app
            return

        try:
            if self._panel_manager is None:
                widgets: list[Widget] = [Static("Panel unavailable.")]
            else:
                data = self._panel_manager.run_panel_fetch(name)
                widgets = _panel_section_widgets(data)
            await container.remove_children()
            await container.mount(*widgets)
        except Exception as exc:  # noqa: BLE001 — refresh_panel must never crash the dashboard
            try:
                await container.remove_children()
                await container.mount(
                    Static(Text(f"Error rendering panel ({type(exc).__name__})."))
                )
            except Exception:  # noqa: BLE001 — best-effort fallback; nothing further to do
                pass

    async def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Fetch-on-activate: a panel's `fetch()` may do real work (network
        calls, DB queries, ...), so panel data is (re)fetched only when its
        tab actually becomes active — never for every panel on every refresh
        tick, unlike the built-in tabs' `set_interval`-driven refreshes."""
        name = self._pane_id_to_panel.get(event.pane.id or "")
        if name is not None:
            await self.refresh_panel(name)

    def refresh_steps(self, run_id: int) -> None:
        steps = state_service.get_steps_for_run(run_id)
        self.steps_table.clear()
        for step in steps:
            # fix/linear-sync-display-time sweep: same naive-UTC bug class
            # as refresh_runs above -- route through the shared helper.
            self.steps_table.add_row(
                str(step["run_id"]),
                step["step"],
                step["status"],
                (step.get("detail") or "")[:80],
                display_time.to_display(step["timestamp"]),
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
