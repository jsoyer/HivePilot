"""Unit tests for `hivepilot.services.async_run_service` (Mirador actionable
dashboard PRD, Sprint 3).

NOTE: this file is not in the sprint's declared `files_to_create` list, but
the TDD hook (`check-test-exists.sh`) blocks any Write/Edit to a NEW
production file unless a matching `tests/test_<module>.py` exists first --
`tests/test_async_runs_endpoint.py` doesn't match that filename convention
for `async_run_service.py`. Logged as an in-scope deviation in the sprint's
Agent Notes.
"""

from __future__ import annotations

import threading
import time


def test_import_is_side_effect_free():
    """Importing the module must not spin up a thread pool or start any
    background work."""
    before = threading.active_count()
    import hivepilot.services.async_run_service  # noqa: F401

    after = threading.active_count()
    assert after == before


def test_is_cancel_requested_false_for_unknown_run_id():
    from hivepilot.services import async_run_service

    assert async_run_service.is_cancel_requested(999_999) is False


def test_request_cancel_is_a_noop_for_unknown_run_id():
    """Must never raise for a run_id that was never submitted."""
    from hivepilot.services import async_run_service

    async_run_service.request_cancel(999_999)  # should not raise


def test_submit_run_executes_fn_on_a_background_thread():
    from hivepilot.services import async_run_service

    calls = []
    done = threading.Event()

    def _fn():
        calls.append(1)
        done.set()

    async_run_service.submit_run(1, _fn)
    assert done.wait(timeout=2.0)
    assert calls == [1]


def test_submit_run_registers_and_then_cleans_up_the_cancel_event():
    from hivepilot.services import async_run_service

    run_id = 42
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def _fn():
        started.set()
        release.wait(timeout=2.0)
        finished.set()

    async_run_service.submit_run(run_id, _fn)
    assert started.wait(timeout=2.0)
    # While in-flight, request_cancel/is_cancel_requested must work against
    # the registered Event.
    assert async_run_service.is_cancel_requested(run_id) is False
    async_run_service.request_cancel(run_id)
    assert async_run_service.is_cancel_requested(run_id) is True

    release.set()
    assert finished.wait(timeout=2.0)
    # Give the wrapper's `finally` cleanup a brief moment to run.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if async_run_service.is_cancel_requested(run_id) is False:
            break
        time.sleep(0.01)
    # After cleanup, the run_id is no longer registered -- both cancel
    # helpers must degrade gracefully (never raise) for it.
    assert async_run_service.is_cancel_requested(run_id) is False


def test_submit_run_swallows_fn_exceptions():
    """A raising `fn` must not propagate out of the worker thread / crash
    the process -- `submit_run` itself returns immediately regardless."""
    from hivepilot.services import async_run_service

    finished = threading.Event()

    def _boom():
        try:
            raise RuntimeError("boom")
        finally:
            finished.set()

    async_run_service.submit_run(7, _boom)
    assert finished.wait(timeout=2.0)
