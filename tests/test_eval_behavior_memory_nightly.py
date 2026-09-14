"""HP-113: nightly behavior-memory suite (opt-in, not default PR CI)."""

from __future__ import annotations

import os

import pytest

from hivepilot.eval_behavior_memory import (
    APPROVE_TWICE,
    BEHAVIOR_MEMORY_EVAL_ENV,
    PENDING_UNCHANGED,
    REJECT_UNCHANGED,
    check_suite,
)

pytestmark = [
    pytest.mark.behavior_memory,
    pytest.mark.skipif(
        os.environ.get(BEHAVIOR_MEMORY_EVAL_ENV) != "1",
        reason="behavior memory eval is opt-in (HIVEPILOT_BEHAVIOR_MEMORY_EVAL=1)",
    ),
]


def test_behavior_suite_meets_memory_contract() -> None:
    verdicts = check_suite()
    by_name = {item.scenario: item for item in verdicts}
    assert by_name[PENDING_UNCHANGED].effect_count == 0
    assert by_name[REJECT_UNCHANGED].effect_count == 0
    assert by_name[APPROVE_TWICE].effect_count == 1
    assert by_name[APPROVE_TWICE].cached_second is True
