"""Structure of the reordered noxys-v2 pipeline.

Planning phase (CEO → CTO → CISO architecture → Jules synthesis) ends with a
human plan checkpoint before the developer; then dev → review (PR) → CISO code
clearance → QA → docs → Jules final PR approval.
"""

from __future__ import annotations

from hivepilot.services.pipeline_service import validate_pipeline
from hivepilot.services.project_service import load_pipelines, load_tasks

EXPECTED_STAGES = [
    ("CEO Intake", "noxys-ceo-intake"),
    ("CTO Architecture", "noxys-cto-review"),
    ("Security (architecture)", "noxys-ciso"),
    ("Plan Synthesis", "noxys-cos-synthesis"),
    ("Implementation", "noxys-developer"),
    ("Review", "noxys-reviewer"),
    ("Security (code)", "noxys-ciso"),
    ("QA", "noxys-qa"),
    ("Documentation", "noxys-documentation"),
    ("PR Approval", "noxys-cos-pr-approval"),
]


def _pipeline():
    return load_pipelines().pipelines["noxys-v2"]


def test_company_v2_exists_and_validates() -> None:
    validate_pipeline(_pipeline(), load_tasks())  # all referenced tasks must exist


def test_stage_order_matches_spec() -> None:
    stages = [(s.name, s.task) for s in _pipeline().stages]
    assert stages == EXPECTED_STAGES


def test_checkpoint_is_before_implementation_only() -> None:
    paused = [s.name for s in _pipeline().stages if s.pause_before]
    assert paused == ["Implementation"]


def test_ciso_runs_twice() -> None:
    ciso_stages = [s.name for s in _pipeline().stages if s.task == "noxys-ciso"]
    assert ciso_stages == ["Security (architecture)", "Security (code)"]


def test_new_cos_tasks_are_chief_of_staff() -> None:
    tasks = load_tasks().tasks
    assert tasks["noxys-cos-synthesis"].role == "chief_of_staff"
    assert tasks["noxys-cos-pr-approval"].role == "chief_of_staff"
