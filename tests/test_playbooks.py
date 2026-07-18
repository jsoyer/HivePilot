"""Tests for the Phase 16 multi-agent playbooks library
(hivepilot.scaffold.playbooks).

Covers:
- Registry surface: list_playbooks() / get_playbook() / PLAYBOOKS.
- scaffold_playbook(): writes expected files under playbooks/<name>/,
  refuses to overwrite without --force, and overwrites with force=True.
- The key correctness test: every playbook's scaffolded pipeline.yaml /
  tasks.yaml / roles.yaml PARSES VALID against the real pydantic models
  (PipelineConfig / TaskConfig / Role) — not just "is valid YAML".
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from hivepilot.models import PipelineConfig, TaskConfig
from hivepilot.roles import Role
from hivepilot.scaffold.playbooks import (
    PLAYBOOKS,
    Playbook,
    get_playbook,
    list_playbooks,
    scaffold_playbook,
)

PLAYBOOK_NAMES = ["plan-build-review", "explore-synthesize", "propose-challenge-revise"]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_playbooks_registry_has_exactly_the_three_v1_playbooks() -> None:
    assert set(PLAYBOOKS.keys()) == set(PLAYBOOK_NAMES)


def test_list_playbooks_returns_sorted_playbooks() -> None:
    playbooks = list_playbooks()
    assert [p.name for p in playbooks] == sorted(PLAYBOOK_NAMES)
    for p in playbooks:
        assert isinstance(p, Playbook)
        assert p.title
        assert p.description
        assert p.flow_summary
        assert p.files


@pytest.mark.parametrize("name", PLAYBOOK_NAMES)
def test_get_playbook_returns_playbook_for_known_name(name: str) -> None:
    playbook = get_playbook(name)
    assert playbook is not None
    assert playbook.name == name


def test_get_playbook_returns_none_for_unknown_name() -> None:
    assert get_playbook("does-not-exist") is None


@pytest.mark.parametrize("name", PLAYBOOK_NAMES)
def test_every_playbook_ships_the_expected_file_kinds(name: str) -> None:
    playbook = get_playbook(name)
    assert playbook is not None
    assert "pipeline.yaml" in playbook.files
    assert "tasks.yaml" in playbook.files
    assert "roles.yaml" in playbook.files
    assert "README.md" in playbook.files
    assert any(rel.startswith("prompts/") and rel.endswith(".md") for rel in playbook.files)


# ---------------------------------------------------------------------------
# scaffold_playbook()
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", PLAYBOOK_NAMES)
def test_scaffold_playbook_writes_expected_files(tmp_path: Path, name: str) -> None:
    playbook = get_playbook(name)
    assert playbook is not None

    created = scaffold_playbook(name, tmp_path)

    base = tmp_path / "playbooks" / name
    for rel in playbook.files:
        assert (base / rel).exists(), f"Expected file not created: {rel}"

    created_rels = {str(p.relative_to(base)) for p in created}
    assert created_rels == set(playbook.files.keys())


def test_scaffold_playbook_raises_for_unknown_name(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        scaffold_playbook("does-not-exist", tmp_path)


def test_scaffold_playbook_refuses_overwrite_without_force(tmp_path: Path) -> None:
    scaffold_playbook("plan-build-review", tmp_path)
    with pytest.raises(FileExistsError):
        scaffold_playbook("plan-build-review", tmp_path, force=False)


def test_scaffold_playbook_force_overwrites(tmp_path: Path) -> None:
    scaffold_playbook("plan-build-review", tmp_path)
    readme = tmp_path / "playbooks" / "plan-build-review" / "README.md"
    readme.write_text("# sentinel\n")

    created = scaffold_playbook("plan-build-review", tmp_path, force=True)

    assert readme.read_text() != "# sentinel\n"
    assert len(created) > 0


# ---------------------------------------------------------------------------
# Config validity — the key correctness test.
#
# A playbook that scaffolds config which does not parse against the real
# models is useless: this proves each scaffolded pipeline.yaml/tasks.yaml/
# roles.yaml actually constructs PipelineConfig/TaskConfig/Role successfully.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", PLAYBOOK_NAMES)
def test_scaffolded_roles_yaml_parses_as_valid_role_models(tmp_path: Path, name: str) -> None:
    scaffold_playbook(name, tmp_path)
    roles_path = tmp_path / "playbooks" / name / "roles.yaml"
    raw = yaml.safe_load(roles_path.read_text(encoding="utf-8"))

    assert "roles" in raw
    assert raw["roles"], "roles.yaml must declare at least one role"

    roles: dict[str, Role] = {}
    for entry in raw["roles"]:
        entry = dict(entry)
        # prompt_file is a bare filename in the fragment (relative to
        # prompts/agents/ once wired in) — Role expects a Path.
        entry["prompt_file"] = Path(entry["prompt_file"])
        role = Role(**entry)
        roles[role.name] = role

    assert len(roles) == len(raw["roles"]), "role names must be unique"


@pytest.mark.parametrize("name", PLAYBOOK_NAMES)
def test_scaffolded_tasks_yaml_parses_as_valid_task_config_models(
    tmp_path: Path, name: str
) -> None:
    scaffold_playbook(name, tmp_path)
    tasks_path = tmp_path / "playbooks" / name / "tasks.yaml"
    raw = yaml.safe_load(tasks_path.read_text(encoding="utf-8"))

    assert "tasks" in raw
    assert raw["tasks"], "tasks.yaml must declare at least one task"

    tasks: dict[str, TaskConfig] = {}
    for task_name, task_def in raw["tasks"].items():
        tasks[task_name] = TaskConfig(**task_def)

    assert len(tasks) == len(raw["tasks"])


@pytest.mark.parametrize("name", PLAYBOOK_NAMES)
def test_scaffolded_pipeline_yaml_parses_as_valid_pipeline_config_models(
    tmp_path: Path, name: str
) -> None:
    scaffold_playbook(name, tmp_path)
    pipeline_path = tmp_path / "playbooks" / name / "pipeline.yaml"
    raw = yaml.safe_load(pipeline_path.read_text(encoding="utf-8"))

    assert "pipelines" in raw
    assert raw["pipelines"], "pipeline.yaml must declare at least one pipeline"

    pipelines: dict[str, PipelineConfig] = {}
    for pipeline_name, pipeline_def in raw["pipelines"].items():
        pipelines[pipeline_name] = PipelineConfig(**pipeline_def)

    assert len(pipelines) == len(raw["pipelines"])
    assert name in pipelines, "the pipeline key should match the playbook name"


@pytest.mark.parametrize("name", PLAYBOOK_NAMES)
def test_scaffolded_pipeline_stages_reference_tasks_defined_in_tasks_yaml(
    tmp_path: Path, name: str
) -> None:
    """Cross-file consistency: every stage.task in pipeline.yaml must exist
    as a key in tasks.yaml, and every task's role must exist in roles.yaml —
    otherwise a scaffolded playbook would be internally inconsistent even
    though each file parses in isolation."""
    scaffold_playbook(name, tmp_path)
    base = tmp_path / "playbooks" / name

    pipeline_raw = yaml.safe_load((base / "pipeline.yaml").read_text(encoding="utf-8"))
    tasks_raw = yaml.safe_load((base / "tasks.yaml").read_text(encoding="utf-8"))
    roles_raw = yaml.safe_load((base / "roles.yaml").read_text(encoding="utf-8"))

    task_names = set(tasks_raw["tasks"].keys())
    role_names = {entry["name"] for entry in roles_raw["roles"]}

    (pipeline_def,) = pipeline_raw["pipelines"].values()
    for stage in pipeline_def["stages"]:
        assert stage["task"] in task_names, f"stage references unknown task: {stage['task']}"

    for task_def in tasks_raw["tasks"].values():
        role = task_def.get("role")
        if role is not None:
            assert role in role_names, f"task references unknown role: {role}"
