"""TUI for browsing/inspecting loaded HivePilot plugins, with an
enable/disable toggle.

v1 (see docs/v4/PLUGINS.md, roadmap Phase 26a) was browse + inspect only.
Sprint 5 (Phase 26b) adds a `space` binding that flips the highlighted
plugin's presence in `settings.plugins_disabled` and persists that change —
see `persist_plugins_disabled` below. Toggling does NOT live-unregister a
plugin's runners/notifiers/hooks: `hivepilot.plugins.PluginManager` only
scans/registers once, at construction, so a toggle takes effect the next
time the process starts (live hot-reload is out of scope for this sprint).

Modeled directly on `hivepilot.ui.dashboard.RunDashboard` (same `App` shape:
`compose()`/`BINDINGS`/`Header`+`Footer`/`DataTable`, same unconditional
top-of-module `textual` import — the optional-dependency guard lives at the
CLI-command boundary, exactly like the `dashboard` command in
`hivepilot/cli.py`, not inside this module).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from textual.app import App, ComposeResult
from textual.coordinate import Coordinate
from textual.widgets import DataTable, Footer, Header, Static

from hivepilot.orchestrator import Orchestrator
from hivepilot.plugins import PluginRecord

PLUGIN_COLUMNS = ("Name", "Source", "Status", "Type(s)", "Detail")

# Index of the "Status" column within PLUGIN_COLUMNS / each plugin_rows() row.
_STATUS_COLUMN_INDEX = 2

_ENV_KEY = "HIVEPILOT_PLUGINS_DISABLED"

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
    disabled: set[str] | None = None,
) -> list[tuple[str, str, str, str, str]]:
    """Build (name, source, status, type(s), detail) rows for the Loaded
    Plugins table.

    `disabled` is an optional set of plugin names to show as "disabled" —
    used by the TUI to reflect an in-session `space` toggle immediately
    (see `PluginManagerApp.action_toggle_plugin`). `loaded` itself only ever
    contains plugins `PluginManager` actually loaded (i.e. NOT already in
    `settings.plugins_disabled` at construction time), so every row defaults
    to "enabled" unless explicitly marked otherwise via `disabled`.
    """
    disabled = disabled or set()
    rows: list[tuple[str, str, str, str, str]] = []
    for record in loaded:
        caps = plugin_capabilities(record, runner_map, notifier_map, hooks)
        status = "disabled" if record.name in disabled else "enabled"
        rows.append(
            (record.name, record.source, status, _format_type_label(caps), _format_detail(caps))
        )
    return rows


def persist_plugins_disabled(disabled: list[str], *, env_path: Path | None = None) -> Path:
    """Upsert `HIVEPILOT_PLUGINS_DISABLED=<json list>` into the `.env` file
    `Settings` reads its overrides from.

    There is no dedicated writer for scalar/list `Settings` fields today
    (unlike `hivepilot.services.config_writer`'s ruamel round-trip writer,
    which only covers the 6 declarative YAML domain files — projects/roles/
    policies/groups/pipelines/tasks — none of which back `plugins_disabled`;
    every `Settings` field, including `plugins_enabled`/`plugins_disabled`,
    is sourced purely from env vars / the resolved `.env` file). This upserts
    the SAME dotenv file/format `Settings` already reads (see
    `hivepilot.config._resolve_env_file`) rather than inventing a new one —
    it preserves every other line verbatim and only replaces (or appends)
    the `HIVEPILOT_PLUGINS_DISABLED=` line.

    Effective on next start only: `PluginManager` scans/registers once, at
    construction — see module docstring.
    """
    if env_path is None:
        from hivepilot.config import Settings

        # Settings.model_config["env_file"] is resolved once, at class
        # definition/import time (see hivepilot.config._resolve_env_file) —
        # it will NOT reflect a HIVEPILOT_ENV_FILE change made after startup.
        env_path = Path(str(Settings.model_config.get("env_file") or ".env"))

    line = f"{_ENV_KEY}={json.dumps(sorted(disabled))}"

    lines: list[str] = []
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()

    for i, existing in enumerate(lines):
        if existing.startswith(f"{_ENV_KEY}="):
            lines[i] = line
            break
    else:
        lines.append(line)

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return env_path


class PluginManagerApp(App):
    """Interactive browser/inspector for loaded plugins, with an
    enable/disable toggle (`space`).

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
        # Deliberately "toggle_plugin", not "toggle" — see
        # action_toggle_plugin's docstring: "toggle" would resolve to
        # textual's own built-in DOMNode.action_toggle(attribute_name),
        # a different, incompatible action.
        ("space", "toggle_plugin", "Enable/Disable"),
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
        self._rows: list[tuple[str, str, str, str, str]] = []

    def compose(self) -> ComposeResult:
        yield Header()
        self.plugins_table: DataTable = DataTable(id="plugins")
        self.plugins_table.add_columns(*PLUGIN_COLUMNS)
        self.details: Static = Static("Select a plugin and press Enter for details.", id="details")
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

    def action_toggle_plugin(self) -> None:
        # NOT named `action_toggle` — textual's own `DOMNode.action_toggle
        # (attribute_name: str)` is a built-in generic action (toggles a
        # reactive attribute by name) with an incompatible signature;
        # shadowing it with a zero-arg override is a Liskov violation mypy
        # flags once textual's stubs are resolvable, and needlessly hides a
        # framework primitive future bindings elsewhere in this app might
        # want to use.
        self.toggle_selected()

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
        for name, source, status, type_label, detail in self._rows:
            self.plugins_table.add_row(name, source, status, type_label, detail)
        if self._rows:
            self.plugins_table.move_cursor(row=0, column=0)
        self.show_details()

    def _selected_row_index(self) -> int | None:
        if not self._rows:
            return None
        row_index = self.plugins_table.cursor_row
        if row_index is None or not (0 <= row_index < len(self._rows)):
            row_index = 0
        return row_index

    def show_details(self) -> None:
        row_index = self._selected_row_index()
        if row_index is None:
            self.details.update("No plugins loaded.")
            return
        name, source, status, type_label, detail = self._rows[row_index]
        self.details.update(f"{name} ({source}, {status}) — {type_label}\n{detail}")

    def toggle_selected(self) -> None:
        """Flip the highlighted plugin's presence in `settings.plugins_disabled`,
        persist it via `persist_plugins_disabled`, and update its row's status
        cell in place. Does NOT live-unregister the plugin — see module
        docstring: this only takes effect on the next start."""
        row_index = self._selected_row_index()
        if row_index is None:
            return

        from hivepilot.config import settings

        name = self._rows[row_index][0]
        current = set(settings.plugins_disabled)
        if name in current:
            current.discard(name)
            now_disabled = False
        else:
            current.add(name)
            now_disabled = True

        updated = sorted(current)
        settings.plugins_disabled = updated
        persist_plugins_disabled(updated)

        old_row = self._rows[row_index]
        new_status = "disabled" if now_disabled else "enabled"
        self._rows[row_index] = (
            old_row[0],
            old_row[1],
            new_status,
            old_row[3],
            old_row[4],
        )
        self.plugins_table.update_cell_at(Coordinate(row_index, _STATUS_COLUMN_INDEX), new_status)

        self.details.update(
            f"{name} {new_status}. Persisted to plugins_disabled — "
            "applies on next start (plugins load once at startup; "
            "no live reload)."
        )

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:  # type: ignore[override]
        # `event.data_table` (not `.table`) is the attribute on textual's
        # DataTable.RowHighlighted message — see hivepilot/ui/dashboard.py
        # for the identical (pre-existing, out-of-boundary-for-this-sprint)
        # `event.table` usage that hits the same AttributeError on
        # textual>=0.6x; logged, not fixed here (outside this sprint's file
        # boundaries).
        if event.data_table.id != "plugins":
            return
        self.show_details()
