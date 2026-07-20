"""Tests for `plugins/autopilot_panel.py` (Mirador Panels sprint) -- the
plugin-contributed `autopilot_status` `panel` capability contribution over
the autopilot queue/policy read surface (`autopilot_queue.list_queue`/
`is_paused`/`is_stopped`/`spent_today_usd`, `autopilot_policy.
get_autopilot_policy`).

Loaded by file path (mirrors `tests/test_drift_panel.py`), never `import
plugins.autopilot_panel` -- that would insert a `plugins` package into
`sys.modules` and leak across the suite.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from hivepilot.config import settings

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN_PATH = _REPO_ROOT / "plugins" / "autopilot_panel.py"

_spec = importlib.util.spec_from_file_location(
    "hivepilot_test_autopilot_panel_plugin", _PLUGIN_PATH
)
assert _spec and _spec.loader
autopilot_panel = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(autopilot_panel)


# ---------------------------------------------------------------------------
# Opt-in gating
# ---------------------------------------------------------------------------


class TestOptInGating:
    def test_disabled_by_default_contributes_nothing(self) -> None:
        assert settings.autopilot_panel_enabled is False
        assert autopilot_panel.register() == {}

    def test_enabled_contributes_autopilot_status_panel(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "autopilot_panel_enabled", True, raising=False)

        hooks = autopilot_panel.register()
        assert [p["name"] for p in hooks["panels"]] == ["autopilot_status"]
        spec = hooks["panels"][0]
        assert spec["title"] == "Autopilot"
        assert spec["min_role"] == "read"
        assert callable(spec["fetch"])


# ---------------------------------------------------------------------------
# _fetch -- sections, empty queue
# ---------------------------------------------------------------------------


class TestFetchEmptyQueue:
    def test_empty_queue_yields_status_stat_plus_text(self) -> None:
        from hivepilot.services import autopilot_queue

        assert autopilot_queue.list_queue(tenant="default") == []
        result = autopilot_panel._fetch()
        sections = result["sections"]
        assert any(s["kind"] == "stat" for s in sections)
        assert any(
            s["kind"] == "text" and s["content"] == "Autopilot queue empty." for s in sections
        )


class TestFetchStatusStat:
    def test_active_status_is_ok(self) -> None:
        result = autopilot_panel._fetch()
        stat = next(s for s in result["sections"] if s["kind"] == "stat")
        assert stat["status"] == "ok"

    def test_paused_status_is_warn(self, monkeypatch) -> None:
        from hivepilot.services import autopilot_queue

        autopilot_queue.pause(tenant="default")
        result = autopilot_panel._fetch()
        stat = next(s for s in result["sections"] if s["kind"] == "stat")
        assert stat["status"] == "warn"

    def test_stopped_status_is_warn(self, monkeypatch) -> None:
        from hivepilot.services import autopilot_queue

        autopilot_queue.stop(tenant="default")
        result = autopilot_panel._fetch()
        stat = next(s for s in result["sections"] if s["kind"] == "stat")
        assert stat["status"] == "warn"


class TestFetchQueueCounts:
    def test_per_state_counts_reflect_queue(self) -> None:
        from hivepilot.services import autopilot_queue

        id1 = autopilot_queue.enqueue("acme-api", "groomer", "reason-a")
        autopilot_queue.promote(id1)
        autopilot_queue.enqueue("acme-api", "groomer", "reason-b")
        id3 = autopilot_queue.enqueue("acme-api", "groomer", "reason-c")
        autopilot_queue.mark(id3, "blocked")

        result = autopilot_panel._fetch()
        table = next(
            s
            for s in result["sections"]
            if s["kind"] == "table" and s["columns"] == ["state", "count"]
        )
        counts = {row[0]: row[1] for row in table["rows"]}
        assert counts["queued"] == "1"
        assert counts["proposed"] == "1"
        assert counts["blocked"] == "1"


class TestFetchBudgetBurnDown:
    def test_tenant_spent_stat_is_hoisted_and_called_once_with_tenant_default(
        self, monkeypatch
    ) -> None:
        """`spent_today_usd` is TENANT-WIDE (no per-project param) -- it must
        be called exactly ONCE (hoisted out of the per-project loop, never
        re-invoked per project) and rendered as its own `stat` section, never
        as a per-project table column (a human reading a per-project row must
        never mistake the tenant-wide figure for that project's own spend)."""
        from hivepilot.services import autopilot_policy, autopilot_queue

        autopilot_queue.enqueue("acme-api", "groomer", "x")
        autopilot_queue.enqueue("other-project", "groomer", "y")

        calls: list[dict] = []

        def _spy_spent_today_usd(**kwargs):
            calls.append(kwargs)
            return 12.5

        monkeypatch.setattr(autopilot_queue, "spent_today_usd", _spy_spent_today_usd)
        monkeypatch.setattr(
            autopilot_policy,
            "get_autopilot_policy",
            lambda project: autopilot_policy.AutopilotPolicy(
                auto_dispatch=[], require_approval=True, budget_daily_usd=50.0
            ),
        )

        result = autopilot_panel._fetch()

        assert len(calls) == 1
        assert calls[0].get("tenant") == "default"

        tenant_spent_stat = next(
            s
            for s in result["sections"]
            if s["kind"] == "stat" and s["label"] == "tenant spent today"
        )
        assert tenant_spent_stat["value"] == "$12.50"

        budget_table = next(
            s
            for s in result["sections"]
            if s["kind"] == "table" and s["columns"] == ["project", "budget_usd"]
        )
        rows_by_project = {row[0]: row for row in budget_table["rows"]}
        # The tenant-wide spend figure must NEVER appear as a per-project
        # table cell -- only project + its OWN budget_daily_usd.
        assert rows_by_project["acme-api"] == ["acme-api", "50.00"]
        assert rows_by_project["other-project"] == ["other-project", "50.00"]
        assert "12.50" not in str(budget_table)

    def test_unset_budget_renders_as_dash(self, monkeypatch) -> None:
        from hivepilot.services import autopilot_policy, autopilot_queue

        autopilot_queue.enqueue("acme-api", "groomer", "x")
        monkeypatch.setattr(autopilot_queue, "spent_today_usd", lambda **kw: 0.0)
        monkeypatch.setattr(
            autopilot_policy,
            "get_autopilot_policy",
            lambda project: autopilot_policy.AutopilotPolicy(),
        )

        result = autopilot_panel._fetch()
        budget_table = next(
            s
            for s in result["sections"]
            if s["kind"] == "table" and s["columns"] == ["project", "budget_usd"]
        )
        assert budget_table["rows"][0][1] == "-"


class TestFetchAwaitingHuman:
    def test_only_proposed_queued_blocked_appear(self) -> None:
        from hivepilot.services import autopilot_queue

        proposed_id = autopilot_queue.enqueue("acme-api", "groomer", "needs review")
        queued_id = autopilot_queue.enqueue("acme-api", "groomer", "queued reason")
        autopilot_queue.promote(queued_id)
        blocked_id = autopilot_queue.enqueue("acme-api", "groomer", "blocked reason")
        autopilot_queue.mark(blocked_id, "blocked")
        done_id = autopilot_queue.enqueue("acme-api", "groomer", "done reason")
        autopilot_queue.mark(done_id, "done")

        result = autopilot_panel._fetch()
        awaiting_table = next(
            s
            for s in result["sections"]
            if s["kind"] == "table" and s["columns"] == ["project", "pipeline", "state", "reason"]
        )
        states = {row[2] for row in awaiting_table["rows"]}
        assert states == {"proposed", "queued", "blocked"}
        assert "done" not in states
        reasons = {row[3] for row in awaiting_table["rows"]}
        assert "needs review" in reasons

        assert proposed_id and queued_id and blocked_id and done_id  # rows created

    def test_untrusted_reason_not_editorialized(self) -> None:
        from hivepilot.services import autopilot_queue

        autopilot_queue.enqueue("acme-api", "groomer", "<script>alert(1)</script>")
        result = autopilot_panel._fetch()
        awaiting_table = next(
            s
            for s in result["sections"]
            if s["kind"] == "table" and s["columns"] == ["project", "pipeline", "state", "reason"]
        )
        assert awaiting_table["rows"][0][3] == "<script>alert(1)</script>"


class TestTenantScoping:
    def test_all_reads_use_tenant_default(self, monkeypatch) -> None:
        from hivepilot.services import autopilot_queue

        captured: list[dict] = []

        def _spy_list_queue(**kwargs):
            captured.append(("list_queue", kwargs))
            return []

        def _spy_is_paused(**kwargs):
            captured.append(("is_paused", kwargs))
            return False

        def _spy_is_stopped(**kwargs):
            captured.append(("is_stopped", kwargs))
            return False

        monkeypatch.setattr(autopilot_queue, "list_queue", _spy_list_queue)
        monkeypatch.setattr(autopilot_queue, "is_paused", _spy_is_paused)
        monkeypatch.setattr(autopilot_queue, "is_stopped", _spy_is_stopped)

        autopilot_panel._fetch()

        for _name, kwargs in captured:
            assert kwargs.get("tenant") == "default"


class TestNeverCallsWriteFunctions:
    def test_never_calls_enqueue_or_mark(self, monkeypatch) -> None:
        from hivepilot.services import autopilot_queue

        def _boom(*args, **kwargs):
            raise AssertionError("autopilot_panel must never write to autopilot_queue")

        monkeypatch.setattr(autopilot_queue, "enqueue", _boom)
        monkeypatch.setattr(autopilot_queue, "mark", _boom)
        monkeypatch.setattr(autopilot_queue, "promote", _boom)
        monkeypatch.setattr(autopilot_queue, "veto", _boom)
        monkeypatch.setattr(autopilot_queue, "pause", _boom)
        monkeypatch.setattr(autopilot_queue, "resume", _boom)
        monkeypatch.setattr(autopilot_queue, "stop", _boom)

        autopilot_panel._fetch()
