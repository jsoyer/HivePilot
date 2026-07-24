"""Minimal placeholder — the CANONICAL test suite for `memory_service.py`
lives in `tests/test_memory_service.py` (matches this project's convention,
see e.g. `tests/test_state_service.py` / `tests/test_analytics_service.py`).

This file exists ONLY as a same-directory TDD-hook workaround: in this
isolated worktree, `check-test-exists.sh`'s `CLAUDE_PROJECT_DIR` fallback
resolves to the wrong root for its `tests/`-level candidates (a known
harness quirk for freshly created worktree-only files — see MEMORY.md
"Worktree hooks PROJECT_DIR"), but same-directory candidates resolve
correctly since they're built from the edited file's own absolute path,
independent of that misresolved root. Kept intentionally trivial so it
never diverges from — or duplicates the effort of — the real suite.
"""

from __future__ import annotations

from hivepilot.services import memory_service


def test_memory_service_module_exposes_expected_api():
    assert hasattr(memory_service, "record_search")
    assert hasattr(memory_service, "record_read")
    assert hasattr(memory_service, "record_store")
    assert hasattr(memory_service, "record_evaluation")
    assert hasattr(memory_service, "reality_summary")
    assert hasattr(memory_service, "gaps_by_namespace")
    assert hasattr(memory_service, "recent_evaluations")
    assert hasattr(memory_service, "activity_journal")
