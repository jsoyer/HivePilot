"""Read-only TUI for browsing/inspecting loaded HivePilot plugins.

v1 (see docs/v4/PLUGINS.md, roadmap Phase 26a): browse + inspect only — no
enable/disable. Modeled directly on `hivepilot.ui.dashboard.RunDashboard`
(same `App` shape: `compose()`/`BINDINGS`/`Header`+`Footer`/`DataTable`, same
unconditional top-of-module `textual` import — the optional-dependency guard
lives at the CLI-command boundary, exactly like the `dashboard` command in
`hivepilot/cli.py`, not inside this module).
"""

from __future__ import annotations

from typing import Any

from textual.app import App, ComposeResult
from textual.widgets import DataTable, Footer, Header, Static

from hivepilot.orchestrator import Orchestrator
from hivepilot.plugins import PluginRecord

PLUGIN_COLUMNS = ("Name", "Source", "Type(s)", "Detail")

_CAPABILITY_LABELS = {"runners": "runner", "notifiers": "notifier", "hooks": "hook"}


def _plugin_module_hint(record: PluginRecord) -> str | None:
    """Best-effort module name a plugin's contributed objects would carry.

    Mirrors how `hivepilot/plugins.py` loads each source:
    - local-file: `_scan_local_plugins` loads via
      `importlib.util.spec_from_file_location(f"hivepilot_plugin_{file.stem}", file)`
      — so any class/function defined in that file reports that module name.
    - entry-point: `record.location` is `"<module>:<attr> (<dist>==<version>)"`
      (see `load_entry_point_plugins`) — the module before `:` is the hint.

    Returns None when no reliable hint can be derived.
    """
    if record.source == "local-file":
        from pathlib import Path

        stem = Path(record.location).stem
        if not stem:
            return None
        return f"hivepilot_plugin_{stem}"
    if record.source == "entry-point":
        value = record.location.split(" (", 1)[0]
        module = value.split(":", 1)[0].strip()
        return module or None
    return None


def _module_matches(obj: Any, hint: str) -> bool:
    module = getattr(obj, "__module__", "") or ""
    return module == hint or module.startswith(hint + ".")


def plugin_capabilities(
    record: PluginRecord,
    runner_map: dict[str, Any],
    notifier_map: dict[str, Any],
    hooks: dict[str, list[Any]],
) -> dict[str, list[str]]:
    """Best-effort cross-reference of a plugin record against the process-global
    RUNNER_MAP / NOTIFIER_MAP / PluginManager.hooks, by matching `__module__`
    against the hint derived from the plugin's own source/location.

    Per-plugin attribution isn't tracked anywhere upstream (`plugins list`
    documents this same v1 limitation — it's not a full join either); when the
    hint can't be derived, or nothing matches, every list comes back empty and
    the caller should show the "unknown (see aggregate)" fallback.
    """
    caps: dict[str, list[str]] = {"runners": [], "notifiers": [], "hooks": []}
    hint = _plugin_module_hint(record)
    if not hint:
        return caps

    for kind, cls in runner_map.items():
        if _module_matches(cls, hint):
            caps["runners"].append(kind)
    for name, fn in notifier_map.items():
        if _module_matches(fn, hint):
            caps["notifiers"].append(name)
    for hook_name, callables in hooks.items():
        if any(_module_matches(fn, hint) for fn in callables):
            caps["hooks"].append(hook_name)

    caps["runners"].sort()
    caps["notifiers"].sort()
    caps["hooks"].sort()
    return caps


def _format_type_label(caps: dict[str, list[str]]) -> str:
    labels = [_CAPABILITY_LABELS[key] for key in ("runners", "notifiers", "hooks") if caps[key]]
    return ", ".join(labels) if labels else "unknown (see aggregate)"


def _format_detail(caps: dict[str, list[str]]) -> str:
    parts = []
    if caps["runners"]:
        parts.append("runners=" + ",".join(caps["runners"]))
    if caps["notifiers"]:
        parts.append("notifiers=" + ",".join(caps["notifiers"]))
    if caps["hooks"]:
        parts.append("hooks=" + ",".join(caps["hooks"]))
    return "; ".join(parts) if parts else "no attributable capabilities (see aggregate detail)"


