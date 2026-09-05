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
from conftest import BUNDLED_PLUGINS

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


def test_probed_plugins_are_the_documented_set() -> None:
    """Adding a name here requires the sole-writer proof in the docstring."""
    # memory_events is attributed by backend column; hindsight joined HP-51.
    assert plugin_activity.probed_plugins() == frozenset(
        {"headroom", "obsidian", "hindsight"}
    )


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


def test_hindsight_counts_memory_events() -> None:
    memory_service.record_search(
        namespace="ns", query="q", result_count=2, actor="system", backend="hindsight"
    )
    memory_service.record_store(namespace="ns", key="k", actor="system", backend="hindsight")

    activity = plugin_activity.activity_for("hindsight")

    assert activity is not None
    assert activity.events == 2
    assert activity.evidence == "memory_events (backend=hindsight)"


def test_memory_event_writers_are_all_attributed_by_backend() -> None:
    """The successor to a tripwire that fired exactly as designed.

    This used to assert mem0 was the SOLE writer of `memory_events`, because
    `plugin_activity` attributed the whole table to it on that call-site fact
    alone. Its docstring said the fix, when a second writer appeared, was to
    add a backend column and filter on it -- not to relax the assertion.

    Obsidian was then instrumented, the test failed, and the column and filter
    landed. What must hold now is stronger: every plugin writing to that table
    has a probe, and every such probe filters by backend. A writer without one
    would silently inflate whichever plugin's row it landed in.
    """
    plugins_dir = BUNDLED_PLUGINS
    recorders = {"record_search", "record_store", "record_read"}

    writers = {
        path.stem
        for path in plugins_dir.glob("*.py")
        if any(call in path.read_text(encoding="utf-8") for call in recorders)
    }

    from hivepilot.services import plugin_activity

    unprobed = writers - plugin_activity.probed_plugins()
    assert not unprobed, (
        f"memory_events writers with no activity probe: {sorted(unprobed)}. "
        "Their events would be attributed to whichever plugin's probe does not "
        "filter them out -- add a probe with backend=<name>."
    )

    source = (
        Path(plugin_activity.__file__).read_text(encoding="utf-8")
        if hasattr(plugin_activity, "__file__") and plugin_activity.__file__
        else ""
    )
    for name in writers:
        assert f'backend="{name}"' in source, (
            f"probe for {name!r} does not filter memory_events by backend; "
            "its row would absorb the other backend's recalls"
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
