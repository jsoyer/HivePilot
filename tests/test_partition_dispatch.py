"""Tests for parallel partition dispatch, the CLI, the API surface and the
PR-URL capture (propose -> ratify -> dispatch PRD, Sprint 3, spec sections 7
and 8).

The crash-recovery half (the startup reconciler) lives in the sibling module
`tests/test_partition_reconcile.py`.

`Orchestrator.run_pipeline` is mocked throughout: this sprint owns the
DISPATCH mechanics -- the wave plan, the claim/commit journal, the
concurrency cap, the kill switch and budget re-checks between waves, the
skipped-vs-failed distinction and the PR link -- not the pipeline engine,
which is already covered elsewhere.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from hivepilot.partition import load_partition
from hivepilot.services import autopilot_queue, partition_service, state_service

# ---------------------------------------------------------------------------
# Live-config fixture -- the same real-YAML-through-the-real-resolver shape
# Sprint 2's tests use. The gate and the dispatcher both read LIVE config, so
# mocking the loader would test the opposite of the property under test.
# ---------------------------------------------------------------------------

PROJECTS_YAML = """
projects:
  acme-api:
    path: /tmp/acme-api
    modules:
      core: apps/core
  acme-web:
    path: /tmp/acme-web
"""

PIPELINES_YAML = """
pipelines:
  bugfix:
    description: fix a bug
    stages:
      - name: fix
        task: implement
  ship-it:
    description: ship a change
    stages:
      - name: ship
        task: ship
"""

TASKS_YAML = """
tasks:
  implement:
    description: implement something
    role: developer
  ship:
    description: push a branch and open a PR
    role: developer
    git:
      push: true
      create_pr: true
"""

POLICIES_YAML = """
policies:
  default:
    require_approval: true
  projects:
    acme-api:
      outward_actions:
        - git_push
        - forge_pr
      budget_daily_usd: 100.0
      max_partition_cost_usd: 50.0
      max_task_wall_clock_seconds: 3600
    acme-web:
      outward_actions:
        - git_push
        - forge_pr
      budget_daily_usd: 100.0
      max_partition_cost_usd: 50.0
      max_task_wall_clock_seconds: 3600
