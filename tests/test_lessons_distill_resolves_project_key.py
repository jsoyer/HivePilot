"""A `RunResult` carries a DIRECTORY name; `_project` expects a project KEY.

Measured on run 635:

    {"pipeline": "forage", "project": "forage", "run_id": 635,
     "error": "Unknown project: forage", "event": "lessons.distill_error"}

The project is keyed `greenfield-forage` and lives in `~/forage`. Every
`RunResult` in the pipeline path is built as `RunResult(project.path.name,
...)`, so the pipeline's distillation call does `self._project("forage")`
and raises. Lesson distillation has therefore NEVER run for any project
whose key differs from its folder — silently, because the call is wrapped
best-effort so a broken distiller cannot fail a pipeline.

It works everywhere the two happen to coincide (`noxys`, `hivepilot`),
which is why this survived the whole auto-learning PRD.

The resolution is deliberately narrow: try the key first, then the
directory basename among THIS run's own projects — and refuse to guess when
two of them share a basename, because attributing a lesson to the wrong
project is worse than not distilling it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.models import ProjectConfig, ProjectsFile
from hivepilot.orchestrator import Orchestrator


def _orch(projects: dict[str, ProjectConfig]) -> Orchestrator:
    orch = Orchestrator.__new__(Orchestrator)
    orch.projects = ProjectsFile(projects=projects)
    return orch


class TestResolvingAResultBackToItsProject:
    def test_a_key_that_equals_the_directory_still_resolves(self, tmp_path):
        """The case that always worked must keep working."""
        cfg = ProjectConfig(path=tmp_path / "noxys")
        orch = _orch({"noxys": cfg})

        assert orch._project_for_result("noxys", ["noxys"]) is cfg

    def test_a_directory_name_resolves_to_its_keyed_project(self, tmp_path):
        """The defect in one assertion: key `greenfield-forage`, folder
        `forage`, and the result carries the folder."""
        cfg = ProjectConfig(path=tmp_path / "forage")
        orch = _orch({"greenfield-forage": cfg})

        assert orch._project_for_result("forage", ["greenfield-forage"]) is cfg

    def test_an_ambiguous_directory_name_refuses_to_guess(self, tmp_path):
        """Two projects, one basename: distilling a lesson under the wrong
        project is worse than distilling none."""
        a = ProjectConfig(path=tmp_path / "a" / "forage")
        b = ProjectConfig(path=tmp_path / "b" / "forage")
        orch = _orch({"forage-a": a, "forage-b": b})

        assert orch._project_for_result("forage", ["forage-a", "forage-b"]) is None

    def test_a_name_belonging_to_no_project_resolves_to_nothing(self, tmp_path):
        """Some pipeline results carry a comma-joined component list or the
        pipeline's own name; those are not projects and must not raise."""
        orch = _orch({"noxys": ProjectConfig(path=tmp_path / "noxys")})

        assert orch._project_for_result("web, api", ["noxys"]) is None
        assert orch._project_for_result("forage", []) is None

    def test_the_search_stays_inside_this_run(self, tmp_path):
        """A basename match is only trusted among the projects this run
        actually targeted — a global scan would attribute a lesson to a
        project the run never touched."""
        target = ProjectConfig(path=tmp_path / "one" / "forage")
        stranger = ProjectConfig(path=tmp_path / "two" / "forage")
        orch = _orch({"keyed-target": target, "unrelated": stranger})

        assert orch._project_for_result("forage", ["keyed-target"]) is target


class TestThePipelineDistillsForAKeyedProject:
    def test_distillation_receives_the_project_not_an_exception(self, tmp_path):
        """End of the chain: with the folder/key mismatch in place, the
        pipeline's distill call must hand a real ProjectConfig through."""
        cfg = ProjectConfig(path=tmp_path / "forage")
        orch = _orch({"greenfield-forage": cfg})

        resolved = orch._project_for_result("forage", ["greenfield-forage"])

        assert isinstance(resolved, ProjectConfig)
        assert resolved.path.name == "forage"

    @pytest.mark.parametrize("missing", ["", "  "])
    def test_a_blank_result_name_resolves_to_nothing(self, tmp_path, missing):
        orch = _orch({"noxys": ProjectConfig(path=Path(tmp_path) / "noxys")})

        assert orch._project_for_result(missing, ["noxys"]) is None
