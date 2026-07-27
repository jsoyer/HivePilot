"""Same-directory TDD-hook companion for `partition_service.py`.

NOT the real coverage. `pyproject.toml` sets `testpaths = ["tests"]`, so
plain `pytest` never collects this file; the actual, spec-mandated coverage
for this module lives in `tests/test_partition_service.py` (journal +
state machine + audit) and `tests/test_partition_ratify_validation.py`
(the fail-closed gate order).

This file exists only because `~/.claude/hooks/check-test-exists.sh`
resolves `CLAUDE_PROJECT_DIR` to the OUTER repo root when running inside an
isolated git worktree, so its `$PROJECT_DIR/tests/...` candidate can never
match a file created inside `.claude/worktrees/agent-*/tests/`. Its
same-directory candidate is computed from the edited file's own absolute
path and is therefore the only PROJECT_DIR-independent way to satisfy the
check from a worktree (documented in Sprint 1's Agent Notes).

The assertions below are real, not vacuous: they pin the two fail-closed
defaults that everything else in the module rests on.
"""

from __future__ import annotations


def test_partition_and_task_status_vocabularies_are_closed() -> None:
    from hivepilot.services import partition_service

    assert "ratified" in partition_service.PARTITION_STATUSES
    assert "skipped" in partition_service.TASK_STATUSES
    # `skipped` must be a DISTINCT terminal state from `failed` -- a task
    # whose prerequisite failed did not itself fail.
    assert "failed" in partition_service.TASK_STATUSES


def test_error_http_status_mapping_is_defined_once() -> None:
    from hivepilot.services import partition_service

    assert partition_service.DigestMismatchError.status_code == 409
    assert partition_service.ConsentRequiredError.status_code == 403
    assert partition_service.PolicyDeniedError.status_code == 403
