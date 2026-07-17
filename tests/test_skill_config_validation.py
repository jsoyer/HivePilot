"""Tests for the `skills:` field on TaskStep/PipelineStage and its
fail-closed cross-reference + `min_role` validation in `validate_config`
(skill-plugin-type PRD, Sprint 3).

Mirrors the existing `tests/test_config_validation.py` fixture pattern
(`_write_config` writes a minimal-but-complete config directory) plus the
`tests/test_skills_registry.py` pattern for registering a real,
local-file-plugin-backed skill via `PluginManager` (monkeypatch
`plugins.settings.base_dir` to a tmp_path containing a `plugins/` dir).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml

from hivepilot.models import PipelineConfig, PipelineStage, TaskConfig, TaskStep
from hivepilot.services import config_validation

# Stub optional deps before importing anything that transitively imports
# hivepilot.plugins (mirrors tests/test_plugins.py / test_skills_registry.py).
_STUBS = [
    "langchain",
    "langchain.text_splitter",
    "langchain_community",
    "langchain_community.embeddings",
    "langchain_community.vectorstores",
]
for _mod in _STUBS:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


def _write_config(
    base_dir: Path,
    *,
    roles: list[dict],
    tasks: dict[str, dict],
    stages: list[dict],
) -> None:
    """Minimal-but-complete config directory, mirroring
    test_config_validation.py's `_write_config` helper."""
    (base_dir / "projects.yaml").write_text(yaml.dump({"projects": {}}))
    (base_dir / "roles.yaml").write_text(yaml.dump({"roles": roles}))
    (base_dir / "policies.yaml").write_text(yaml.dump({"policies": {}}))
    (base_dir / "groups.yaml").write_text(yaml.dump({"groups": {}}))
    (base_dir / "tasks.yaml").write_text(yaml.dump({"tasks": tasks}))
    (base_dir / "pipelines.yaml").write_text(yaml.dump({"pipelines": {"demo": {"stages": stages}}}))


