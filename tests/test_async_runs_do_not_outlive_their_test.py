"""Two "fixes" and the flake survived both. Here is the actual mechanism.

`test_failure_never_surfaces_raw_exception_text` fails roughly one full-suite
run in six and passes in isolation. I claimed it settled twice — in #448 on
the strength of one green suite, and in #451 having found a real defect
(`"pending"` counted as terminal) that turned out not to be the only one.

The mechanism is contamination between tests, and it is structural:

- `async_run_service` submits work to a **module-level
  `ThreadPoolExecutor(max_workers=8)` singleton**, shared for the whole test
  session. `submit_run` returns immediately and nothing ever joins the task.
- `conftest`'s `_isolate_state_db` gives every test a **fresh database**. So
  every test's first run is `id = 1`.

A task submitted by test A can therefore still be running during test B, and
when it calls `state_service.complete_run(1, ...)` the path is resolved at
call time — landing on **test B's** database, stamping `finished_at` and a
status on test B's run 1.

That explains every observation: the 1-in-6 frequency (it needs a straggler),
the load dependence (contention makes stragglers), the wrong `status`, and
why #451 did not help — the straggler *sets* `finished_at`, so the waiter
returns promptly with a poisoned row instead of waiting for the real one.

`_registry` is keyed by `run_id` too, so test B's submit silently overwrites
test A's cancellation Event — the same collision, one level up.

The fix is to stop letting a test's async work outlive it: `wait_until_idle`
plus an autouse fixture that drains after every test. Nothing about the
production path changes; the executor is deliberately fire-and-forget so
`POST /v1/runs` stays fast, and it still is.
"""

from __future__ import annotations

import threading
import time

from hivepilot.services import async_run_service


class TestWaitUntilIdle:
    def test_an_empty_registry_is_immediately_idle(self) -> None:
        assert async_run_service.wait_until_idle(0.5) is True

    def test_it_waits_for_an_in_flight_run(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def _work() -> None:
            started.set()
            release.wait(timeout=5)

        async_run_service.submit_run(9_001, _work)
        assert started.wait(timeout=5), "the work never started"

        assert async_run_service.wait_until_idle(0.2) is False, (
            "a run still in flight must not read as idle"
        )

        release.set()
        assert async_run_service.wait_until_idle(5.0) is True

    def test_it_returns_false_rather_than_hanging_forever(self) -> None:
        """A drain that blocks the whole suite on one stuck task would be
        worse than the flake it replaces."""
        release = threading.Event()
        started = threading.Event()

        def _work() -> None:
            started.set()
            release.wait(timeout=5)

        async_run_service.submit_run(9_002, _work)
        started.wait(timeout=5)

        begin = time.monotonic()
        idle = async_run_service.wait_until_idle(0.3)
        elapsed = time.monotonic() - begin

        release.set()
        async_run_service.wait_until_idle(5.0)

        assert idle is False
        assert elapsed < 2.0, "it must respect its own timeout"

    def test_a_failing_task_still_leaves_the_registry_clean(self) -> None:
        """`submit_run`'s `finally` pops the registry even when the work
        raises — otherwise one failing test would wedge the drain for every
        test after it."""

        def _boom() -> None:
            raise RuntimeError("boom")

        async_run_service.submit_run(9_003, _boom)

        assert async_run_service.wait_until_idle(5.0) is True
