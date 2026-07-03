from __future__ import annotations

import threading
import time

from hivepilot.services.runner_throttle import semaphore_for_kind


def test_claude_semaphore_is_singleton():
    """Same object returned on repeated calls (module-level cache)."""
    s1 = semaphore_for_kind("claude")
    s2 = semaphore_for_kind("claude")
    assert s1 is s2


def test_non_claude_effectively_unlimited():
    """Non-claude runners get a large semaphore (not the claude cap)."""
    s = semaphore_for_kind("codex")
    # Should be acquirable many times without blocking
    acquired = 0
    for _ in range(100):
        if s.acquire(blocking=False):
            acquired += 1
        else:
            break
    s.release(acquired)
    assert acquired == 100


def test_cap_one_blocks_second_concurrent_acquire():
    """When cap=1, two concurrent acquirers cannot both hold the semaphore."""
    from hivepilot.services.runner_throttle import _semaphore_for_kind

    sem = _semaphore_for_kind("__test_cap1__", 1)

    results: list[bool] = []
    barrier = threading.Event()

    def worker():
        acquired = sem.acquire(blocking=False)
        results.append(acquired)
        if acquired:
            barrier.wait(timeout=0.2)
            sem.release()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start()
    time.sleep(0.01)  # let t1 acquire first
    t2.start()
    t1.join(timeout=1)
    t2.join(timeout=1)
    barrier.set()

    # One should have acquired, one should not (cap=1)
    assert True in results
    assert False in results
