"""Tests for stop/cancel (Mirador actionable dashboard PRD, Sprint 4).

Covers three layers, from innermost to outermost:

- `hivepilot.services.async_run_service.request_cancel`/`is_cancel_requested`
  — bool-returning, lock-safe, never-raising (see module docstring there).
- `Orchestrator._execute_task_body`'s step loop — the cooperative cancel
  check at each step boundary. Mirrors `tests/test_step_approval_gate.py`'s
  real-`Orchestrator` + wired-registry pattern, so this exercises the ACTUAL
  production step loop, not a stand-in.
- `POST /v1/runs/{run_id}/cancel` — fail-closed auth/tenant matrix (mirrors
  `tests/test_async_runs_endpoint.py`'s `tmp_tokens_file`/`api_client`/`_auth`
  fixtures) plus a full async end-to-end flow (202 -> CANCELLED, no thread
  left running).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.models import ProjectConfig, RunnerDefinition, TaskConfig, TaskStep
from hivepilot.services.state_service import RunStatus
from hivepilot.services.token_service import add_token

# ---------------------------------------------------------------------------
# Shared fixtures/helpers — mirrors tests/test_async_runs_endpoint.py exactly.
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
    from hivepilot.services.api_service import app

    return TestClient(app, raise_server_exceptions=True)


def _auth(raw_token: str) -> dict:
    return {"Authorization": f"Bearer {raw_token}"}


def _wait_for_terminal(run_id: int, timeout: float = 5.0) -> dict:
    from hivepilot.services import state_service

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = [r for r in state_service.list_recent_runs(limit=200) if r["id"] == run_id]
        if rows and rows[0]["status"] not in ("running",):
            return rows[0]
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} never reached a terminal status")


# ---------------------------------------------------------------------------
# RunStatus.CANCELLED — vocabulary + terminal (finished_at set)
# ---------------------------------------------------------------------------


class TestRunStatusCancelled:
    def test_cancelled_member_exists_with_expected_value(self) -> None:
        assert RunStatus.CANCELLED.value == "cancelled"

    def test_from_str_parses_cancelled_case_insensitively(self) -> None:
        assert RunStatus.from_str("cancelled") is RunStatus.CANCELLED
        assert RunStatus.from_str("CANCELLED") is RunStatus.CANCELLED
        assert RunStatus.from_str("Cancelled") is RunStatus.CANCELLED

    def test_complete_run_sets_finished_at_for_cancelled(self) -> None:
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("p", "t")
        assert state_service.get_run(run_id)["finished_at"] is None

        state_service.complete_run(
            run_id, RunStatus.CANCELLED.value, detail="cancelled by operator"
        )

        row = state_service.get_run(run_id)
        assert row["status"] == "cancelled"
        assert row["finished_at"] is not None

    def test_get_run_returns_none_for_unknown_id(self) -> None:
        from hivepilot.services import state_service

        assert state_service.get_run(999_999_999) is None


# ---------------------------------------------------------------------------
# async_run_service.request_cancel — bool signature, fail-closed
# ---------------------------------------------------------------------------


class TestRequestCancelReturnValue:
    def test_returns_true_when_run_id_is_registered(self) -> None:
        from hivepilot.services import async_run_service

        run_id = 7_770_001
        with async_run_service._registry_lock:
            async_run_service._registry[run_id] = threading.Event()
        try:
            assert async_run_service.request_cancel(run_id) is True
            assert async_run_service.is_cancel_requested(run_id) is True
        finally:
            with async_run_service._registry_lock:
                async_run_service._registry.pop(run_id, None)

    def test_returns_false_for_unknown_run_id_never_raises(self) -> None:
        """Fail-closed: an unknown/never-submitted run_id must resolve to
        `False` -- never raise, never silently report success."""
        from hivepilot.services import async_run_service

        assert async_run_service.request_cancel(999_999_998) is False

    def test_returns_false_for_already_terminal_run_id(self) -> None:
        """A run popped from the registry (submit_run's `finally`, i.e.
        already terminal) must also resolve to `False` -- fail-closed, not
        "silently allow re-cancelling a finished run"."""
        from hivepilot.services import async_run_service

        run_id = 7_770_002
        with async_run_service._registry_lock:
            async_run_service._registry[run_id] = threading.Event()
            async_run_service._registry.pop(run_id, None)
        assert async_run_service.request_cancel(run_id) is False


# ---------------------------------------------------------------------------
# Orchestrator._execute_task_body's step loop — real Orchestrator, wired
# registry (mirrors tests/test_step_approval_gate.py's pattern exactly).
# ---------------------------------------------------------------------------


def _make_orch():
    from hivepilot.models import PipelinesFile
    from hivepilot.orchestrator import Orchestrator

    pipelines_file = PipelinesFile(pipelines={})
    with (
        patch("hivepilot.orchestrator.load_projects", return_value=MagicMock(projects={})),
        patch("hivepilot.orchestrator.load_tasks", return_value=MagicMock(tasks={}, runners={})),
        patch("hivepilot.orchestrator.load_pipelines", return_value=pipelines_file),
        patch("hivepilot.orchestrator.RunnerRegistry", return_value=MagicMock()),
        patch("hivepilot.orchestrator.PluginManager", return_value=MagicMock()),
    ):
        return Orchestrator()


def _two_step_task() -> TaskConfig:
    return TaskConfig(
        description="t",
        engine="native",
        steps=[
            TaskStep(name="prep", runner="prep-runner"),
            TaskStep(name="apply", runner="apply-runner"),
        ],
    )


def _wire_registry(orch) -> tuple[MagicMock, MagicMock]:
    defs = {
        "prep-runner": RunnerDefinition(kind="shell"),
        "apply-runner": RunnerDefinition(kind="shell"),
    }
    orch.registry = MagicMock()
    orch.registry._definition_for.side_effect = lambda name: defs[name]

    mock_prep = MagicMock()
    mock_prep.capture.return_value = "prep output"
    mock_apply = MagicMock()
    mock_apply.capture.return_value = "apply output"
    runners = {"prep-runner": mock_prep, "apply-runner": mock_apply}
    orch.registry.get_runner.side_effect = lambda name: runners[name]
    return mock_prep, mock_apply


class TestOrchestratorStepLoopCancellation:
    def test_cancel_between_steps_reaches_cancelled_second_step_never_runs(self) -> None:
        """The operator hits Stop WHILE step 1 ('prep') is running -- the
        cancel flag becomes set as a side effect of that step's own capture()
        call, simulating the real timing of an out-of-band `POST /v1/runs/
        {run_id}/cancel` racing a step that's already executing. The step
        loop must catch this at the NEXT boundary (before 'apply' runs), mark
        the run CANCELLED via `state_service.complete_run` exactly once, and
        raise `RunCancelled` -- 'apply' must NEVER execute."""
        from hivepilot.orchestrator import RunCancelled
        from hivepilot.services import async_run_service

        orch = _make_orch()
        mock_prep, mock_apply = _wire_registry(orch)
        task = _two_step_task()
        project = ProjectConfig(path=Path("/tmp/p"))

        run_id = 4_242_001
        with async_run_service._registry_lock:
            async_run_service._registry[run_id] = threading.Event()

        def _prep_capture(*_a, **_k):
            async_run_service.request_cancel(run_id)
            return "prep output"

        mock_prep.capture.side_effect = _prep_capture

        try:
            with (
                patch("hivepilot.orchestrator.state_service.record_step"),
                patch("hivepilot.orchestrator.state_service.complete_run") as mock_complete,
                patch.object(orch, "_resolve_secrets", return_value={}),
            ):
                with pytest.raises(RunCancelled):
                    orch._execute_task(
                        project=project,
                        task_name="x",
                        task=task,
                        extra_prompt=None,
                        auto_git=False,
                        run_id=run_id,
                    )

            mock_prep.capture.assert_called_once()
            mock_apply.capture.assert_not_called()
            mock_complete.assert_called_once_with(
                run_id, RunStatus.CANCELLED.value, detail="cancelled by operator"
            )
        finally:
            with async_run_service._registry_lock:
                async_run_service._registry.pop(run_id, None)

    def test_no_cancel_requested_both_steps_run_normally(self) -> None:
        """Baseline / no-regression: without a cancel request, both steps run
        exactly once and the task completes normally."""
        orch = _make_orch()
        mock_prep, mock_apply = _wire_registry(orch)
        task = _two_step_task()
        project = ProjectConfig(path=Path("/tmp/p"))

        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            result = orch._execute_task(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=4_242_002,
            )

        mock_prep.capture.assert_called_once()
        mock_apply.capture.assert_called_once()
        assert result == "prep output\napply output"

    def test_sync_caller_without_run_id_never_cancels(self) -> None:
        """Sync `run_task`/`_run_task_body` callers pass no `run_id` --
        `is_cancel_requested` must never even be consulted (there is no
        registry entry for `None`), so both steps always run regardless of
        any global cancellation state."""
        orch = _make_orch()
        mock_prep, mock_apply = _wire_registry(orch)
        task = _two_step_task()
        project = ProjectConfig(path=Path("/tmp/p"))

        with (
            patch("hivepilot.orchestrator.state_service.record_step"),
            patch("hivepilot.orchestrator.state_service.complete_run") as mock_complete,
            patch.object(orch, "_resolve_secrets", return_value={}),
        ):
            result = orch._execute_task(
                project=project,
                task_name="x",
                task=task,
                extra_prompt=None,
                auto_git=False,
                run_id=None,
            )

        mock_prep.capture.assert_called_once()
        mock_apply.capture.assert_called_once()
        mock_complete.assert_not_called()
        assert result == "prep output\napply output"


# ---------------------------------------------------------------------------
# POST /v1/runs/{run_id}/cancel — auth/tenant/fail-closed matrix
# ---------------------------------------------------------------------------


class TestCancelEndpointAuthAndFailClosed:
    def test_requires_auth(self, api_client):
        resp = api_client.post("/v1/runs/1/cancel")
        assert resp.status_code == 401

    def test_read_role_forbidden(self, api_client, tmp_tokens_file):
        """role < run -> 403, fail-closed (checked before any run lookup)."""
        raw, _ = add_token("read")
        resp = api_client.post("/v1/runs/1/cancel", headers=_auth(raw))
        assert resp.status_code == 403

    def test_unknown_run_id_404(self, api_client, tmp_tokens_file):
        raw, _ = add_token("run")
        resp = api_client.post("/v1/runs/999999/cancel", headers=_auth(raw))
        assert resp.status_code == 404

    def test_cross_tenant_cancel_forbidden(self, api_client, tmp_tokens_file):
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("proj", "task", tenant="tenant-a")
        raw, _ = add_token("run", tenant="tenant-b")
        resp = api_client.post(f"/v1/runs/{run_id}/cancel", headers=_auth(raw))
        assert resp.status_code == 403

    def test_adversarial_empty_registry_entry_is_409_not_fail_open(
        self, api_client, tmp_tokens_file
    ):
        """A real run row exists (correct tenant, caller has a `run`-rank
        token) but was NEVER registered in the in-flight async registry (a
        sync run, or an async run that already finished) -- an empty/absent
        registry entry must map to `409 not cancellable`, NEVER a
        false-success `202`."""
        from hivepilot.services import state_service

        run_id = state_service.record_run_start("proj", "task", tenant="default")
        raw, _ = add_token("run", tenant="default")
        resp = api_client.post(f"/v1/runs/{run_id}/cancel", headers=_auth(raw))
        assert resp.status_code == 409
        assert resp.json()["detail"] == "not cancellable"

    def test_same_tenant_run_role_can_cancel_a_registered_run(self, api_client, tmp_tokens_file):
        from hivepilot.services import async_run_service, state_service

        run_id = state_service.record_run_start("proj", "task", tenant="default")
        with async_run_service._registry_lock:
            async_run_service._registry[run_id] = threading.Event()
        try:
            raw, _ = add_token("run", tenant="default")
            resp = api_client.post(f"/v1/runs/{run_id}/cancel", headers=_auth(raw))
            assert resp.status_code == 202
            assert resp.json() == {"run_id": run_id, "status": "cancelling"}
            assert async_run_service.is_cancel_requested(run_id) is True
        finally:
            with async_run_service._registry_lock:
                async_run_service._registry.pop(run_id, None)

    def test_admin_bypasses_tenant_check(self, api_client, tmp_tokens_file):
        from hivepilot.services import async_run_service, state_service

        run_id = state_service.record_run_start("proj", "task", tenant="tenant-a")
        with async_run_service._registry_lock:
            async_run_service._registry[run_id] = threading.Event()
        try:
            raw, _ = add_token("admin", tenant="tenant-b")
            resp = api_client.post(f"/v1/runs/{run_id}/cancel", headers=_auth(raw))
            assert resp.status_code == 202
            assert resp.json() == {"run_id": run_id, "status": "cancelling"}
            assert async_run_service.is_cancel_requested(run_id) is True
        finally:
            with async_run_service._registry_lock:
                async_run_service._registry.pop(run_id, None)


# ---------------------------------------------------------------------------
# Full async flow: POST /v1/runs -> POST /v1/runs/{id}/cancel -> CANCELLED,
# with the real ThreadPoolExecutor/registry (async_run_service.submit_run),
# exactly like tests/test_async_runs_endpoint.py's create_run tests.
# ---------------------------------------------------------------------------


def _fake_project(name: str = "acme-web") -> ProjectConfig:
    return ProjectConfig(path=Path(name))


def _fake_task() -> TaskConfig:
    return TaskConfig(description="deploy things")


class TestCancelEndpointFullAsyncFlow:
    def test_cancel_reaches_terminal_cancelled_and_no_thread_left_running(
        self, api_client, tmp_tokens_file, monkeypatch
    ):
        from hivepilot.orchestrator import RunCancelled
        from hivepilot.services import api_service, async_run_service, policy_service, state_service

        project = _fake_project()
        task = _fake_task()

        step1_started = threading.Event()
        may_continue = threading.Event()

        def _execute_task(*, run_id=None, **_kwargs):
            step1_started.set()
            assert may_continue.wait(timeout=5.0), "test harness never signalled continue"
            if run_id is not None and async_run_service.is_cancel_requested(run_id):
                state_service.complete_run(
                    run_id, RunStatus.CANCELLED.value, detail="cancelled by operator"
                )
                raise RunCancelled(f"Run {run_id} cancelled by operator.")
            return "should not reach here"

        orch = SimpleNamespace(
            tasks=SimpleNamespace(tasks={"deploy": task}),
            _project=lambda name: project,
            _cve_gate_block_detail=lambda *a, **k: None,
            _execute_task=_execute_task,
        )
        monkeypatch.setattr(api_service, "_get_orchestrator", lambda: orch)
        monkeypatch.setattr(
            policy_service, "enforce_policy", lambda *a, **k: policy_service.Policy()
        )

        # Warm up the (lazily-constructed, process-local) executor with a
        # trivial job first and wait for it to finish -- a `ThreadPoolExecutor`
        # reuses an already-idle worker thread for the next submission rather
        # than spawning a new one (CPython's `_adjust_thread_count` only
        # starts a new thread when no idle one is available), so this makes
        # "no NET NEW thread survives the cancel flow" the meaningful,
        # non-flaky assertion below -- the pool's own worker thread is
        # expected to persist (that's what a thread pool is), only a *leaked*
        # extra one would indicate a bug.
        _warmup_done = threading.Event()
        async_run_service.submit_run(-1, _warmup_done.set)
        assert _warmup_done.wait(timeout=5.0)

        baseline_threads = threading.active_count()

        raw, _ = add_token("run")
        resp = api_client.post(
            "/v1/runs", json={"task": "deploy", "project": "acme-web"}, headers=_auth(raw)
        )
        assert resp.status_code == 202
        run_id = resp.json()["run_id"]

        assert step1_started.wait(timeout=5.0), "background worker never started"

        cancel_resp = api_client.post(f"/v1/runs/{run_id}/cancel", headers=_auth(raw))
        assert cancel_resp.status_code == 202
        assert cancel_resp.json() == {"run_id": run_id, "status": "cancelling"}

        may_continue.set()

        row = _wait_for_terminal(run_id)
        assert row["status"] == "cancelled"

        # No thread left running after cancel: the background worker thread
        # (and the registry entry it owned) must be fully cleaned up.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and threading.active_count() > baseline_threads:
            time.sleep(0.05)
        assert threading.active_count() <= baseline_threads
        assert async_run_service.is_cancel_requested(run_id) is False

        # Re-cancelling an already-terminal run must 409, never a
        # false-success 202 (the registry entry was popped on completion).
        re_cancel = api_client.post(f"/v1/runs/{run_id}/cancel", headers=_auth(raw))
        assert re_cancel.status_code == 409
