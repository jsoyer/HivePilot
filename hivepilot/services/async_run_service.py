"""Process-local background executor + in-flight registry for async runs
(Mirador actionable dashboard PRD, Sprint 3 -- `POST /v1/runs`).

`POST /v1/runs` (`hivepilot/services/api_service.py`) records a run row and
must return the caller a `run_id` immediately (202, <500ms); the actual
pipeline execution happens here, on a background thread, so the HTTP
response never blocks on it.

Owns a lazily-constructed `ThreadPoolExecutor` (mirrors `api_service.py`'s
`_get_orchestrator()` lazy-singleton pattern) and an in-flight registry of
`run_id -> threading.Event` cancellation flags.

`request_cancel`/`is_cancel_requested` are the API surface real cooperative
cancellation is wired through (Mirador actionable dashboard PRD, Sprint 4 --
`POST /v1/runs/{run_id}/cancel` + `Orchestrator._execute_task_body`'s step
loop) -- see the "Async Run Handle" invariant, verified by
`grep -q 'request_cancel' hivepilot/services/async_run_service.py`.

Side-effect-free at import time: no thread pool is constructed and no
thread is started merely by importing this module.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

_MAX_WORKERS = 8

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()

_registry: dict[int, threading.Event] = {}
_registry_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    """Lazy singleton, same double-checked-locking shape as
    `api_service._get_orchestrator()` -- constructed on first use, not at
    import time."""
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=_MAX_WORKERS, thread_name_prefix="hivepilot-async-run"
                )
    return _executor


def wait_until_idle(timeout: float = 5.0) -> bool:
    """Block until no run is in flight, or *timeout* elapses. True iff idle.

    `submit_run` is deliberately fire-and-forget so `POST /v1/runs` stays
    fast, and nothing joins the task. That is right in production and wrong
    in a test suite: the executor is a module-level singleton shared for the
    whole session, while `conftest`'s `_isolate_state_db` gives every test a
    FRESH database -- so every test's first run is `id = 1`.

    A task submitted by one test can therefore still be running during the
    next, and `state_service.complete_run(1, ...)` resolves its path at call
    time, landing on the *current* test's database and stamping a status and
    `finished_at` on a run it knows nothing about. That is what made
    `test_failure_never_surfaces_raw_exception_text` fail about one
    full-suite run in six while passing in isolation.

    Returns False rather than blocking forever: a drain that hangs the whole
    suite on one stuck task would be worse than the flake it replaces.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _registry_lock:
            if not _registry:
                return True
        time.sleep(0.01)
    with _registry_lock:
        return not _registry


def request_cancel(run_id: int) -> bool:
    """Signal cooperative cancellation for *run_id*, if it's currently
    in-flight (Mirador actionable dashboard PRD, Sprint 4 --
    `POST /v1/runs/{run_id}/cancel`).

    Returns `True` iff *run_id* was found in the in-flight registry and its
    `Event` was set -- the caller (the cancel endpoint) uses this as the
    single source of truth for "is this run actually cancellable right
    now", mapping `False` to a `409`, never a false-success response.

    Returns `False` for an unknown, never-submitted, or already-terminal
    (popped from the registry in `submit_run`'s `finally`) *run_id* -- never
    raises, lock-safe. An empty/absent registry entry is exactly the
    fail-closed case this must never silently treat as success.
    """
    with _registry_lock:
        event = _registry.get(run_id)
    if event is None:
        return False
    event.set()
    return True


def is_cancel_requested(run_id: int) -> bool:
    """`True` iff `request_cancel(run_id)` has been called for an in-flight
    run. `False` for an unknown/not-yet-submitted/already-completed
    run_id -- never raises."""
    with _registry_lock:
        event = _registry.get(run_id)
    return event.is_set() if event is not None else False


_bg_counter = 0


def submit_background(fn: Callable[[], None]) -> None:
    """Fire-and-forget a background task NOT tied to a run_id (e.g. a space's
    async agent reply, HP-46). Tracked in the same in-flight registry as
    `submit_run` under a synthetic NEGATIVE key (real run ids are always
    positive, so they never collide), so `wait_until_idle` drains it too —
    which is what keeps it from leaking across tests. Never raises; `fn`
    self-handles its own errors, this is only a last-resort guard."""
    global _bg_counter
    with _registry_lock:
        _bg_counter += 1
        key = -_bg_counter
        _registry[key] = threading.Event()

    def _wrapper() -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 -- last-resort guard; fn should self-handle
            from hivepilot.utils.logging import get_logger

            get_logger(__name__).error("async_run.background_failed")
        finally:
            with _registry_lock:
                _registry.pop(key, None)

    _get_executor().submit(_wrapper)


def submit_run(run_id: int, fn: Callable[[], None]) -> None:
    """Submit *fn* to run on a background thread, registering *run_id* in
    the in-flight cancellation registry for the duration.

    *fn* takes no arguments and returns nothing meaningful to this caller
    -- it owns its own success/failure recording (typically via
    `state_service.complete_run`). This function returns immediately;
    nothing here blocks on *fn* completing, which is the whole point of
    `POST /v1/runs` staying fast.

    Any exception *fn* raises is caught and logged (never re-raised, never
    crashes the process) -- by the time *fn* runs, `POST /v1/runs` has
    already returned its 202 response, so nothing is left to propagate the
    exception to.
    """
    with _registry_lock:
        _registry[run_id] = threading.Event()

    def _wrapper() -> None:
        try:
            fn()
        except Exception:  # noqa: BLE001 -- last-resort guard; fn should self-handle
            from hivepilot.utils.logging import get_logger

            get_logger(__name__).error("async_run.worker_failed", run_id=run_id)
        finally:
            with _registry_lock:
                _registry.pop(run_id, None)

    _get_executor().submit(_wrapper)
