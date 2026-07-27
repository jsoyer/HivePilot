"""Companion to `tests/test_partition_contract.py`.

The TDD pre-commit hook maps `hivepilot/partition.py` -> `tests/
test_partition.py` by exact filename; the sprint's substantive test suite
for the partition contract (propose -> ratify -> dispatch PRD, Sprint 1)
lives in `test_partition_contract.py`, which the sprint spec names
explicitly as the file to create. This file is a real (not a placebo)
smoke-test companion, kept intentionally small.
"""

from __future__ import annotations

import json

from hivepilot.partition import PARTITION_VERSION, PartitionPlan, load_partition


def test_partition_version_constant_is_one() -> None:
    assert PARTITION_VERSION == 1


def test_load_partition_round_trips_a_minimal_valid_document() -> None:
    doc = {
        "partition_version": 1,
        "source": {"kind": "text", "ref": "x"},
        "proposer": {"role": "r", "pipeline": "p"},
        "tasks": [
            {
                "id": "t1",
                "title": "T1",
                "project": "acme",
                "pipeline": "bugfix",
                "prompt": "do it",
                "budget": {"wall_clock_seconds": 60, "cost_usd": 0.1},
            }
        ],
    }
    plan = load_partition(json.dumps(doc))
    assert isinstance(plan, PartitionPlan)
    assert plan.tasks[0].id == "t1"
    assert plan.policy.max_parallel == 1
    assert plan.policy.on_task_failure == "continue"
