"""Run-scoped PR-URL ATTRIBUTION key (propose -> ratify -> dispatch PRD,
spec section 8 — exact per-run attribution).

The problem
-----------

`git_service`'s PR ledger records every pull request a forge actually
reported, and the partition journal wants to show the operator WHICH PR a
given dispatched task opened. v1 of that feature could only INFER the answer
from a time window: take a ledger high-water mark before the run, read back
everything recorded for that project afterwards, and attribute the URL only
when *exactly one* entry matched. Two same-project tasks running at once both
saw two entries, so both honestly recorded `NULL`.

The missing ingredient was never the ledger — it was identity. This module
carries a per-dispatch identity from the dispatcher down to the exact instant
`git_service.record_pr_opened` writes an entry, so the reader can ask "which
URL did *this* task open" instead of "which URL was opened *around now*".

Why an ambient scope rather than a threaded parameter
-----------------------------------------------------

The call path from the dispatcher to the ledger write is
`partition_service._make_task_work` -> `Orchestrator.run_pipeline` ->
`_run_pipeline_body` -> `run_task` -> `_run_task_body` (ThreadPoolExecutor)
-> `_execute_task` -> `_execute_task_body` -> `git_service.
perform_git_actions` -> `create_pr` -> `record_pr_opened`: nine frames, seven
of which are the two hottest, most-called functions in the repo and care
nothing about partitions.

Threading a parameter through all nine would cover exactly the paths edited
on the day it was written. An ambient scope covers every path inside the
dispatcher's dynamic extent — including the resume/approved re-entries into
`run_task`, and any path added later — with one `with` statement at the
dispatcher and one `capture`/`adopt` pair at the single thread boundary that
already hand-propagates OTel context and the outward permission for exactly
the same reason.

This is deliberately NOT a third mechanism: it is the shape
`hivepilot.outward` already established (`scope` / `capture` / `adopt`),
hooked at the same `ThreadPoolExecutor.submit` boundary in
`hivepilot/orchestrator.py`. `outward.py`'s own docstring states the rule
this follows — *"contextvars do NOT cross a thread boundary"*, so the
boundary is covered by an explicit pair and by its own test rather than by
hope (see `tests/test_pr_attribution.py::TestThreadBoundary`).

Fail-closed rules
-----------------

1. **No key means no attribution.** `current()` outside any scope is `None`,
   and a `None` key never matches a ledger entry. A run that somehow loses
   the key records `NULL` — a gap, never a stray link.
2. **A blank key is not an identity.** `""` / `"   "` are treated as absent,
   so two unrelated blank-keyed entries can never be made to "match".
3. **Attribution never creates a URL.** This module only labels entries that
   a forge already reported. It cannot make the journal show a link that no
   forge produced — `create_pr` remains the sole source of URLs, and it
   still returns `None` when the forge gave it nothing.

Non-partition runs
------------------

Nothing outside `partition_service` ever opens a scope, so `current()` is
`None` for an ordinary `hivepilot run`, a scheduled run, an API run and a
swarm run. Their ledger entries carry `attribution=None` exactly as they
carried no attribution field at all before, and no non-partition code path
reads the ledger. The behaviour is unchanged by construction, not by
inspection.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from typing import Iterator

#: The attribution key in force for the current context, or `None`.
#: Private: `current()` is the only supported read.
_CURRENT: ContextVar[str | None] = ContextVar("hivepilot_pr_attribution", default=None)


def _normalise(key: str | None) -> str | None:
    """A usable key, or `None`.

    Blank and whitespace-only keys collapse to `None`: an empty value on an
    identity must mean "absent", never "matches other empty values". (This
    repo's most repeated bug class is an absent value read as "no
    constraint"; here absent means "no attribution".)
    """
    if key is None:
        return None
    stripped = key.strip()
    return stripped or None


def current() -> str | None:
    """The attribution key for this context, or `None` when unattributed."""
    return _CURRENT.get()


@contextlib.contextmanager
def scope(key: str | None) -> Iterator[str | None]:
    """Attribute everything in this block — on this thread — to *key*.

    `None` (and a blank string) INHERIT the enclosing scope rather than
    clearing it, so a nested `run_pipeline` inside a dispatched partition
    task cannot accidentally orphan the task's PRs.
    """
    effective = _normalise(key) or current()
    token = _CURRENT.set(effective)
    try:
        yield effective
    finally:
        _CURRENT.reset(token)


def capture() -> str | None:
    """The current key, as a value safe to hand to another thread.

    Pair with `adopt` at every thread boundary — contextvars are NOT
    inherited by `threading.Thread` or `ThreadPoolExecutor.submit`.
    """
    return current()


@contextlib.contextmanager
def adopt(key: str | None) -> Iterator[None]:
    """Re-establish a `capture()`d key inside a worker thread.

    Always resets on exit, so a POOLED worker thread can never carry one
    task's key into the next task that lands on it.
    """
    token = _CURRENT.set(_normalise(key))
    try:
        yield
    finally:
        _CURRENT.reset(token)
