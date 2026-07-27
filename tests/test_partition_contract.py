"""Tests for `hivepilot.partition` — the partition contract (propose ->
ratify -> dispatch PRD, Sprint 1, spec section 3).

Every rejection path must raise one of the module's own *named* exceptions
(`PartitionError` subclasses), never a bare `pydantic.ValidationError` blob
-- see `hivepilot/partition.py`'s module docstring for why (pydantic only
intercepts ValueError/TypeError/AssertionError raised inside validators;
these exceptions deliberately do not subclass those, so they propagate
untouched).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from hivepilot.partition import (
    PARTITION_VERSION,
    BlankPromptError,
    DependencyCycleError,
    DuplicateTaskIdError,
    InvalidBudgetError,
    InvalidProjectTargetError,
    MalformedPartitionError,
    PartitionPlan,
    UnknownTaskDependencyError,
    load_partition,
)


def _valid_doc(**overrides):
    doc = {
        "partition_version": 1,
        "source": {"kind": "text", "ref": "docs/bug-1234.md", "digest": "sha256:abc"},
        "proposer": {
            "role": "partitioner",
            "pipeline": "propose-partition",
            "run_id": 4711,
            "generated_at": "2026-07-27T09:12:00Z",
        },
        "policy": {"max_parallel": 3, "on_task_failure": "continue"},
        "tasks": [
            {
                "id": "parse-guard",
                "title": "Guard the null deref in the parser",
                "project": "acme-api",
                "pipeline": "bugfix",
                "prompt": "Fix the null deref in parser.py",
                "depends_on": [],
                "budget": {"wall_clock_seconds": 1500, "cost_usd": 1.50},
                "done_when": ["repro test fails before, passes after"],
                "outward": True,
            }
        ],
    }
    doc.update(overrides)
    return doc


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_partition_parses() -> None:
    plan = load_partition(json.dumps(_valid_doc()))
    assert plan.partition_version == PARTITION_VERSION
    assert plan.tasks[0].id == "parse-guard"
    assert plan.tasks[0].budget.wall_clock_seconds == 1500
    assert plan.tasks[0].budget.cost_usd == 1.50


def test_project_accepts_plain_project() -> None:
    doc = _valid_doc()
    doc["tasks"][0]["project"] = "acme-api"
    plan = load_partition(json.dumps(doc))
    assert plan.tasks[0].project == "acme-api"


def test_project_accepts_project_slash_module() -> None:
    doc = _valid_doc()
    doc["tasks"][0]["project"] = "acme-api/billing"
    plan = load_partition(json.dumps(doc))
    assert plan.tasks[0].project == "acme-api/billing"


def test_empty_depends_on_is_parallel_eligible() -> None:
    doc = _valid_doc()
    doc["tasks"][0]["depends_on"] = []
    plan = load_partition(json.dumps(doc))
    assert plan.tasks[0].depends_on == []


def test_valid_dag_with_dependency_resolves() -> None:
    doc = _valid_doc()
    doc["tasks"].append(
        {
            "id": "downstream",
            "title": "Depends on parse-guard",
            "project": "acme-api",
            "pipeline": "bugfix",
            "prompt": "Build on the guard",
            "depends_on": ["parse-guard"],
            "budget": {"wall_clock_seconds": 600, "cost_usd": 0.5},
        }
    )
    plan = load_partition(json.dumps(doc))
    assert [t.id for t in plan.tasks] == ["parse-guard", "downstream"]


# ---------------------------------------------------------------------------
# Malformed JSON / schema
# ---------------------------------------------------------------------------


def test_malformed_json_syntax_raises_named_error() -> None:
    with pytest.raises(MalformedPartitionError):
        load_partition("{not valid json")


def test_top_level_non_object_raises_named_error() -> None:
    with pytest.raises(MalformedPartitionError):
        load_partition(json.dumps([1, 2, 3]))


def test_missing_required_top_level_key_raises_named_error() -> None:
    doc = _valid_doc()
    del doc["proposer"]
    with pytest.raises(MalformedPartitionError):
        load_partition(json.dumps(doc))


def test_unsupported_partition_version_raises_named_error() -> None:
    doc = _valid_doc(partition_version=2)
    with pytest.raises(MalformedPartitionError):
        load_partition(json.dumps(doc))


def test_no_tasks_raises_named_error() -> None:
    doc = _valid_doc(tasks=[])
    with pytest.raises(MalformedPartitionError):
        load_partition(json.dumps(doc))


def test_task_not_an_object_raises_named_error() -> None:
    doc = _valid_doc(tasks=["not-a-task"])
    with pytest.raises(MalformedPartitionError):
        load_partition(json.dumps(doc))


def test_blank_source_kind_raises_named_error() -> None:
    doc = _valid_doc()
    doc["source"]["kind"] = "   "
    with pytest.raises(MalformedPartitionError):
        load_partition(json.dumps(doc))


# ---------------------------------------------------------------------------
# DAG: duplicate ids / unknown deps / cycles
# ---------------------------------------------------------------------------


def test_duplicate_task_id_raises_named_error() -> None:
    doc = _valid_doc()
    dup = dict(doc["tasks"][0])
    doc["tasks"].append(dup)
    with pytest.raises(DuplicateTaskIdError):
        load_partition(json.dumps(doc))


def test_unknown_dependency_raises_named_error() -> None:
    doc = _valid_doc()
    doc["tasks"][0]["depends_on"] = ["does-not-exist"]
    with pytest.raises(UnknownTaskDependencyError):
        load_partition(json.dumps(doc))


def test_two_node_cycle_raises_named_error() -> None:
    doc = _valid_doc()
    doc["tasks"][0]["depends_on"] = ["b"]
    doc["tasks"].append(
        {
            "id": "b",
            "title": "B",
            "project": "acme-api",
            "pipeline": "bugfix",
            "prompt": "b prompt",
            "depends_on": ["parse-guard"],
            "budget": {"wall_clock_seconds": 300, "cost_usd": 0.2},
        }
    )
    with pytest.raises(DependencyCycleError):
        load_partition(json.dumps(doc))


def test_self_loop_raises_named_error() -> None:
    doc = _valid_doc()
    doc["tasks"][0]["depends_on"] = ["parse-guard"]
    with pytest.raises(DependencyCycleError):
        load_partition(json.dumps(doc))


# ---------------------------------------------------------------------------
# Blank prompts
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
def test_blank_prompt_raises_named_error(blank: str) -> None:
    doc = _valid_doc()
    doc["tasks"][0]["prompt"] = blank
    with pytest.raises(BlankPromptError):
        load_partition(json.dumps(doc))


def test_missing_prompt_key_raises_named_error() -> None:
    doc = _valid_doc()
    del doc["tasks"][0]["prompt"]
    with pytest.raises((BlankPromptError, MalformedPartitionError)):
        load_partition(json.dumps(doc))


# ---------------------------------------------------------------------------
# Budgets: mandatory, positive; tokens rejected
# ---------------------------------------------------------------------------


def test_missing_budget_raises_named_error() -> None:
    doc = _valid_doc()
    del doc["tasks"][0]["budget"]
    with pytest.raises(InvalidBudgetError):
        load_partition(json.dumps(doc))


def test_missing_wall_clock_seconds_raises_named_error() -> None:
    doc = _valid_doc()
    del doc["tasks"][0]["budget"]["wall_clock_seconds"]
    with pytest.raises(InvalidBudgetError):
        load_partition(json.dumps(doc))


def test_zero_wall_clock_seconds_raises_named_error() -> None:
    doc = _valid_doc()
    doc["tasks"][0]["budget"]["wall_clock_seconds"] = 0
    with pytest.raises(InvalidBudgetError):
        load_partition(json.dumps(doc))


def test_negative_wall_clock_seconds_raises_named_error() -> None:
    doc = _valid_doc()
    doc["tasks"][0]["budget"]["wall_clock_seconds"] = -1
    with pytest.raises(InvalidBudgetError):
        load_partition(json.dumps(doc))


def test_missing_cost_usd_raises_named_error() -> None:
    doc = _valid_doc()
    del doc["tasks"][0]["budget"]["cost_usd"]
    with pytest.raises(InvalidBudgetError):
        load_partition(json.dumps(doc))


def test_zero_cost_usd_raises_named_error() -> None:
    doc = _valid_doc()
    doc["tasks"][0]["budget"]["cost_usd"] = 0
    with pytest.raises(InvalidBudgetError):
        load_partition(json.dumps(doc))


def test_negative_cost_usd_raises_named_error() -> None:
    doc = _valid_doc()
    doc["tasks"][0]["budget"]["cost_usd"] = -0.01
    with pytest.raises(InvalidBudgetError):
        load_partition(json.dumps(doc))


def test_tokens_field_in_budget_is_rejected() -> None:
    """Tokens are deliberately NOT a supported budget unit (unenforceable
    across ansible/kubectl/helm/shell/container runners) -- a `tokens` key
    in a budget object must be rejected, not silently ignored."""
    doc = _valid_doc()
    doc["tasks"][0]["budget"]["tokens"] = 100_000
    with pytest.raises(InvalidBudgetError):
        load_partition(json.dumps(doc))


def test_task_budget_model_has_no_tokens_field() -> None:
    from hivepilot.partition import TaskBudget

    assert "tokens" not in TaskBudget.model_fields


# ---------------------------------------------------------------------------
# project target shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_target", ["", "   ", "has space", "acme//module", "acme/mod/extra", "/leading"]
)
def test_malformed_project_target_raises_named_error(bad_target: str) -> None:
    doc = _valid_doc()
    doc["tasks"][0]["project"] = bad_target
    with pytest.raises((InvalidProjectTargetError, MalformedPartitionError)):
        load_partition(json.dumps(doc))


# ---------------------------------------------------------------------------
# PartitionPlan.model_validate accepts a dict directly (not just via
# load_partition's JSON-string entry point) -- used by callers who already
# hold a parsed dict (e.g. Sprint 2's ratification gate re-validating an
# operator-edited plan).
# ---------------------------------------------------------------------------


def test_partition_plan_model_validate_accepts_dict() -> None:
    plan = PartitionPlan.model_validate(_valid_doc())
    assert plan.tasks[0].id == "parse-guard"


def test_partition_plan_model_validate_raises_named_error_on_cycle() -> None:
    doc = _valid_doc()
    doc["tasks"][0]["depends_on"] = ["parse-guard"]
    with pytest.raises(DependencyCycleError):
        PartitionPlan.model_validate(doc)


# ---------------------------------------------------------------------------
# Zero optional dependencies: `hivepilot.partition` must import cleanly with
# only stdlib + pydantic (a core dependency) -- no langchain/httpx/etc.
# ---------------------------------------------------------------------------

_ALLOWED_TOP_LEVEL_IMPORTS = {"__future__", "re", "json", "typing", "pydantic"}


def test_partition_module_imports_are_all_core_dependencies() -> None:
    import hivepilot.partition as partition_module

    source = Path(partition_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top_level_modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level_modules.add(node.module.split(".")[0])
    unexpected = top_level_modules - _ALLOWED_TOP_LEVEL_IMPORTS
    assert not unexpected, f"hivepilot.partition imports non-core modules: {unexpected}"


def test_partition_module_importable_standalone() -> None:
    """Mirrors the AC's `python -c "import hivepilot.partition"` check, in a
    genuinely isolated subprocess (not `importlib.reload` in-process, which
    would replace `hivepilot.partition`'s classes with new objects for the
    rest of this test session and break `isinstance` checks in sibling test
    modules that imported the original class)."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "-c", "import hivepilot.partition"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
