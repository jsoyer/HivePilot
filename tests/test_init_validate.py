"""Tests for hivepilot init (scaffold) and validate commands."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXPECTED_FILES = [
    "projects.yaml",
    "roles.yaml",
    "policies.yaml",
    "groups.yaml",
    "pipelines.yaml",
    "tasks.yaml",
    ".env.example",
    "prompts/agents/planner.md",
    "prompts/agents/reviewer.md",
    "prompts/agents/developer.md",
]


# ---------------------------------------------------------------------------
# scaffold_config tests
# ---------------------------------------------------------------------------


def test_init_creates_files(tmp_path: Path) -> None:
    """scaffold_config should create all expected files."""
    from hivepilot.scaffold.templates import scaffold_config

    created = scaffold_config(tmp_path)
    for rel in EXPECTED_FILES:
        assert (tmp_path / rel).exists(), f"Expected file not created: {rel}"
    # created list should include all expected files
    created_rels = {str(p.relative_to(tmp_path)) for p in created}
    for rel in EXPECTED_FILES:
        assert rel in created_rels, f"{rel} not in returned list"


def test_init_refuses_overwrite(tmp_path: Path) -> None:
    """scaffold_config should raise FileExistsError when files exist and force=False."""
    from hivepilot.scaffold.templates import scaffold_config

    # Pre-create one of the target files
    (tmp_path / "projects.yaml").write_text("# pre-existing\n")
    with pytest.raises(FileExistsError):
        scaffold_config(tmp_path, force=False)


def test_init_force_overwrites(tmp_path: Path) -> None:
    """scaffold_config with force=True should succeed even when files already exist."""
    from hivepilot.scaffold.templates import scaffold_config

    # Pre-create one of the target files with sentinel content
    existing = tmp_path / "projects.yaml"
    existing.write_text("# pre-existing\n")

    created = scaffold_config(tmp_path, force=True)
    # File must have been overwritten (no longer "pre-existing" only)
    assert existing.exists()
    assert existing.read_text() != "# pre-existing\n"
    # All expected files should exist
    for rel in EXPECTED_FILES:
        assert (tmp_path / rel).exists(), f"Expected file not created: {rel}"
    assert len(created) > 0


# ---------------------------------------------------------------------------
# validate_config tests
# ---------------------------------------------------------------------------

# The repo root is two levels above this test file's directory:
# tests/ -> repo root
REPO_ROOT = Path(__file__).parent.parent


def test_validate_current_config_clean() -> None:
    """validate_config on the live repo-root config should return no problems."""
    from hivepilot.services.config_validation import validate_config

    problems = validate_config(base_dir=REPO_ROOT)
    assert problems == [], "Unexpected problems in repo config:\n" + "\n".join(problems)


def test_validate_broken_config(tmp_path: Path) -> None:
    """validate_config should report a missing task when a pipeline references one."""
    from hivepilot.services.config_validation import validate_config

    # Minimal valid projects.yaml
    (tmp_path / "projects.yaml").write_text(
        yaml.dump({"projects": {"example-project": {"path": "~/dev/example"}}})
    )
    # roles.yaml with one role
    (tmp_path / "roles.yaml").write_text(
        yaml.dump({"roles": [{"name": "planner", "prompt_file": "planner.md"}]})
    )
    # policies.yaml — empty projects section
    (tmp_path / "policies.yaml").write_text(
        yaml.dump({"policies": {"default": {"allow_auto_git": True}}})
    )
    # groups.yaml — empty
    (tmp_path / "groups.yaml").write_text(yaml.dump({"groups": {}}))
    # tasks.yaml — one valid task
    (tmp_path / "tasks.yaml").write_text(
        yaml.dump(
            {
                "tasks": {
                    "real-task": {
                        "description": "A real task",
                        "role": "planner",
                        "steps": [],
                    }
                }
            }
        )
    )
    # pipelines.yaml — references a task that does NOT exist
    (tmp_path / "pipelines.yaml").write_text(
        yaml.dump(
            {
                "pipelines": {
                    "my-pipeline": {
                        "description": "Test pipeline",
                        "stages": [
                            {"name": "step1", "task": "missing-task"},
                        ],
                    }
                }
            }
        )
    )
    # Create prompt file so role validation passes
    (tmp_path / "prompts" / "agents").mkdir(parents=True)
    (tmp_path / "prompts" / "agents" / "planner.md").write_text("# Planner\n")

    problems = validate_config(base_dir=tmp_path)
    assert any("missing-task" in p for p in problems), (
        f"Expected a problem mentioning 'missing-task', got: {problems}"
    )
