"""Tests for `hivepilot.services.plugin_activity`.

The `_isolate_state_db` autouse fixture in `conftest.py` redirects the DB to a
per-test tmp file, so these tests never touch the real ``./state.db``.

The distinction under test is the point of the module: a plugin that is
installed and configured is not the same as a plugin that has run. Health
answers the first question; these probes answer the second. Several tests here
exist specifically to stop the two from collapsing back into one another --
notably `test_never_run_reads_zero_not_none`, which pins that "measured, and it
has done nothing" must stay distinguishable from "cannot be measured".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.services import headroom_metrics, memory_service, plugin_activity

# ---------------------------------------------------------------------------
# Registry: measurable vs presence-only
# ---------------------------------------------------------------------------


def test_unprobed_plugin_is_not_measurable() -> None:
    """A plugin with no telemetry must report `None`, never a fabricated zero.

    `rtk` and `gh` are PATH checks: nothing records when they are used. Showing
    them "0 events" would read as "installed but idle", which is a claim the
    data cannot support.
    """
    assert "rtk" not in plugin_activity.probed_plugins()
    assert plugin_activity.activity_for("rtk") is None


def test_probed_plugins_are_the_documented_two() -> None:
    """Adding a name here requires the sole-writer proof in the docstring."""
    assert plugin_activity.probed_plugins() == frozenset({"headroom", "mem0"})


# ---------------------------------------------------------------------------
# headroom
# ---------------------------------------------------------------------------


def test_never_run_reads_zero_not_none() -> None:
    """An empty table is a *reading*, not an absence of one.

    This is the case that made the module necessary: headroom sat at
    `status="ok"` having never once run. That must surface as
    `events=0, last_used=None` -- a measurable plugin with nothing to show --
    and must not be confused with an unmeasurable plugin.
    """
    activity = plugin_activity.activity_for("headroom")

    assert activity is not None
    assert activity.events == 0
    assert activity.last_used is None
    assert activity.evidence == "headroom_compressions + headroom_skips"


def test_counts_both_compressions_and_skips() -> None:
    """headroom splits its record across two tables; it is still one plugin.

    A skip is evidence the plugin *ran* and declined -- exactly as much proof
    of life as a compression, and the signal that separates "never invoked"
    from "invoked and ineffective".
    """
    headroom_metrics.record_compression(step="build", chars_before=1000, chars_after=400, ratio=0.4)
    headroom_metrics.record_skip(step="build", reason="non_shrinking", chars=120)
    headroom_metrics.record_skip(step="test", reason="already_compressed", chars=80)

    activity = plugin_activity.activity_for("headroom")

    assert activity is not None
    assert activity.events == 3
    assert activity.last_used is not None


def test_last_used_is_not_bounded_by_the_window() -> None:
    """A long-idle plugin must still be able to say *when* it last ran.

    Collapsing an old timestamp to `None` would make "ran once, months ago"
    indistinguishable from "never ran" -- the very conflation this module
    exists to prevent.
    """
    headroom_metrics.record_compression(step="old", chars_before=900, chars_after=300, ratio=0.33)
    _backdate("headroom_compressions", days=90)

    activity = plugin_activity.activity_for("headroom", window_days=30)

    assert activity is not None
    assert activity.events == 0, "a 90-day-old event is outside a 30-day window"
    assert activity.last_used is not None, "but it still happened, and we know when"


# ---------------------------------------------------------------------------
# mem0
# ---------------------------------------------------------------------------


def test_mem0_counts_memory_events() -> None:
    memory_service.record_search(namespace="ns", query="q", result_count=5, actor="system")
    memory_service.record_store(namespace="ns", key="k", actor="system")

    activity = plugin_activity.activity_for("mem0")

    assert activity is not None
    assert activity.events == 2
    assert activity.evidence == "memory_events"


def test_only_mem0_records_memory_events() -> None:
    """Guard for an attribution that rests on a call-site fact, not a column.

    `memory_events` has no backend column, so `plugin_activity` attributes the
    whole table to `mem0` purely because `plugins/mem0.py` is its only writer.
    If a second memory plugin (obsidian is the obvious candidate) starts
    recording, mem0's activity row would silently absorb the other plugin's
    work and read as busier than it is.

    This test fails the moment that happens. When it does, the fix is to add a
    backend column to `memory_events` and filter on it -- not to relax this
    assertion.
    """
    plugins_dir = Path(__file__).resolve().parent.parent / "plugins"
    recorders = {"record_search", "record_store", "record_read"}

    writers = {
        path.name
        for path in plugins_dir.glob("*.py")
        if any(call in path.read_text(encoding="utf-8") for call in recorders)
    }

    assert writers == {"mem0.py"}, (
        f"memory_events gained writers beyond mem0: {sorted(writers - {'mem0.py'})}. "
        "plugin_activity attributes that whole table to mem0 -- add a backend "
        "column and filter on it before landing this."
    )


# ---------------------------------------------------------------------------
# Tenant scoping -- activity is partitioned even though health is not
# ---------------------------------------------------------------------------


def test_activity_is_scoped_to_the_caller_tenant() -> None:
    headroom_metrics.record_skip(tenant="acme", step="s", reason="non_shrinking")
    headroom_metrics.record_skip(tenant="other", step="s", reason="non_shrinking")
    headroom_metrics.record_skip(tenant="other", step="s", reason="non_shrinking")

    acme = plugin_activity.activity_for("headroom", tenant="acme")

    assert acme is not None
    assert acme.events == 1, "one tenant must not see another's activity"


def test_unscoped_tenant_sees_every_tenant() -> None:
    """`tenant=None` is the admin view, matching `_analytics_tenant`."""
    headroom_metrics.record_skip(tenant="acme", step="s", reason="non_shrinking")
    headroom_metrics.record_skip(tenant="other", step="s", reason="non_shrinking")

    everyone = plugin_activity.activity_for("headroom", tenant=None)

    assert everyone is not None
    assert everyone.events == 2


# ---------------------------------------------------------------------------
# Failure containment -- plugin health must never 500 on a bad read
# ---------------------------------------------------------------------------


def test_probe_failure_returns_none_instead_of_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(**_kwargs: object) -> plugin_activity.PluginActivity:
        raise RuntimeError("table is corrupt")

    monkeypatch.setitem(plugin_activity._PROBES, "headroom", boom)

    assert plugin_activity.activity_for("headroom") is None
    assert "headroom" in plugin_activity.probed_plugins(), (
        "a failed read must not make a measurable plugin look unmeasurable"
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _backdate(table: str, *, days: int) -> None:
    """Push every row in *table* *days* into the past."""
    from hivepilot.services import db

    with db.connect() as conn:
        conn.execute(f"UPDATE {table} SET ts = datetime('now', '-{days} days')")