"""


@pytest.fixture
def live_config(tmp_path, monkeypatch):
    from hivepilot.config import settings
    from hivepilot.services import policy_service

    (tmp_path / "projects.yaml").write_text(PROJECTS_YAML, encoding="utf-8")
    (tmp_path / "pipelines.yaml").write_text(PIPELINES_YAML, encoding="utf-8")
    (tmp_path / "tasks.yaml").write_text(TASKS_YAML, encoding="utf-8")
    (tmp_path / "policies.yaml").write_text(POLICIES_YAML, encoding="utf-8")
    monkeypatch.setattr(settings, "config_repo", str(tmp_path), raising=False)
    policy_service.reload_policies()
    yield tmp_path
    policy_service.reload_policies()


@pytest.fixture(autouse=True)
def _zero_spend(monkeypatch):
    monkeypatch.setattr(autopilot_queue, "spent_today_usd", lambda *, tenant="default": 0.0)


@pytest.fixture(autouse=True)
def _generous_parallelism(monkeypatch):
    """Default config caps `claude` at 1, which is exactly the honesty point
    of `effective_parallelism` -- but it would also serialize every dispatch
    test. Individual tests that assert ON the cap set their own values."""
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "claude_max_concurrency", 8, raising=False)
    monkeypatch.setattr(settings, "concurrency_limit", 8, raising=False)


def _task(task_id: str, **overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": task_id,
        "title": f"Task {task_id}",
        "project": "acme-api",
        "pipeline": "bugfix",
        "prompt": f"do the {task_id} work",
        "depends_on": [],
        "budget": {"wall_clock_seconds": 30, "cost_usd": 1.0},
        "done_when": ["a repro test passes"],
        "outward": False,
    }
    task.update(overrides)
    return task


def _plan(*tasks: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    plan: dict[str, Any] = {
        "partition_version": 1,
        "source": {"kind": "text", "ref": "docs/bug-1234.md", "digest": "sha256:aaa"},
        "proposer": {"role": "partitioner", "pipeline": "propose-partition", "run_id": 4711},
        "policy": {"max_parallel": 3, "on_task_failure": "continue"},
        "tasks": list(tasks) or [_task("t1")],
    }
    plan.update(overrides)
    return plan


def _ratified(plan: dict[str, Any], *, consent: bool = False, tenant: str = "default") -> str:
    """Create + ratify a partition; return its id."""
    plan_json = json.dumps(plan)
    partition_id = partition_service.create_partition(plan_json=plan_json, tenant=tenant)
    row = partition_service.get_partition(partition_id, tenant=tenant)
    assert row is not None
    partition_service.ratify_partition(
        partition_id,
        partition_json=plan_json,
        outward_consent=consent,
        approver="operator",
        expected_digest=str(row["proposed_digest"]),
        tenant=tenant,
    )
    return partition_id


class FakeOrchestrator:
    """Records every `run_pipeline` call and tracks peak concurrency.

    `results_for` maps a task's PROMPT to the success boolean to report, so a
    test can make exactly one task in a wave fail.
    """

    def __init__(
        self,
        *,
        failures: set[str] | None = None,
        hold: float = 0.0,
        on_call: Any = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.failures = failures or set()
        self.hold = hold
        self.on_call = on_call
        self._lock = threading.Lock()
        self._in_flight = 0
        self.peak_in_flight = 0

    def run_pipeline(self, **kwargs: Any) -> list[Any]:
        with self._lock:
            self.calls.append(kwargs)
            self._in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self._in_flight)
        try:
            if self.hold:
                time.sleep(self.hold)
            if self.on_call is not None:
                self.on_call(kwargs)
            prompt = str(kwargs.get("extra_prompt") or "")
            success = prompt not in self.failures
            return [SimpleNamespace(project="acme-api", target="bugfix", success=success)]
        finally:
            with self._lock:
                self._in_flight -= 1

    @property
    def prompts(self) -> list[str]:
        return [str(call.get("extra_prompt")) for call in self.calls]


def _statuses(partition_id: str) -> dict[str, str]:
    return {
        str(row["task_id"]): str(row["status"])
        for row in partition_service.list_partition_tasks(partition_id)
    }


# ---------------------------------------------------------------------------
# The wave planner
# ---------------------------------------------------------------------------


class TestWavePlanner:
    def test_independent_tasks_are_all_one_wave(self, live_config) -> None:
        plan = load_partition(json.dumps(_plan(_task("a"), _task("b"), _task("c"))))
        assert partition_service.plan_waves(plan) == (("a", "b", "c"),)

    def test_waves_are_topological_levels_of_the_depends_on_dag(self, live_config) -> None:
        plan = load_partition(
            json.dumps(
                _plan(
                    _task("a"),
                    _task("b", depends_on=["a"]),
                    _task("c", depends_on=["a"]),
                    _task("d", depends_on=["b", "c"]),
                )
            )
        )
        assert partition_service.plan_waves(plan) == (("a",), ("b", "c"), ("d",))

    def test_wave_order_is_deterministic(self, live_config) -> None:
        plan = load_partition(json.dumps(_plan(_task("z"), _task("m"), _task("a"))))
        assert partition_service.plan_waves(plan) == (("a", "m", "z"),)


# ---------------------------------------------------------------------------
# Effective parallelism -- surfaced, never assumed (spec section 7)
# ---------------------------------------------------------------------------


class TestEffectiveParallelism:
    def test_claude_max_concurrency_of_one_makes_three_parallel_agents_one_agent_thrice(
        self, live_config, monkeypatch
    ) -> None:
        """The headline honesty case from the spec: on a DEFAULT install,
        `max_parallel: 3` really is one agent, three times."""
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "claude_max_concurrency", 1, raising=False)
        monkeypatch.setattr(settings, "concurrency_limit", 8, raising=False)

        plan = load_partition(json.dumps(_plan(_task("a"), _task("b"), _task("c"))))
        assessment = partition_service.effective_parallelism(plan)

        assert assessment.requested == 3
        assert assessment.effective == 1
        assert any("claude_max_concurrency=1" in note for note in assessment.notes)

    def test_settings_concurrency_limit_also_caps(self, live_config, monkeypatch) -> None:
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "claude_max_concurrency", 8, raising=False)
        monkeypatch.setattr(settings, "concurrency_limit", 2, raising=False)

        plan = load_partition(json.dumps(_plan(_task("a"), _task("b"), _task("c"))))
        assert partition_service.effective_parallelism(plan).effective == 2

    def test_unresolvable_pipeline_assumes_the_throttled_runner_cap(
        self, live_config, monkeypatch
    ) -> None:
        """Fail-closed: "I cannot tell what this runs" must never be reported
        as "unthrottled, run them all at once"."""
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "claude_max_concurrency", 1, raising=False)
        monkeypatch.setattr(
            partition_service.project_service,
            "load_pipelines",
            lambda: (_ for _ in ()).throw(RuntimeError("config unreadable")),
        )
        plan = load_partition(json.dumps(_plan(_task("a"), _task("b"))))
        assessment = partition_service.effective_parallelism(plan)

        assert assessment.effective == 1
        assert any("fail-closed" in note for note in assessment.notes)

    def test_effective_parallelism_is_never_zero(self, live_config, monkeypatch) -> None:
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "concurrency_limit", 0, raising=False)
        plan = load_partition(json.dumps(_plan(_task("a"))))
        assert partition_service.effective_parallelism(plan).effective == 1


# ---------------------------------------------------------------------------
# The dispatcher (spec section 7)
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_three_independent_tasks_submit_exactly_three_runs_and_three_claim_rows(
        self, live_config
    ) -> None:
        partition_id = _ratified(_plan(_task("a"), _task("b"), _task("c")))
        orch = FakeOrchestrator()

        outcome = partition_service.dispatch_partition(partition_id, orchestrator=orch)

        assert len(orch.calls) == 3
        assert sorted(outcome.dispatched) == ["a", "b", "c"]
        assert outcome.committed == ("a", "b", "c")
        rows = partition_service.list_partition_tasks(partition_id)
        assert len(rows) == 3
        # Every row went through claim -> running -> committed and carries the
        # claim owner + a real run id.
        assert all(row["claimed_by"] for row in rows)
        assert all(row["run_id"] is not None for row in rows)
        assert all(str(row["status"]) == "committed" for row in rows)
        assert len({row["run_id"] for row in rows}) == 3

    def test_a_proposed_partition_never_dispatches(self, live_config) -> None:
        """The invariant: no code path starts a task from `proposed`."""
        plan_json = json.dumps(_plan(_task("a")))
        partition_id = partition_service.create_partition(plan_json=plan_json)
        orch = FakeOrchestrator()

        outcome = partition_service.dispatch_partition(partition_id, orchestrator=orch)

        assert orch.calls == []
        assert outcome.dispatched == ()
        assert outcome.halted_reason is not None
        assert partition_service.get_partition(partition_id)["status"] == "proposed"

    def test_dependent_task_never_starts_and_is_skipped_not_failed_when_prereq_fails(
        self, live_config
    ) -> None:
        partition_id = _ratified(
            _plan(
                _task("root"),
                _task("child", depends_on=["root"]),
                _task("grandchild", depends_on=["child"]),
            )
        )
        orch = FakeOrchestrator(failures={"do the root work"})

        outcome = partition_service.dispatch_partition(partition_id, orchestrator=orch)

        assert orch.prompts == ["do the root work"]
        statuses = _statuses(partition_id)
        assert statuses["root"] == "failed"
        # `skipped`, NEVER `failed`: the dependents did nothing wrong.
        assert statuses["child"] == "skipped"
        assert statuses["grandchild"] == "skipped"
        assert set(outcome.skipped) == {"child", "grandchild"}
        assert outcome.failed == ("root",)

    def test_independent_siblings_still_run_when_a_peer_fails_under_continue(
        self, live_config
    ) -> None:
        partition_id = _ratified(
            _plan(_task("a"), _task("b"), policy={"max_parallel": 2, "on_task_failure": "continue"})
        )
        orch = FakeOrchestrator(failures={"do the a work"})

        partition_service.dispatch_partition(partition_id, orchestrator=orch)

        assert sorted(orch.prompts) == ["do the a work", "do the b work"]
        assert _statuses(partition_id) == {"a": "failed", "b": "committed"}

    def test_halt_policy_cancels_not_yet_started_tasks(self, live_config) -> None:
        partition_id = _ratified(
            _plan(
                _task("a"),
                _task("later", depends_on=["a"]),
                _task("later2", depends_on=["a"]),
                policy={"max_parallel": 2, "on_task_failure": "halt"},
            )
        )
        orch = FakeOrchestrator(failures={"do the a work"})

        outcome = partition_service.dispatch_partition(partition_id, orchestrator=orch)

        assert orch.prompts == ["do the a work"]
        statuses = _statuses(partition_id)
        assert statuses["later"] == "cancelled"
        assert statuses["later2"] == "cancelled"
        assert set(outcome.cancelled) == {"later", "later2"}
        assert outcome.halted_reason is not None and "halt" in outcome.halted_reason

    def test_pause_halts_the_next_wave_within_one_wave_boundary(self, live_config) -> None:
        """`hivepilot autopilot pause` reaches the dispatcher because
        `is_paused` is re-checked BEFORE each wave, not once at the start."""
        partition_id = _ratified(_plan(_task("first"), _task("second", depends_on=["first"])))

        def _pause_after_first(kwargs: dict) -> None:
            if kwargs.get("extra_prompt") == "do the first work":
                autopilot_queue.pause(tenant="default")

        orch = FakeOrchestrator(on_call=_pause_after_first)
        outcome = partition_service.dispatch_partition(partition_id, orchestrator=orch)

        assert orch.prompts == ["do the first work"]
        assert _statuses(partition_id)["second"] == "pending"
        assert outcome.halted_reason is not None and "paused" in outcome.halted_reason
        # Halted, not finished: `dispatching` never auto-completes.
        assert partition_service.get_partition(partition_id)["status"] == "dispatching"

    def test_pause_before_the_first_wave_dispatches_nothing(self, live_config) -> None:
        partition_id = _ratified(_plan(_task("a")))
        autopilot_queue.pause(tenant="default")
        orch = FakeOrchestrator()

        outcome = partition_service.dispatch_partition(partition_id, orchestrator=orch)

        assert orch.calls == []
        assert outcome.halted_reason is not None
        assert _statuses(partition_id) == {"a": "pending"}

    def test_max_parallel_is_respected(self, live_config) -> None:
        partition_id = _ratified(
            _plan(
                *[_task(f"t{i}") for i in range(6)],
                policy={"max_parallel": 2, "on_task_failure": "continue"},
            )
        )
        orch = FakeOrchestrator(hold=0.05)

        outcome = partition_service.dispatch_partition(partition_id, orchestrator=orch)

        assert len(orch.calls) == 6
        assert outcome.effective_parallelism == 2
        assert orch.peak_in_flight <= 2

    def test_budget_is_rechecked_between_waves_and_wave_two_is_halted_when_over(
        self, live_config, monkeypatch
    ) -> None:
        partition_id = _ratified(_plan(_task("first"), _task("second", depends_on=["first"])))
        spend = {"value": 0.0}

        def _spent(*, tenant: str = "default") -> float:
            return spend["value"]

        monkeypatch.setattr(autopilot_queue, "spent_today_usd", _spent)

        def _blow_the_budget(kwargs: dict) -> None:
            # acme-api's budget_daily_usd is 100.0
            spend["value"] = 150.0

        orch = FakeOrchestrator(on_call=_blow_the_budget)
        outcome = partition_service.dispatch_partition(partition_id, orchestrator=orch)

        assert orch.prompts == ["do the first work"]
        assert _statuses(partition_id)["second"] == "pending"
        assert outcome.halted_reason is not None
        assert "budget" in outcome.halted_reason

    def test_an_unresolvable_spend_halts_rather_than_spending(
        self, live_config, monkeypatch
    ) -> None:
        # Ratify while spend IS resolvable, then break it: the property under
        # test is the DISPATCHER's re-check, not the ratification gate's
        # (which has its own coverage in test_partition_ratify_validation.py).
        partition_id = _ratified(_plan(_task("a")))

        def _boom(*, tenant: str = "default") -> float:
            raise RuntimeError("analytics unavailable")

        monkeypatch.setattr(autopilot_queue, "spent_today_usd", _boom)
        orch = FakeOrchestrator()

        outcome = partition_service.dispatch_partition(partition_id, orchestrator=orch)

        assert orch.calls == []
        assert outcome.halted_reason is not None
        assert "spend" in outcome.halted_reason

    def test_a_second_dispatch_never_re_runs_a_completed_task(self, live_config) -> None:
        partition_id = _ratified(_plan(_task("a")))
        orch = FakeOrchestrator()
        partition_service.dispatch_partition(partition_id, orchestrator=orch)

        again = partition_service.dispatch_partition(partition_id, orchestrator=orch, resume=True)

        assert len(orch.calls) == 1
        assert again.dispatched == ()

    def test_outward_consent_is_threaded_to_auto_git(self, live_config) -> None:
        consented = _ratified(_plan(_task("a", pipeline="ship-it")), consent=True)
        orch = FakeOrchestrator()
        partition_service.dispatch_partition(consented, orchestrator=orch)
        assert orch.calls[0]["auto_git"] is True

        withheld = _ratified(_plan(_task("b")))
        orch2 = FakeOrchestrator()
        partition_service.dispatch_partition(withheld, orchestrator=orch2)
        assert orch2.calls[0]["auto_git"] is False

    def test_an_empty_result_list_is_a_failure_not_a_commit(self, live_config) -> None:
        """Fail-closed: absence of evidence is not evidence of success."""
        partition_id = _ratified(_plan(_task("a")))
        orch = SimpleNamespace(run_pipeline=lambda **kwargs: [])

        partition_service.dispatch_partition(partition_id, orchestrator=orch)

        assert _statuses(partition_id) == {"a": "failed"}

    def test_a_raising_pipeline_marks_the_task_failed_never_committed(self, live_config) -> None:
        partition_id = _ratified(_plan(_task("a")))

        def _boom(**kwargs: Any) -> list[Any]:
            raise RuntimeError("the runner exploded")

        partition_service.dispatch_partition(
            partition_id, orchestrator=SimpleNamespace(run_pipeline=_boom)
        )

        assert _statuses(partition_id) == {"a": "failed"}
        assert partition_service.get_partition(partition_id)["status"] == "failed"

    def test_partition_completes_only_when_every_task_is_terminal(self, live_config) -> None:
        partition_id = _ratified(_plan(_task("a"), _task("b")))
        partition_service.dispatch_partition(partition_id, orchestrator=FakeOrchestrator())
        assert partition_service.get_partition(partition_id)["status"] == "completed"


# ---------------------------------------------------------------------------
# `drain_one` never touches partition tasks (spec section 7)
# ---------------------------------------------------------------------------


class TestQueueIsolation:
    def test_dispatch_writes_partition_task_queue_rows(self, live_config) -> None:
        partition_id = _ratified(_plan(_task("a"), _task("b")))
        partition_service.dispatch_partition(partition_id, orchestrator=FakeOrchestrator())

        rows = autopilot_queue.list_queue(
            tenant="default", kind=autopilot_queue.KIND_PARTITION_TASK
        )
        assert len(rows) == 2
        assert {row.kind for row in rows} == {autopilot_queue.KIND_PARTITION_TASK}
        # The journal row links back to its queue row.
        assert all(
            task_row["queue_id"] is not None
            for task_row in partition_service.list_partition_tasks(partition_id)
        )

    def test_drain_one_never_picks_a_partition_task_row(self, live_config) -> None:
        """The unattended autopilot tick must never re-dispatch human-ratified
        partition work through `autopilot_gate` (whose `require_approval ==
        False` condition is the exact inverse of a ratified plan)."""
        autopilot_queue.enqueue(
            "acme-api",
            "bugfix",
            "a partition task",
            state="proposed",
            kind=autopilot_queue.KIND_PARTITION_TASK,
        )
        autopilot_queue.enqueue(
            "acme-api",
            "bugfix",
            "a partition task, queued",
            state="queued",
            kind=autopilot_queue.KIND_PARTITION_TASK,
        )

        assert autopilot_queue.next_dispatchable(tenant="default") is None

        orch = FakeOrchestrator()
        assert autopilot_queue.drain_one(orch, tenant="default") is None
        assert orch.calls == []

    def test_a_dispatched_partition_leaves_drain_one_with_nothing_to_do(self, live_config) -> None:
        partition_id = _ratified(_plan(_task("a")))
        partition_service.dispatch_partition(partition_id, orchestrator=FakeOrchestrator())

        orch = FakeOrchestrator()
        assert autopilot_queue.drain_one(orch, tenant="default") is None
        assert orch.calls == []


# ---------------------------------------------------------------------------
# The PR URL -- recorded when a forge reports one, NULL when it does not
# ---------------------------------------------------------------------------


class TestForgePrUrl:
    def test_extract_pr_url_reads_the_url_gh_prints(self) -> None:
        from hivepilot.forges.provider import extract_pr_url

        assert (
            extract_pr_url("https://github.com/acme/api/pull/42\n")
            == "https://github.com/acme/api/pull/42"
        )

    def test_extract_pr_url_never_invents_one(self) -> None:
        from hivepilot.forges.provider import extract_pr_url

        for raw in (None, "", "   ", "Creating pull request...", 42, object(), "/pull/42"):
            assert extract_pr_url(raw) is None

    def test_github_open_pr_returns_the_url_gh_reported(self, tmp_path) -> None:
        from unittest.mock import patch

        from hivepilot.forges.github import GitHubForge
        from hivepilot.models import GitActions, ProjectConfig

        project = ProjectConfig(path=tmp_path, default_branch="main")
        with patch("hivepilot.forges.github.subprocess.run") as run_mock:
            run_mock.return_value = SimpleNamespace(
                stdout="https://github.com/acme/api/pull/7\n", returncode=0
            )
            url = GitHubForge().open_pr(
                project=project, branch="hivepilot/x", git=GitActions(create_pr=True)
            )
        assert url == "https://github.com/acme/api/pull/7"

    def test_github_open_pr_returns_none_when_gh_printed_no_url(self, tmp_path) -> None:
        from unittest.mock import patch

        from hivepilot.forges.github import GitHubForge
        from hivepilot.models import GitActions, ProjectConfig

        project = ProjectConfig(path=tmp_path, default_branch="main")
        with patch("hivepilot.forges.github.subprocess.run") as run_mock:
            run_mock.return_value = SimpleNamespace(stdout="", returncode=0)
            url = GitHubForge().open_pr(
                project=project, branch="hivepilot/x", git=GitActions(create_pr=True)
            )
        assert url is None

    def test_extract_pr_url_from_response_reads_forgejo_html_url(self) -> None:
        from hivepilot.forges.provider import extract_pr_url_from_response

        response = SimpleNamespace(json=lambda: {"html_url": "https://git.acme.dev/a/b/pulls/3"})
        assert (
            extract_pr_url_from_response(response, "html_url") == "https://git.acme.dev/a/b/pulls/3"
        )

    def test_extract_pr_url_from_response_is_none_on_every_unreadable_shape(self) -> None:
        from hivepilot.forges.provider import extract_pr_url_from_response

        def _raises() -> dict:
            raise ValueError("not json")

        for response in (
            None,
            SimpleNamespace(),
            SimpleNamespace(json=_raises),
            SimpleNamespace(json=lambda: "not a mapping"),
            SimpleNamespace(json=lambda: {}),
            SimpleNamespace(json=lambda: {"html_url": None}),
            SimpleNamespace(json=lambda: {"html_url": "/relative/3"}),
        ):
            assert extract_pr_url_from_response(response, "html_url") is None


class TestPrUrlJournalling:
    def test_git_service_create_pr_returns_and_ledgers_the_forge_url(
        self, tmp_path, monkeypatch
    ) -> None:
        from hivepilot.models import GitActions, ProjectConfig
        from hivepilot.services import git_service

        class _Forge:
            def open_pr(self, *, project, branch, git):  # noqa: ANN001, ANN202
                return "https://github.com/acme/api/pull/9"

        monkeypatch.setattr(git_service, "resolve_forge", lambda project: _Forge())
        project = ProjectConfig(path=tmp_path / "acme-api")
        mark = git_service.pr_ledger_mark()
        url = git_service.create_pr(
            project=project, branch="hivepilot/x", git=GitActions(create_pr=True)
        )

        assert url == "https://github.com/acme/api/pull/9"
        assert git_service.pr_urls_since(mark, project="acme-api") == (
            "https://github.com/acme/api/pull/9",
        )

    def test_a_legacy_forge_returning_none_ledgers_nothing(self, tmp_path, monkeypatch) -> None:
        """An out-of-tree forge plugin written against the pre-widening
        `-> None` signature must never contribute a fabricated link."""
        from hivepilot.models import GitActions, ProjectConfig
        from hivepilot.services import git_service

        class _LegacyForge:
            def open_pr(self, *, project, branch, git) -> None:  # noqa: ANN001
                return None

        monkeypatch.setattr(git_service, "resolve_forge", lambda project: _LegacyForge())
        project = ProjectConfig(path=tmp_path / "acme-api")
        mark = git_service.pr_ledger_mark()

        assert (
            git_service.create_pr(
                project=project, branch="hivepilot/x", git=GitActions(create_pr=True)
            )
            is None
        )
        assert git_service.pr_urls_since(mark, project="acme-api") == ()

    def test_pr_url_is_recorded_on_the_journal_when_the_forge_returns_one(
        self, live_config
    ) -> None:
        from hivepilot.services import git_service

        partition_id = _ratified(_plan(_task("a", pipeline="ship-it")), consent=True)

        def _open_a_pr(kwargs: dict) -> None:
            git_service.record_pr_opened(
                project="acme-api",
                branch="hivepilot/a",
                url="https://github.com/acme/api/pull/11",
            )

        partition_service.dispatch_partition(
            partition_id, orchestrator=FakeOrchestrator(on_call=_open_a_pr)
        )

        rows = partition_service.list_partition_tasks(partition_id)
        assert rows[0]["pr_url"] == "https://github.com/acme/api/pull/11"

    def test_pr_url_stays_null_when_the_forge_returned_none(self, live_config) -> None:
        partition_id = _ratified(_plan(_task("a", pipeline="ship-it")), consent=True)

        partition_service.dispatch_partition(partition_id, orchestrator=FakeOrchestrator())

        rows = partition_service.list_partition_tasks(partition_id)
        assert rows[0]["status"] == "committed"
        assert rows[0]["pr_url"] is None

    def test_an_ambiguous_capture_window_records_null_rather_than_a_guess(
        self, live_config
    ) -> None:
        """Two PRs for one project inside one task's window cannot be told
        apart from here -- so the journal records NOTHING, never a coin flip."""
        from hivepilot.services import git_service

        partition_id = _ratified(_plan(_task("a", pipeline="ship-it")), consent=True)

        def _open_two_prs(kwargs: dict) -> None:
            git_service.record_pr_opened(
                project="acme-api", branch="b1", url="https://github.com/acme/api/pull/1"
            )
            git_service.record_pr_opened(
                project="acme-api", branch="b2", url="https://github.com/acme/api/pull/2"
            )

        partition_service.dispatch_partition(
            partition_id, orchestrator=FakeOrchestrator(on_call=_open_two_prs)
        )

        assert partition_service.list_partition_tasks(partition_id)[0]["pr_url"] is None

    def test_no_pr_url_is_captured_without_outward_consent(self, live_config) -> None:
        from hivepilot.services import git_service

        partition_id = _ratified(_plan(_task("a")))

        def _open_a_pr(kwargs: dict) -> None:
            git_service.record_pr_opened(
                project="acme-api", branch="x", url="https://github.com/acme/api/pull/3"
            )

        partition_service.dispatch_partition(
            partition_id, orchestrator=FakeOrchestrator(on_call=_open_a_pr)
        )

        assert partition_service.list_partition_tasks(partition_id)[0]["pr_url"] is None


class TestExactPrUrlAttribution:
    """Exact per-task attribution (`hivepilot.pr_attribution`).

    v1 could only INFER which task opened which pull request, from a ledger
    time window, so two same-project tasks running at once both degraded to
    `NULL`. Every test here fails against that version: the window cannot
    tell two concurrent tasks apart, and it cannot tell a task's own PR from
    a stray one opened by an unrelated run inside the same window.
    """

    def test_two_concurrent_same_project_tasks_each_record_their_own_pr_url(
        self, live_config
    ) -> None:
        """The headline case. Both tasks take their ledger mark, then meet at
        a barrier before either opens a PR — so BOTH windows contain BOTH
        URLs, which is exactly the situation the window-only capture could
        not resolve (it recorded `—` for both). With a per-dispatch identity
        each task sees only its own entry."""
        from hivepilot.services import git_service

        partition_id = _ratified(
            _plan(_task("a", pipeline="ship-it"), _task("b", pipeline="ship-it")),
            consent=True,
        )
        both_marked = threading.Barrier(2)
        urls = {
            "do the a work": "https://github.com/acme/api/pull/101",
            "do the b work": "https://github.com/acme/api/pull/102",
        }

        def _open_my_own_pr(kwargs: dict) -> None:
            prompt = str(kwargs.get("extra_prompt"))
            # Both marks are already taken by the time anyone is released, so
            # every URL below lands inside every task's window.
            both_marked.wait(timeout=10)
            git_service.record_pr_opened(
                project="acme-api", branch=f"hivepilot/{prompt}", url=urls[prompt]
            )

        partition_service.dispatch_partition(
            partition_id, orchestrator=FakeOrchestrator(on_call=_open_my_own_pr)
        )

        recorded = {
            str(row["task_id"]): row["pr_url"]
            for row in partition_service.list_partition_tasks(partition_id)
        }
        assert recorded == {
            "a": "https://github.com/acme/api/pull/101",
            "b": "https://github.com/acme/api/pull/102",
        }

    def test_a_pr_opened_by_an_unrelated_run_is_never_claimed_by_a_task(self, live_config) -> None:
        """A scheduled/API/manual run opening a PR for the same project while
        a partition task happens to be running is NOT this task's PR. The
        window-only capture saw exactly one entry and claimed it — a wrong
        link, which is the one outcome the journal must never produce.

        The unrelated PR is ledgered from a plain thread, which carries no
        attribution scope — precisely how any run outside this dispatch
        reaches the ledger.
        """
        from hivepilot.services import git_service

        partition_id = _ratified(_plan(_task("a", pipeline="ship-it")), consent=True)

        def _someone_else_opens_a_pr(kwargs: dict) -> None:
            other = threading.Thread(
                target=lambda: git_service.record_pr_opened(
                    project="acme-api",
                    branch="release/nightly",
                    url="https://github.com/acme/api/pull/999",
                )
            )
            other.start()
            other.join(timeout=10)

        partition_service.dispatch_partition(
            partition_id, orchestrator=FakeOrchestrator(on_call=_someone_else_opens_a_pr)
        )

        rows = partition_service.list_partition_tasks(partition_id)
        assert rows[0]["status"] == "committed"
        assert rows[0]["pr_url"] is None

    def test_a_missing_attribution_records_null_rather_than_a_stray_entry(
        self, live_config
    ) -> None:
        """Fail-closed at the reader. If the identity never reached the
        ledger, there is no fallback to "whatever was opened around now" —
        that fallback IS the bug."""
        from hivepilot.services import git_service

        mark = git_service.pr_ledger_mark()
        git_service.record_pr_opened(
            project="acme-api", branch="x", url="https://github.com/acme/api/pull/7"
        )

        assert partition_service._capture_pr_url(mark, "acme-api", None) is None
        assert partition_service._capture_pr_url(mark, "acme-api", "   ") is None

    def test_the_attribution_key_is_unique_per_run_not_per_task(self) -> None:
        """`run_id` is what disambiguates: a retry of the same task in the
        same partition creates a NEW run, and must not inherit the previous
        attempt's pull request."""
        first = partition_service._pr_attribution_key("p1", "a", 11)
        retry = partition_service._pr_attribution_key("p1", "a", 12)
        sibling = partition_service._pr_attribution_key("p1", "b", 13)

        assert first != retry != sibling
        assert len({first, retry, sibling}) == 3

    def test_a_forge_that_reports_no_url_ledgers_nothing_even_when_attributed(
        self, tmp_path, monkeypatch
    ) -> None:
        """Attribution labels links; it can never invent one. A forge that
        cannot cheaply produce a URL still yields `None` and an empty
        ledger — the journal shows `—`."""
        from hivepilot import pr_attribution
        from hivepilot.models import GitActions, ProjectConfig
        from hivepilot.services import git_service

        class _SilentForge:
            def open_pr(self, *, project, branch, git) -> None:  # noqa: ANN001
                return None

        monkeypatch.setattr(git_service, "resolve_forge", lambda project: _SilentForge())
        project = ProjectConfig(path=tmp_path / "acme-api")
        key = "partition:p1:task:a:run:1"
        mark = git_service.pr_ledger_mark()

        with pr_attribution.scope(key):
            assert (
                git_service.create_pr(
                    project=project, branch="hivepilot/x", git=GitActions(create_pr=True)
                )
                is None
            )

        assert git_service.pr_urls_since(mark, project="acme-api", attribution=key) == ()

    def test_create_pr_ledgers_under_the_ambient_attribution_key(
        self, tmp_path, monkeypatch
    ) -> None:
        from hivepilot import pr_attribution
        from hivepilot.models import GitActions, ProjectConfig
        from hivepilot.services import git_service

        class _Forge:
            def open_pr(self, *, project, branch, git):  # noqa: ANN001, ANN202
                return "https://github.com/acme/api/pull/12"

        monkeypatch.setattr(git_service, "resolve_forge", lambda project: _Forge())
        project = ProjectConfig(path=tmp_path / "acme-api")
        mark = git_service.pr_ledger_mark()

        with pr_attribution.scope("partition:p1:task:a:run:1"):
            git_service.create_pr(
                project=project, branch="hivepilot/x", git=GitActions(create_pr=True)
            )

        assert git_service.pr_urls_since(
            mark, project="acme-api", attribution="partition:p1:task:a:run:1"
        ) == ("https://github.com/acme/api/pull/12",)
        # A DIFFERENT key matches nothing — the filter is identity, not a hint.
        assert git_service.pr_urls_since(mark, project="acme-api", attribution="other") == ()

    def test_a_non_partition_run_ledgers_unattributed_and_is_never_claimed(
        self, tmp_path, monkeypatch
    ) -> None:
        """The regression guard for every ordinary run: no scope is ever
        opened outside `partition_service`, so the entry is unattributed —
        recorded and readable exactly as before, and invisible to any
        attributed query."""
        from hivepilot.models import GitActions, ProjectConfig
        from hivepilot.services import git_service

        class _Forge:
            def open_pr(self, *, project, branch, git):  # noqa: ANN001, ANN202
                return "https://github.com/acme/api/pull/13"

        monkeypatch.setattr(git_service, "resolve_forge", lambda project: _Forge())
        project = ProjectConfig(path=tmp_path / "acme-api")
        mark = git_service.pr_ledger_mark()

        url = git_service.create_pr(
            project=project, branch="hivepilot/x", git=GitActions(create_pr=True)
        )

        assert url == "https://github.com/acme/api/pull/13"
        # Unchanged for the unfiltered read every pre-existing caller does...
        assert git_service.pr_urls_since(mark, project="acme-api") == (
            "https://github.com/acme/api/pull/13",
        )
        # ...and unreachable from any partition task's attributed read.
        assert git_service.pr_urls_since(mark, project="acme-api", attribution="anything") == ()


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


