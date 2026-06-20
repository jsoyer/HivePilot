"""Structure of the reordered company-v2 pipeline.

Planning phase (CEO → CTO → CISO architecture → Jules synthesis) ends with a
human plan checkpoint before the developer; then dev → review (PR) → CISO code
clearance → QA → docs → Jules final PR approval.
"""

from __future__ import annotations

from hivepilot.services.pipeline_service import validate_pipeline
from hivepilot.services.project_service import load_pipelines, load_tasks

EXPECTED_STAGES = [
    ("CEO Intake", "company-ceo-intake"),
    ("CTO Architecture", "company-cto-review"),
    ("Security (architecture)", "company-ciso"),
    ("Plan Synthesis", "company-cos-synthesis"),
    ("Implementation", "company-developer"),
    ("Review", "company-reviewer"),
    ("Security (code)", "company-ciso"),
    ("QA", "company-qa"),
    ("Documentation", "company-documentation"),
    ("PR Approval", "company-cos-pr-approval"),
]


def _pipeline():
    return load_pipelines().pipelines["company-v2"]


def test_company_v2_exists_and_validates() -> None:
    validate_pipeline(_pipeline(), load_tasks())  # all referenced tasks must exist


def test_stage_order_matches_spec() -> None:
    stages = [(s.name, s.task) for s in _pipeline().stages]
    assert stages == EXPECTED_STAGES


def test_checkpoint_is_before_implementation_only() -> None:
    paused = [s.name for s in _pipeline().stages if s.pause_before]
    assert paused == ["Implementation"]


def test_ciso_runs_twice() -> None:
    ciso_stages = [s.name for s in _pipeline().stages if s.task == "company-ciso"]
    assert ciso_stages == ["Security (architecture)", "Security (code)"]


def test_new_cos_tasks_are_chief_of_staff() -> None:
    tasks = load_tasks().tasks
    assert tasks["company-cos-synthesis"].role == "chief_of_staff"
    assert tasks["company-cos-pr-approval"].role == "chief_of_staff"