def _register_skill_plugin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, name: str, min_role: str | None = None
) -> None:
    """Register a single skill via a real local-file plugin, monkeypatching
    `hivepilot.plugins.settings.base_dir` -- the same singleton
    `config_validation.py`'s `PluginManager()` construction will read."""
    from hivepilot import plugins as plugins_mod

    pdir = tmp_path / "plugins"
    pdir.mkdir(exist_ok=True)
    min_role_kv = f", 'min_role': {min_role!r}" if min_role else ""
    (pdir / f"{name}_skill.py").write_text(
        "def register():\n"
        f"    return {{'skills': [{{'name': {name!r}, 'description': 'D', "
        f"'provider': 'acme', 'files': {{'SKILL.md': 'hello'}}{min_role_kv}}}]}}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)


# ---------------------------------------------------------------------------
# Round-trip: YAML -> TaskStep / PipelineStage (order preserved, dupes removed)
# ---------------------------------------------------------------------------


def test_skills_round_trip_from_yaml_into_task_step_and_pipeline_stage() -> None:
    task_raw: dict[str, Any] = {
        "description": "d",
        "steps": [
            {
                "name": "s1",
                "runner": "claude",
                "skills": ["b", "a", "b", "c"],
            }
        ],
    }
    task = TaskConfig(**task_raw)
    assert task.steps[0].skills == ["b", "a", "c"]

    pipeline_raw: dict[str, Any] = {
        "description": "p",
        "stages": [{"name": "st", "task": "t1", "skills": ["y", "x", "y"]}],
    }
    pipeline = PipelineConfig(**pipeline_raw)
    assert pipeline.stages[0].skills == ["y", "x"]


# ---------------------------------------------------------------------------
# Unknown skill -> hard validation error (fail closed)
# ---------------------------------------------------------------------------


def test_unknown_skill_in_task_step_is_hard_error(tmp_path: Path, monkeypatch) -> None:
    roles = [{"name": "role_a"}]
    tasks = {
        "task-a": {
            "role": "role_a",
            "steps": [{"name": "s1", "runner": "claude", "skills": ["does-not-exist"]}],
        }
    }
    stages = [{"name": "Stage A", "task": "task-a"}]
    _write_config(tmp_path, roles=roles, tasks=tasks, stages=stages)

    # No plugin registered at all -- PluginManager().list_skills() is empty.
    from hivepilot import plugins as plugins_mod

    monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

    problems = config_validation.validate_config(base_dir=tmp_path)

    matching = [p for p in problems if "does-not-exist" in p and "unknown skill" in p]
    assert matching, f"Expected an unknown-skill problem, got: {problems}"


def test_unknown_skill_in_pipeline_stage_is_hard_error(tmp_path: Path, monkeypatch) -> None:
    roles = [{"name": "role_a"}]
    tasks = {"task-a": {"role": "role_a", "steps": []}}
    stages = [{"name": "Stage A", "task": "task-a", "skills": ["ghost-skill"]}]
    _write_config(tmp_path, roles=roles, tasks=tasks, stages=stages)

    from hivepilot import plugins as plugins_mod

    monkeypatch.setattr(plugins_mod.settings, "base_dir", tmp_path, raising=False)

    problems = config_validation.validate_config(base_dir=tmp_path)

    matching = [p for p in problems if "ghost-skill" in p and "unknown skill" in p]
    assert matching, f"Expected an unknown-skill problem, got: {problems}"


def test_known_skill_with_no_min_role_produces_no_error(tmp_path: Path, monkeypatch) -> None:
    roles = [{"name": "role_a"}]
    tasks = {
        "task-a": {
            "role": "role_a",
            "steps": [{"name": "s1", "runner": "claude", "skills": ["public_skill"]}],
        }
    }
    stages = [{"name": "Stage A", "task": "task-a"}]
    _write_config(tmp_path, roles=roles, tasks=tasks, stages=stages)
    _register_skill_plugin(tmp_path, monkeypatch, name="public_skill")

    problems = config_validation.validate_config(base_dir=tmp_path)

    assert problems == [], f"Unexpected problems for a public (ungated) skill: {problems}"


# ---------------------------------------------------------------------------
# min_role gating (token_service.ROLE_RANKS-backed, fail-closed on unknown)
# ---------------------------------------------------------------------------


def test_skill_min_role_satisfied_by_step_role_produces_no_error(
    tmp_path: Path, monkeypatch
) -> None:
    """The task's `role:` value must be a name recognized by
    `token_service.ROLE_RANKS` for the gate to ever be satisfiable (see
    Sprint 3 Agent Notes: agent-persona role names like `developer` are a
    different namespace than read/run/approve/admin)."""
    roles = [{"name": "admin"}]
    tasks = {
        "task-a": {
            "role": "admin",
            "steps": [{"name": "s1", "runner": "claude", "skills": ["gated_skill"]}],
        }
    }
    stages = [{"name": "Stage A", "task": "task-a"}]
    _write_config(tmp_path, roles=roles, tasks=tasks, stages=stages)
    _register_skill_plugin(tmp_path, monkeypatch, name="gated_skill", min_role="admin")

    problems = config_validation.validate_config(base_dir=tmp_path)

    assert problems == [], f"admin role must satisfy an admin-gated skill: {problems}"


def test_skill_min_role_too_high_for_step_role_is_error(tmp_path: Path, monkeypatch) -> None:
    roles = [{"name": "read"}]
    tasks = {
        "task-a": {
            "role": "read",
            "steps": [{"name": "s1", "runner": "claude", "skills": ["gated_skill"]}],
        }
    }
    stages = [{"name": "Stage A", "task": "task-a"}]
    _write_config(tmp_path, roles=roles, tasks=tasks, stages=stages)
    _register_skill_plugin(tmp_path, monkeypatch, name="gated_skill", min_role="admin")

    problems = config_validation.validate_config(base_dir=tmp_path)

    matching = [p for p in problems if "gated_skill" in p and "min_role" in p]
    assert matching, f"Expected a min_role problem, got: {problems}"


def test_skill_min_role_gate_on_pipeline_stage_resolves_via_referenced_task_role(
    tmp_path: Path, monkeypatch
) -> None:
    roles = [{"name": "read"}]
    tasks = {"task-a": {"role": "read", "steps": []}}
    stages = [{"name": "Stage A", "task": "task-a", "skills": ["gated_skill"]}]
    _write_config(tmp_path, roles=roles, tasks=tasks, stages=stages)
    _register_skill_plugin(tmp_path, monkeypatch, name="gated_skill", min_role="admin")

    problems = config_validation.validate_config(base_dir=tmp_path)

    matching = [p for p in problems if "gated_skill" in p and "min_role" in p]
    assert matching, f"Expected a min_role problem on the pipeline stage, got: {problems}"


def test_skill_min_role_gate_denies_when_step_role_is_unknown_to_role_ranks(
    tmp_path: Path, monkeypatch
) -> None:
    """A task role that is NOT a token_service.ROLE_RANKS name (e.g. a
    typical agent-persona role like 'developer') must be treated as an
    unknown role and DENIED -- never a fail-open pass because
    `token_service.role_rank()` returns -1 for it (authz-config-fail-closed
    mirror: never let a -1 sentinel reach a `<` gate)."""
    roles = [{"name": "developer"}]
    tasks = {
        "task-a": {
            "role": "developer",
            "steps": [{"name": "s1", "runner": "claude", "skills": ["gated_skill"]}],
        }
    }
    stages = [{"name": "Stage A", "task": "task-a"}]
    _write_config(tmp_path, roles=roles, tasks=tasks, stages=stages)
    _register_skill_plugin(tmp_path, monkeypatch, name="gated_skill", min_role="read")

    problems = config_validation.validate_config(base_dir=tmp_path)

    matching = [p for p in problems if "gated_skill" in p and "min_role" in p]
    assert matching, f"Expected an unknown-role denial (never a fail-open pass), got: {problems}"


def test_skill_min_role_gate_denies_when_task_has_no_role_at_all(
    tmp_path: Path, monkeypatch
) -> None:
    roles: list[dict] = []
    tasks = {
        "task-a": {
            "steps": [{"name": "s1", "runner": "claude", "skills": ["gated_skill"]}],
        }
    }
    stages = [{"name": "Stage A", "task": "task-a"}]
    _write_config(tmp_path, roles=roles, tasks=tasks, stages=stages)
    _register_skill_plugin(tmp_path, monkeypatch, name="gated_skill", min_role="read")

    problems = config_validation.validate_config(base_dir=tmp_path)

    matching = [p for p in problems if "gated_skill" in p and "min_role" in p]
    assert matching, f"Expected a min_role denial for a roleless task, got: {problems}"


# ---------------------------------------------------------------------------
# Dormancy gate: no `skills` anywhere -> byte-identical (golden/regression)
# ---------------------------------------------------------------------------


def test_no_skills_config_is_byte_identical_to_pre_sprint3_behavior(
    tmp_path: Path, monkeypatch
) -> None:
    """A config with no `skills` field anywhere must validate exactly as
    before Sprint 3 -- no new problems, and no PluginManager side effects
    (the skill-check block must not even construct a PluginManager when
    nothing references `skills`)."""
    roles = [
        {"name": "role_a", "inputs": [], "outputs": ["out1"]},
        {"name": "role_b", "inputs": ["out1"], "outputs": []},
    ]
    tasks: dict[str, dict[str, Any]] = {
        "task-a": {"role": "role_a", "steps": [{"name": "s1", "runner": "claude"}]},
        "task-b": {"role": "role_b", "steps": []},
    }
    stages = [
        {"name": "Stage A", "task": "task-a"},
        {"name": "Stage B", "task": "task-b"},
    ]
    _write_config(tmp_path, roles=roles, tasks=tasks, stages=stages)

    # Deliberately do NOT monkeypatch plugins.settings.base_dir or register
    # any plugin -- if the dormancy guard is broken, a real PluginManager()
    # would scan tmp_path (no plugins/ dir there) which is harmless, but
    # the point of this assertion is that no skill-related problem appears.
    called = {"plugin_manager_constructed": False}

    from hivepilot import plugins as plugins_mod

    original_init = plugins_mod.PluginManager.__init__

    def _spy_init(self, *a, **k):
        called["plugin_manager_constructed"] = True
        return original_init(self, *a, **k)

    monkeypatch.setattr(plugins_mod.PluginManager, "__init__", _spy_init)

    problems = config_validation.validate_config(base_dir=tmp_path)

    assert problems == [], f"Unexpected problems in a no-skills config: {problems}"
    assert called["plugin_manager_constructed"] is False, (
        "PluginManager must not be constructed when no `skills` field is present anywhere "
        "-- this is the dormancy/byte-identical gate."
    )


def test_existing_golden_explicit_base_dir_test_still_holds(tmp_path: Path) -> None:
    """Regression pin on the pre-existing golden test in
    tests/test_config_validation.py::test_explicit_base_dir_still_resolves_prompts_relative_to_it
    -- re-asserted here to guard against Sprint 3 breaking the no-skills path."""
    (tmp_path / "projects.yaml").write_text(
        yaml.dump({"projects": {"demo": {"path": "~/dev/demo"}}})
    )
    (tmp_path / "roles.yaml").write_text(
        yaml.dump({"roles": [{"name": "planner", "prompt_file": "planner.md"}]})
    )
    (tmp_path / "policies.yaml").write_text(yaml.dump({"policies": {}}))
    (tmp_path / "groups.yaml").write_text(yaml.dump({"groups": {}}))
    (tmp_path / "tasks.yaml").write_text(yaml.dump({"tasks": {}}))
    (tmp_path / "pipelines.yaml").write_text(yaml.dump({"pipelines": {}}))
    (tmp_path / "prompts" / "agents").mkdir(parents=True)
    (tmp_path / "prompts" / "agents" / "planner.md").write_text("# planner")

    problems = config_validation.validate_config(base_dir=tmp_path)

    assert problems == [], f"Unexpected problems: {problems}"


# Sanity: TaskStep/PipelineStage are importable from this test module's
# scope (used only to keep the import used, avoiding an unused-import lint
# failure while documenting the round-trip target types explicitly).
def test_taskstep_and_pipelinestage_are_the_round_trip_targets() -> None:
    assert TaskStep(name="s", runner="claude").skills is None
    assert PipelineStage(name="x", task="t").skills is None