class TestCancel:
    def test_cancelling_a_proposed_partition_vetoes_it(self, live_config) -> None:
        partition_id = partition_service.create_partition(plan_json=json.dumps(_plan(_task("a"))))

        assert partition_service.cancel_partition(partition_id, actor="operator") == ()
        assert partition_service.get_partition(partition_id)["status"] == "vetoed"

    def test_cancelling_a_ratified_partition_cancels_its_pending_tasks(self, live_config) -> None:
        partition_id = _ratified(_plan(_task("a"), _task("b")))

        cancelled = partition_service.cancel_partition(partition_id, actor="operator")

        assert sorted(cancelled) == ["a", "b"]
        assert set(_statuses(partition_id).values()) == {"cancelled"}

    def test_cancel_never_rewrites_a_terminal_task(self, live_config) -> None:
        partition_id = _ratified(_plan(_task("a")))
        partition_service.dispatch_partition(partition_id, orchestrator=FakeOrchestrator())

        assert partition_service.cancel_partition(partition_id, actor="operator") == ()
        assert _statuses(partition_id) == {"a": "committed"}

    def test_cancelling_an_unknown_partition_raises_not_found(self, live_config) -> None:
        with pytest.raises(partition_service.PartitionNotFoundError):
            partition_service.cancel_partition("nope", actor="operator")


