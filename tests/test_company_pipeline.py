"""
Tests for the 'company' pipeline definition and artifact write wiring.

Covers:
- company pipeline loads from pipelines.yaml with exactly 10 ordered stages
- Each stage maps to an existing task in tasks.yaml
- Each task references a real prompts/agents/<role>.md file
- The pipeline-step artifact write is dry-run safe (no real files written)
- Artifact write calls ObsidianService.write_note targeting 12 - HivePilot/Runs/
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

WORKTREE_ROOT = Path(__file__).parent.parent
PIPELINES_YAML = WORKTREE_ROOT / "pipelines.yaml"
TASKS_YAML = WORKTREE_ROOT / "tasks.yaml"
PROMPTS_AGENTS_DIR = WORKTREE_ROOT / "prompts" / "agents"

EXPECTED_STAGE_ORDER = [
    "CEO Intake",
    "Chief of Staff Plan",
    "CTO Review",
    "Implementation",
    "Review",
    "Security",
    "QA",
    "Documentation",
    "Report",
    "Approval",
]


@pytest.fixture()
def pipelines_data() -> dict:
    return yaml.safe_load(PIPELINES_YAML.read_text())


@pytest.fixture()
def tasks_data() -> dict:
    return yaml.safe_load(TASKS_YAML.read_text())


# ---------------------------------------------------------------------------
# Pipeline structure
# ---------------------------------------------------------------------------


class TestCompanyPipelineStructure:
    """company pipeline must have 10 correctly-ordered stages."""

    def test_company_pipeline_exists(self, pipelines_data: dict) -> None:
        assert "company" in pipelines_data["pipelines"], (
            "Expected a 'company' key under pipelines: in pipelines.yaml"
        )

    def test_company_pipeline_has_description(self, pipelines_data: dict) -> None:
        pipeline = pipelines_data["pipelines"]["company"]
        assert pipeline.get("description"), "company pipeline must have a description"

    def test_company_pipeline_has_10_stages(self, pipelines_data: dict) -> None:
        stages = pipelines_data["pipelines"]["company"]["stages"]
        assert len(stages) == 10, f"Expected 10 stages, got {len(stages)}"

    def test_company_stage_names_are_ordered(self, pipelines_data: dict) -> None:
        stages = pipelines_data["pipelines"]["company"]["stages"]
        actual_names = [s["name"] for s in stages]
        assert actual_names == EXPECTED_STAGE_ORDER, (
            f"Stage names or order wrong.\nExpected: {EXPECTED_STAGE_ORDER}\nGot: {actual_names}"
        )

    def test_each_stage_has_a_task_key(self, pipelines_data: dict) -> None:
        stages = pipelines_data["pipelines"]["company"]["stages"]
        for stage in stages:
            assert "task" in stage, f"Stage '{stage['name']}' has no 'task' key"
            assert stage["task"], f"Stage '{stage['name']}' has an empty 'task' value"


# ---------------------------------------------------------------------------
# Task → prompt file wiring
# ---------------------------------------------------------------------------


class TestCompanyPipelineTasks:
    """Each company stage task must exist in tasks.yaml and reference a valid prompt."""

    def test_all_stage_tasks_exist_in_tasks_yaml(
        self, pipelines_data: dict, tasks_data: dict
    ) -> None:
        stages = pipelines_data["pipelines"]["company"]["stages"]
        defined_tasks = tasks_data.get("tasks", {})
        for stage in stages:
            task_name = stage["task"]
            assert task_name in defined_tasks, (
                f"Stage '{stage['name']}' references task '{task_name}' "
                f"which is NOT defined in tasks.yaml"
            )

    def test_all_stage_tasks_have_prompt_file(self, pipelines_data: dict, tasks_data: dict) -> None:
        stages = pipelines_data["pipelines"]["company"]["stages"]
        defined_tasks = tasks_data.get("tasks", {})
        for stage in stages:
            task_name = stage["task"]
            task = defined_tasks.get(task_name, {})
            steps = task.get("steps", [])
            assert steps, f"Task '{task_name}' (stage '{stage['name']}') has no steps"
            # At least the first step must carry a prompt_file
            first_step = steps[0]
            assert first_step.get("prompt_file"), (
                f"Task '{task_name}' first step has no prompt_file"
            )

    def test_all_prompt_files_exist_on_disk(self, pipelines_data: dict, tasks_data: dict) -> None:
        stages = pipelines_data["pipelines"]["company"]["stages"]
        defined_tasks = tasks_data.get("tasks", {})
        for stage in stages:
            task_name = stage["task"]
            task = defined_tasks.get(task_name, {})
            for step in task.get("steps", []):
                pf = step.get("prompt_file")
                if pf and pf.startswith("prompts/agents/"):
                    full_path = WORKTREE_ROOT / pf
                    assert full_path.exists(), (
                        f"Prompt file '{pf}' referenced by task '{task_name}' "
                        f"does not exist at {full_path}"
                    )


# ---------------------------------------------------------------------------
# Pydantic model round-trip
# ---------------------------------------------------------------------------


class TestCompanyPipelinePydantic:
    """Parsed YAML must load cleanly through PipelineConfig."""

    def test_pipelines_file_parses(self) -> None:
        from hivepilot.models import PipelinesFile

        data = yaml.safe_load(PIPELINES_YAML.read_text())
        pf = PipelinesFile(**data)
        assert "company" in pf.pipelines

    def test_company_pipeline_stages_count(self) -> None:
        from hivepilot.models import PipelinesFile

        data = yaml.safe_load(PIPELINES_YAML.read_text())
        pf = PipelinesFile(**data)
        company = pf.pipelines["company"]
        assert len(company.stages) == 10

    def test_tasks_file_parses_with_company_tasks(self) -> None:
        from hivepilot.models import TasksFile

        data = yaml.safe_load(TASKS_YAML.read_text())
        tf = TasksFile(**data)
        company_task_keys = {
            "ceo-intake",
            "cos-plan",
            "cto-review",
            "developer",
            "reviewer",
            "ciso",
            "qa",
            "documentation",
            "cos-report",
            "cos-synthesis",
            "cos-pr-approval",
            "ceo-approval",
        }
        company_tasks = [k for k in tf.tasks if k in company_task_keys]
        assert len(company_tasks) >= 9, (
            f"Expected at least 9 company tasks, found {len(company_tasks)}: {company_tasks}"
        )


# ---------------------------------------------------------------------------
# Artifact write — dry-run safety
# ---------------------------------------------------------------------------


class TestArtifactWriteDryRun:
    """Artifact writes must be dry-run safe: no real files created in tests."""

    def test_write_stage_artifact_dry_run_returns_dict(self, tmp_path: Path) -> None:
        """write_stage_artifact with dry_run=True returns a dict, writes nothing."""
        from hivepilot.pipelines import write_stage_artifact

        result = write_stage_artifact(
            vault_path=tmp_path,
            run_id=42,
            stage_name="CEO Intake",
            output="some output text",
            dry_run=True,
        )
        assert isinstance(result, dict)
        assert result["dry_run"] is True
        # No files should have been created under tmp_path
        written = list(tmp_path.rglob("*.md"))
        assert written == [], f"Dry-run should not write files, found: {written}"

    def test_write_stage_artifact_targets_runs_folder(self, tmp_path: Path) -> None:
        """The planned path must be inside 12 - HivePilot/Runs/."""
        from hivepilot.pipelines import write_stage_artifact

        result = write_stage_artifact(
            vault_path=tmp_path,
            run_id=7,
            stage_name="CTO Review",
            output="review content",
            dry_run=True,
        )
        planned_path = result["path"]
        assert "12 - HivePilot" in planned_path, (
            f"Expected path inside '12 - HivePilot/', got: {planned_path}"
        )
        assert "Runs" in planned_path, f"Expected path inside 'Runs/', got: {planned_path}"

    def test_write_stage_artifact_no_vault_is_noop(self) -> None:
        """When vault_path is None, write_stage_artifact must return None silently."""
        from hivepilot.pipelines import write_stage_artifact

        result = write_stage_artifact(
            vault_path=None,
            run_id=1,
            stage_name="QA",
            output="qa output",
            dry_run=True,
        )
        assert result is None

    def test_write_stage_artifact_real_write(self, tmp_path: Path) -> None:
        """With dry_run=False, the file must actually be written."""
        from hivepilot.pipelines import write_stage_artifact

        # Ensure the subtree exists so write_note can target it
        runs_dir = tmp_path / "12 - HivePilot" / "Runs"
        runs_dir.mkdir(parents=True)

        result = write_stage_artifact(
            vault_path=tmp_path,
            run_id=99,
            stage_name="Implementation",
            output="impl output",
            dry_run=False,
        )
        assert result is not None
        assert result["dry_run"] is False
        written = list(tmp_path.rglob("*.md"))
        assert len(written) == 1, f"Expected exactly 1 written file, got: {written}"
