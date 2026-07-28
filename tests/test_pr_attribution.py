"""Unit tests for `hivepilot.pr_attribution` — the run-scoped PR-URL
attribution key.

The property under test is narrow and mechanical: a key set on one thread is
visible to code running inside its dynamic extent, is NOT visible to a thread
that did not `adopt()` it, and never leaks past the scope. Everything else
(the ledger, the journal) is tested where it lives.
"""

from __future__ import annotations

import threading

from hivepilot import pr_attribution


class TestScope:
    def test_no_scope_means_no_key(self) -> None:
        assert pr_attribution.current() is None

    def test_a_scope_installs_its_key_for_the_dynamic_extent(self) -> None:
        with pr_attribution.scope("k1"):
            assert pr_attribution.current() == "k1"
        assert pr_attribution.current() is None

    def test_a_nested_scope_replaces_and_then_restores(self) -> None:
        with pr_attribution.scope("outer"):
            with pr_attribution.scope("inner"):
                assert pr_attribution.current() == "inner"
            assert pr_attribution.current() == "outer"

    def test_none_inherits_rather_than_clearing(self) -> None:
        """`scope(None)` is what every non-partition caller effectively does;
        it must never be able to drop an enclosing attribution."""
        with pr_attribution.scope("outer"):
            with pr_attribution.scope(None):
                assert pr_attribution.current() == "outer"

    def test_a_blank_key_is_treated_as_absent(self) -> None:
        """An empty/whitespace key is not an identity. It must inherit (never
        install a key that would match another blank-keyed entry)."""
        with pr_attribution.scope("outer"):
            with pr_attribution.scope("   "):
                assert pr_attribution.current() == "outer"
        with pr_attribution.scope(""):
            assert pr_attribution.current() is None

    def test_the_scope_is_restored_even_when_the_body_raises(self) -> None:
        try:
            with pr_attribution.scope("k1"):
                raise ValueError("boom")
        except ValueError:
            pass
        assert pr_attribution.current() is None


class TestThreadBoundary:
    def test_a_thread_that_does_not_adopt_sees_no_key(self) -> None:
        """The exact fact the whole capture/adopt pair exists for: contextvars
        are NOT inherited by a thread. Pinned so a future refactor that drops
        the `adopt` at the orchestrator's ThreadPoolExecutor boundary fails
        here rather than degrading the journal silently."""
        seen: list[str | None] = []

        with pr_attribution.scope("k1"):
            thread = threading.Thread(target=lambda: seen.append(pr_attribution.current()))
            thread.start()
            thread.join()

        assert seen == [None]

    def test_capture_and_adopt_carry_the_key_across_a_thread(self) -> None:
        seen: list[str | None] = []

        with pr_attribution.scope("k1"):
            captured = pr_attribution.capture()

        def _worker() -> None:
            with pr_attribution.adopt(captured):
                seen.append(pr_attribution.current())

        thread = threading.Thread(target=_worker)
        thread.start()
        thread.join()

        assert seen == ["k1"]

    def test_adopt_resets_so_a_pooled_thread_never_leaks_a_key(self) -> None:
        """A worker thread is reused. If `adopt` leaked, the NEXT task on that
        thread would inherit the previous task's key and mis-attribute its
        PR — the exact bug class this module exists to close."""
        seen: list[str | None] = []

        def _worker() -> None:
            with pr_attribution.adopt("k1"):
                pass
            seen.append(pr_attribution.current())

        thread = threading.Thread(target=_worker)
        thread.start()
        thread.join()

        assert seen == [None]

    def test_two_threads_hold_independent_keys(self) -> None:
        """Two concurrent partition tasks are two threads. Their keys must not
        see each other — that independence is what makes exact attribution
        possible where a shared time window failed."""
        barrier = threading.Barrier(2)
        seen: dict[str, str | None] = {}
        lock = threading.Lock()

        def _worker(key: str) -> None:
            with pr_attribution.adopt(key):
                barrier.wait(timeout=5)
                with lock:
                    seen[key] = pr_attribution.current()

        threads = [threading.Thread(target=_worker, args=(k,)) for k in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert seen == {"a": "a", "b": "b"}