# ---------------------------------------------------------------------------
# CLI -- `hivepilot partition submit|show|ratify|status|cancel`
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_runner():
    from typer.testing import CliRunner

    return CliRunner()


def _write_plan(tmp_path: Path, plan: dict[str, Any]) -> Path:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return path


class TestCli:
    def test_submit_creates_a_proposed_partition(self, live_config, cli_runner, tmp_path) -> None:
        from hivepilot.cli import app

        plan_file = _write_plan(tmp_path, _plan(_task("a")))
        result = cli_runner.invoke(app, ["partition", "submit", "--file", str(plan_file)])

        assert result.exit_code == 0, result.output
        assert "status: proposed" in result.output
        rows = partition_service.list_partitions()
        assert len(rows) == 1 and rows[0]["status"] == "proposed"

    def test_submit_rejects_a_malformed_plan(self, live_config, cli_runner, tmp_path) -> None:
        from hivepilot.cli import app

        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        result = cli_runner.invoke(app, ["partition", "submit", "--file", str(bad)])

        assert result.exit_code == 1
        assert "Invalid partition document" in result.output
        assert partition_service.list_partitions() == []

    def test_submit_refuses_an_unknown_source_kind(self, live_config, cli_runner, tmp_path) -> None:
        from hivepilot.cli import app

        plan_file = _write_plan(tmp_path, _plan(_task("a")))
        result = cli_runner.invoke(
            app, ["partition", "submit", "--file", str(plan_file), "--source", "nope:whatever"]
        )

        assert result.exit_code == 1
        assert "Unknown partition source" in result.output
        assert partition_service.list_partitions() == []

    def test_submit_refuses_source_digest_drift(self, live_config, cli_runner, tmp_path) -> None:
        from hivepilot.cli import app

        source_file = tmp_path / "bug.md"
        source_file.write_text("the actual bug report", encoding="utf-8")
        plan = _plan(_task("a"))
        plan["source"] = {"kind": "text", "ref": str(source_file), "digest": "sha256:stale"}
        plan_file = _write_plan(tmp_path, plan)

        result = cli_runner.invoke(
            app,
            ["partition", "submit", "--file", str(plan_file), "--source", f"text:{source_file}"],
        )

        assert result.exit_code == 1
        assert "Source digest drift" in result.output
        assert partition_service.list_partitions() == []

    def test_show_reports_status_outward_and_effective_parallelism(
        self, live_config, cli_runner, monkeypatch
    ) -> None:
        from hivepilot.cli import app
        from hivepilot.config import settings

        monkeypatch.setattr(settings, "claude_max_concurrency", 1, raising=False)
        partition_id = partition_service.create_partition(
            plan_json=json.dumps(_plan(_task("a", pipeline="ship-it"), _task("b")))
        )

        result = cli_runner.invoke(app, ["partition", "show", partition_id])

        assert result.exit_code == 0, result.output
        assert "Status:     proposed" in result.output
        assert "forge_pr" in result.output
        assert "effective=1" in result.output

    def test_show_renders_ratified_at_local_time_with_marker(
        self, live_config, cli_runner, monkeypatch
    ) -> None:
        """fix/linear-sync-display-time sweep: `Ratified: ... at
        {ratified_at}` was found echoing the raw stored value verbatim --
        same bug class as the pre-fix `schedule health` table.
        `ratified_at` is written via `CURRENT_TIMESTAMP` (SQLite-engine
        clock, not mockable from Python), so this pins a known value
        directly on the row after a real ratify, matching how the rest of
        the dispatch journal is exercised in this file."""
        from hivepilot.cli import app
        from hivepilot.config import settings
        from hivepilot.services import db

        monkeypatch.setattr(settings, "display_timezone", "Europe/Paris", raising=False)
        partition_id = partition_service.create_partition(plan_json=json.dumps(_plan(_task("a"))))
        orch = FakeOrchestrator()
        monkeypatch.setattr("hivepilot.cli.Orchestrator", lambda *a, **k: orch)
        cli_runner.invoke(app, ["partition", "ratify", partition_id, "--approver", "operator"])

        with db.connect() as conn:
            conn.execute(
                db.ph("UPDATE partitions SET ratified_at=? WHERE id=?"),
                ("2026-07-27 09:08:32", partition_id),
            )

        result = cli_runner.invoke(app, ["partition", "show", partition_id])

        assert result.exit_code == 0, result.output
        assert "09:08" not in result.output
        assert "11:08" in result.output
        assert "CEST" in result.output

    def test_ratify_dispatches_by_default(self, live_config, cli_runner, monkeypatch) -> None:
        from hivepilot.cli import app

        partition_id = partition_service.create_partition(plan_json=json.dumps(_plan(_task("a"))))
        orch = FakeOrchestrator()
        monkeypatch.setattr("hivepilot.cli.Orchestrator", lambda *a, **k: orch)

        result = cli_runner.invoke(
            app, ["partition", "ratify", partition_id, "--approver", "operator"]
        )

        assert result.exit_code == 0, result.output
        assert "Ratified partition" in result.output
        assert len(orch.calls) == 1
        assert _statuses(partition_id) == {"a": "committed"}

    def test_ratify_no_dispatch_ratifies_without_running_anything(
        self, live_config, cli_runner, monkeypatch
    ) -> None:
        from hivepilot.cli import app

        partition_id = partition_service.create_partition(plan_json=json.dumps(_plan(_task("a"))))
        orch = FakeOrchestrator()
        monkeypatch.setattr("hivepilot.cli.Orchestrator", lambda *a, **k: orch)

        result = cli_runner.invoke(
            app,
            ["partition", "ratify", partition_id, "--approver", "operator", "--no-dispatch"],
        )

        assert result.exit_code == 0, result.output
        assert orch.calls == []
        assert _statuses(partition_id) == {"a": "pending"}

    def test_ratify_without_consent_refuses_an_outward_pipeline(
        self, live_config, cli_runner
    ) -> None:
        from hivepilot.cli import app

        partition_id = partition_service.create_partition(
            plan_json=json.dumps(_plan(_task("a", pipeline="ship-it")))
        )

        result = cli_runner.invoke(
            app, ["partition", "ratify", partition_id, "--approver", "operator"]
        )

        assert result.exit_code == 1
        assert "consent_required" in result.output
        assert partition_service.get_partition(partition_id)["status"] == "proposed"

    def test_ratify_refuses_a_stale_expected_digest(self, live_config, cli_runner) -> None:
        from hivepilot.cli import app

        partition_id = partition_service.create_partition(plan_json=json.dumps(_plan(_task("a"))))

        result = cli_runner.invoke(
            app,
            [
                "partition",
                "ratify",
                partition_id,
                "--approver",
                "operator",
                "--expected-digest",
                "sha256:stale",
            ],
        )

        assert result.exit_code == 1
        assert "digest_mismatch" in result.output

    def test_status_shows_an_em_dash_for_a_null_pr_url(self, live_config, cli_runner) -> None:
        from hivepilot.cli import app

        partition_id = _ratified(_plan(_task("a")))
        partition_service.dispatch_partition(partition_id, orchestrator=FakeOrchestrator())

        result = cli_runner.invoke(app, ["partition", "status", partition_id])

        assert result.exit_code == 0, result.output
        assert "committed" in result.output
        assert "—" in result.output

    def test_status_on_an_unknown_partition_exits_nonzero(self, live_config, cli_runner) -> None:
        from hivepilot.cli import app

        result = cli_runner.invoke(app, ["partition", "status", "nope"])
        assert result.exit_code == 1
        assert "Unknown partition" in result.output

    def test_cancel_vetoes_a_proposed_partition(self, live_config, cli_runner) -> None:
        from hivepilot.cli import app

        partition_id = partition_service.create_partition(plan_json=json.dumps(_plan(_task("a"))))

        result = cli_runner.invoke(
            app, ["partition", "cancel", partition_id, "--actor", "operator"]
        )

        assert result.exit_code == 0, result.output
        assert partition_service.get_partition(partition_id)["status"] == "vetoed"