def plugin_rows(
    loaded: list[PluginRecord],
    runner_map: dict[str, Any],
    notifier_map: dict[str, Any],
    hooks: dict[str, list[Any]],
) -> list[tuple[str, str, str, str]]:
    """Build (name, source, type(s), detail) rows for the Loaded Plugins table."""
    rows: list[tuple[str, str, str, str]] = []
    for record in loaded:
        caps = plugin_capabilities(record, runner_map, notifier_map, hooks)
        rows.append((record.name, record.source, _format_type_label(caps), _format_detail(caps)))
    return rows


class PluginManagerApp(App):
    """Interactive, read-only browser/inspector for loaded plugins.

    Pass `loaded`/`runner_map`/`notifier_map`/`hooks` to inject plugin data
    for testing; when omitted, data is read from a fresh `Orchestrator()`
    (`orchestrator.plugins.loaded`, `hivepilot.registry.RUNNER_MAP`,
    `hivepilot.services.notification_service.NOTIFIER_MAP`,
    `orchestrator.plugins.hooks`).
    """

    CSS = """
    #plugins {
        height: 80%;
    }
    #details {
        height: 20%;
    }
    """

    BINDINGS = [
        ("r", "refresh", "Refresh"),
        ("q", "quit", "Quit"),
        ("enter", "details", "Details"),
    ]

    def __init__(
        self,
        *,
        loaded: list[PluginRecord] | None = None,
        runner_map: dict[str, Any] | None = None,
        notifier_map: dict[str, Any] | None = None,
        hooks: dict[str, list[Any]] | None = None,
    ) -> None:
        super().__init__()
        self._loaded = loaded
        self._runner_map = runner_map
        self._notifier_map = notifier_map
        self._hooks = hooks
        self._rows: list[tuple[str, str, str, str]] = []

    def compose(self) -> ComposeResult:
        yield Header()
        self.plugins_table = DataTable(id="plugins")
        self.plugins_table.add_columns(*PLUGIN_COLUMNS)
        self.details = Static("Select a plugin and press Enter for details.", id="details")
        yield self.plugins_table
        yield self.details
        yield Footer()

    def on_mount(self) -> None:
        self.refresh_plugins()
        self.plugins_table.cursor_type = "row"
        self.plugins_table.focus()

    def action_refresh(self) -> None:
        self.refresh_plugins()

    def action_details(self) -> None:
        self.show_details()

    def _load_data(
        self,
    ) -> tuple[list[PluginRecord], dict[str, Any], dict[str, Any], dict[str, list[Any]]]:
        if self._loaded is not None:
            return (
                self._loaded,
                self._runner_map or {},
                self._notifier_map or {},
                self._hooks or {},
            )
        from hivepilot.registry import RUNNER_MAP
        from hivepilot.services.notification_service import NOTIFIER_MAP

        orchestrator = Orchestrator()
        return orchestrator.plugins.loaded, RUNNER_MAP, NOTIFIER_MAP, orchestrator.plugins.hooks

    def refresh_plugins(self) -> None:
        loaded, runner_map, notifier_map, hooks = self._load_data()
        self._rows = plugin_rows(loaded, runner_map, notifier_map, hooks)
        self.plugins_table.clear()
        for name, source, type_label, detail in self._rows:
            self.plugins_table.add_row(name, source, type_label, detail)
        if self._rows:
            self.plugins_table.move_cursor(row=0, column=0)
        self.show_details()

    def show_details(self) -> None:
        if not self._rows:
            self.details.update("No plugins loaded.")
            return
        row_index = self.plugins_table.cursor_row
        if row_index is None or not (0 <= row_index < len(self._rows)):
            row_index = 0
        name, source, type_label, detail = self._rows[row_index]
        self.details.update(f"{name} ({source}) — {type_label}\n{detail}")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:  # type: ignore[override]
        if event.table.id != "plugins":
            return
        self.show_details()