# ---------------------------------------------------------------------------
# API -- GET /v1/partitions, GET /v1/partitions/{id},
#        POST /v1/partitions/{id}/ratify, POST /v1/partitions/{id}/cancel
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_tokens_file(tmp_path, monkeypatch):
    tokens_file = tmp_path / "tokens.yaml"
    tokens_file.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "tokens_file", tokens_file)
    return tokens_file


@pytest.fixture()
def api_client():
    from fastapi.testclient import TestClient

    from hivepilot.services.api_service import app as api_app

    return TestClient(api_app, raise_server_exceptions=True)


def _auth(raw_token: str) -> dict:
    return {"Authorization": f"Bearer {raw_token}"}


@pytest.fixture()
def no_background_dispatch(monkeypatch):
    """The API's ratify endpoint kicks dispatch onto a coordinator thread.
    Capture the call instead of starting a thread, so these tests assert the
    HTTP contract without racing a real dispatcher."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        partition_service,
        "dispatch_partition_background",
        lambda partition_id, **kwargs: calls.append({"partition_id": partition_id, **kwargs}),
    )
    return calls


class TestApi:
    def test_list_requires_a_run_token(self, live_config, tmp_tokens_file, api_client) -> None:
        from hivepilot.services.token_service import add_token

        raw, _ = add_token("read")
        assert api_client.get("/v1/partitions", headers=_auth(raw)).status_code == 403

        raw_run, _ = add_token("run")
        assert api_client.get("/v1/partitions", headers=_auth(raw_run)).status_code == 200

    def test_list_is_tenant_scoped(self, live_config, tmp_tokens_file, api_client) -> None:
        from hivepilot.services.token_service import add_token

        partition_service.create_partition(plan_json=json.dumps(_plan(_task("a"))), tenant="acme")
        partition_service.create_partition(
            plan_json=json.dumps(_plan(_task("b"))), tenant="other-co"
        )

        raw, _ = add_token("run", tenant="acme")
        body = api_client.get("/v1/partitions", headers=_auth(raw)).json()

        assert len(body) == 1
        assert body[0]["tenant"] == "acme"

    def test_get_reports_waves_outward_and_parallelism(
        self, live_config, tmp_tokens_file, api_client, monkeypatch
    ) -> None:
        from hivepilot.config import settings
        from hivepilot.services.token_service import add_token

        monkeypatch.setattr(settings, "claude_max_concurrency", 1, raising=False)
        partition_id = partition_service.create_partition(
            plan_json=json.dumps(
                _plan(_task("a", pipeline="ship-it"), _task("b", depends_on=["a"]))
            )
        )
        raw, _ = add_token("run")

        body = api_client.get(f"/v1/partitions/{partition_id}", headers=_auth(raw)).json()

        assert body["waves"] == [["a"], ["b"]]
        assert "forge_pr" in body["outward_actions"]
        assert body["parallelism"]["requested"] == 3
        assert body["parallelism"]["effective"] == 1
        assert body["parallelism"]["notes"]

    def test_get_reports_a_cross_tenant_partition_as_404(
        self, live_config, tmp_tokens_file, api_client
    ) -> None:
        """A GET must never let a caller distinguish "wrong tenant" from
        "doesn't exist".

        The in-tenant 200 is asserted in the SAME test on purpose: without it,
        "404 for a cross-tenant id" would also pass against a build where the
        endpoint simply does not exist -- a false green.
        """
        from hivepilot.services.token_service import add_token

        theirs = partition_service.create_partition(
            plan_json=json.dumps(_plan(_task("a"))), tenant="other-co"
        )
        mine = partition_service.create_partition(
            plan_json=json.dumps(_plan(_task("b"))), tenant="acme"
        )
        raw, _ = add_token("run", tenant="acme")

        ok = api_client.get(f"/v1/partitions/{mine}", headers=_auth(raw))
        assert ok.status_code == 200
        assert ok.json()["id"] == mine

        assert api_client.get(f"/v1/partitions/{theirs}", headers=_auth(raw)).status_code == 404

    def test_ratify_requires_the_approve_rank(
        self, live_config, tmp_tokens_file, api_client, no_background_dispatch
    ) -> None:
        from hivepilot.services.token_service import add_token

        plan_json = json.dumps(_plan(_task("a")))
        partition_id = partition_service.create_partition(plan_json=plan_json)
        raw, _ = add_token("run")

        response = api_client.post(
            f"/v1/partitions/{partition_id}/ratify",
            headers=_auth(raw),
            json={"partition_json": plan_json, "approver": "api"},
        )
        assert response.status_code == 403

    def test_cross_tenant_ratify_is_403(
        self, live_config, tmp_tokens_file, api_client, no_background_dispatch
    ) -> None:
        from hivepilot.services.token_service import add_token

        plan_json = json.dumps(_plan(_task("a")))
        partition_id = partition_service.create_partition(plan_json=plan_json, tenant="other-co")
        raw, _ = add_token("approve", tenant="acme")

        response = api_client.post(
            f"/v1/partitions/{partition_id}/ratify",
            headers=_auth(raw),
            json={"partition_json": plan_json, "approver": "api"},
        )

        assert response.status_code == 403
        assert "Cross-tenant" in response.json()["detail"]
        assert (
            partition_service.get_partition(partition_id, tenant="other-co")["status"] == "proposed"
        )

    def test_ratify_succeeds_and_kicks_off_dispatch(
        self, live_config, tmp_tokens_file, api_client, no_background_dispatch
    ) -> None:
        from hivepilot.services.token_service import add_token

        plan_json = json.dumps(_plan(_task("a")))
        partition_id = partition_service.create_partition(plan_json=plan_json)
        digest = partition_service.get_partition(partition_id)["proposed_digest"]
        raw, _ = add_token("approve")

        body = api_client.post(
            f"/v1/partitions/{partition_id}/ratify",
            headers=_auth(raw),
            json={
                "partition_json": plan_json,
                "approver": "api",
                "expected_digest": digest,
            },
        ).json()

        assert body["status"] == "ratified"
        assert body["task_ids"] == ["a"]
        assert body["dispatching"] is True
        assert body["parallelism"]["effective"] >= 1
        assert len(no_background_dispatch) == 1

    def test_ratify_translates_the_services_status_codes_verbatim(
        self, live_config, tmp_tokens_file, api_client, no_background_dispatch
    ) -> None:
        """The API layer never re-derives the mapping -- each refusal carries
        its own `status_code` next to the rule it belongs to."""
        from hivepilot.services.token_service import add_token

        raw, _ = add_token("approve")
        plan_json = json.dumps(_plan(_task("a")))
        digest = str(
            partition_service.get_partition(
                partition_service.create_partition(plan_json=plan_json)
            )["proposed_digest"]
        )

        # malformed -> 400
        malformed_id = partition_service.create_partition(plan_json=plan_json)
        assert (
            api_client.post(
                f"/v1/partitions/{malformed_id}/ratify",
                headers=_auth(raw),
                json={"partition_json": "{nope", "approver": "api", "expected_digest": digest},
            ).status_code
            == 400
        )

        # consent required -> 403
        outward_json = json.dumps(_plan(_task("a", pipeline="ship-it")))
        outward_id = partition_service.create_partition(plan_json=outward_json)
        outward_digest = partition_service.get_partition(outward_id)["proposed_digest"]
        assert (
            api_client.post(
                f"/v1/partitions/{outward_id}/ratify",
                headers=_auth(raw),
                json={
                    "partition_json": outward_json,
                    "approver": "api",
                    "expected_digest": outward_digest,
                },
            ).status_code
            == 403
        )

        # stale digest -> 409
        stale_id = partition_service.create_partition(plan_json=plan_json)
        assert (
            api_client.post(
                f"/v1/partitions/{stale_id}/ratify",
                headers=_auth(raw),
                json={
                    "partition_json": plan_json,
                    "approver": "api",
                    "expected_digest": "sha256:stale",
                },
            ).status_code
            == 409
        )

        # unknown partition -> 404
        assert (
            api_client.post(
                "/v1/partitions/does-not-exist/ratify",
                headers=_auth(raw),
                json={"partition_json": plan_json, "approver": "api"},
            ).status_code
            == 404
        )

    def test_ratify_with_dispatch_false_never_starts_anything(
        self, live_config, tmp_tokens_file, api_client, no_background_dispatch
    ) -> None:
        from hivepilot.services.token_service import add_token

        plan_json = json.dumps(_plan(_task("a")))
        partition_id = partition_service.create_partition(plan_json=plan_json)
        digest = partition_service.get_partition(partition_id)["proposed_digest"]
        raw, _ = add_token("approve")

        body = api_client.post(
            f"/v1/partitions/{partition_id}/ratify",
            headers=_auth(raw),
            json={
                "partition_json": plan_json,
                "approver": "api",
                "expected_digest": digest,
                "dispatch": False,
            },
        ).json()

        assert body["dispatching"] is False
        assert no_background_dispatch == []

    def test_cancel_is_gated_at_run_and_tenant_checked(
        self, live_config, tmp_tokens_file, api_client
    ) -> None:
        from hivepilot.services.token_service import add_token

        partition_id = partition_service.create_partition(
            plan_json=json.dumps(_plan(_task("a"))), tenant="other-co"
        )

        raw_read, _ = add_token("read")
        assert (
            api_client.post(
                f"/v1/partitions/{partition_id}/cancel", headers=_auth(raw_read)
            ).status_code
            == 403
        )

        raw_other, _ = add_token("run", tenant="acme")
        response = api_client.post(
            f"/v1/partitions/{partition_id}/cancel", headers=_auth(raw_other)
        )
        assert response.status_code == 403
        assert "Cross-tenant" in response.json()["detail"]

    def test_cancel_vetoes_a_proposed_partition(
        self, live_config, tmp_tokens_file, api_client
    ) -> None:
        from hivepilot.services.token_service import add_token

        partition_id = partition_service.create_partition(plan_json=json.dumps(_plan(_task("a"))))
        raw, _ = add_token("run")

        response = api_client.post(f"/v1/partitions/{partition_id}/cancel", headers=_auth(raw))

        assert response.status_code == 202
        assert partition_service.get_partition(partition_id)["status"] == "vetoed"


# ---------------------------------------------------------------------------
# The run rows the dispatcher creates
# ---------------------------------------------------------------------------


def test_each_dispatched_task_gets_its_own_run_row(live_config) -> None:
    partition_id = _ratified(_plan(_task("a"), _task("b")))
    partition_service.dispatch_partition(partition_id, orchestrator=FakeOrchestrator())

    run_ids = {row["run_id"] for row in partition_service.list_partition_tasks(partition_id)}
    assert len(run_ids) == 2
    for run_id in run_ids:
        assert state_service.get_run(int(run_id)) is not None
