"""The feedback log was the ninth cwd-relative silo, and the only one that
littered.

``knowledge_service`` opened with two module-level constants::

    FEEDBACK_DIR = settings.base_dir / ".hivepilot" / "feedback"
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)

``settings.base_dir`` defaults to ``Path.cwd()`` at process start, so this is
the incident #1 failure mode again -- a service started at ``cwd=/`` and a CLI
run from an operator's home directory write two different logs, and the agent
prompt built from "the last five feedback entries" silently reads whichever one
belongs to the directory the command was typed from.

Two things make it worse than the other eight:

* It is frozen at IMPORT. Pinning ``HIVEPILOT_BASE_DIR`` afterwards fixes every
  other path and leaves this one pointing where it already was.
* The ``mkdir`` runs at import too, so merely importing the module creates a
  ``.hivepilot/feedback`` directory in whatever directory the process happened
  to start in. That is where the stray ``.hivepilot`` directories came from.

And ``check_cwd_relative_paths`` could not see it: its table is keyed by
``Settings`` attribute name, and this path is not a setting.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


class TestImportingTheModuleLittersNothing:
    def test_no_directory_appears_in_the_process_cwd(self, tmp_path: Path) -> None:
        """The whole import side effect, in one assertion.

        A subprocess, because the module is long since imported in this one --
        and a subprocess is also exactly the shape of the real defect: a
        process whose cwd is not the operator's.
        """
        result = subprocess.run(
            [sys.executable, "-c", "import hivepilot.services.knowledge_service"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert not (tmp_path / ".hivepilot").exists(), (
            "importing the module created a directory in the process's cwd"
        )


class TestTheResolvedPathFollowsThePin:
    def test_two_cwds_agree_once_base_dir_is_pinned(self, tmp_path: Path) -> None:
        """Pinning HIVEPILOT_BASE_DIR must actually move the feedback log.

        With the old module-level constant it did not: the value was computed
        before any per-run configuration could apply.
        """
        pinned = tmp_path / "pinned"
        pinned.mkdir()
        code = "from hivepilot.services import knowledge_service as k;print(k.feedback_dir())"
        env_pin = {"HIVEPILOT_BASE_DIR": str(pinned)}

        first = _run(code, cwd=tmp_path, env=env_pin)
        second = _run(code, cwd=pinned, env=env_pin)

        assert first == second
        assert first.startswith(str(pinned))

    def test_without_a_pin_it_follows_the_cwd(self, tmp_path: Path) -> None:
        """The defect itself, stated as behaviour rather than as a warning.

        This is not a bug being asserted as correct -- it is `base_dir`'s
        documented fallback. The point is that it is REAL, which is why the
        doctor has to name it.
        """
        other = tmp_path / "other"
        other.mkdir()
        code = "from hivepilot.services import knowledge_service as k;print(k.feedback_dir())"

        assert _run(code, cwd=tmp_path) != _run(code, cwd=other)


class TestTheDoctorNamesIt:
    def test_an_unpinned_feedback_dir_is_reported(self, monkeypatch, tmp_path: Path) -> None:
        from hivepilot.services import config_doctor

        monkeypatch.delenv("HIVEPILOT_BASE_DIR", raising=False)
        monkeypatch.setattr(config_doctor.settings, "base_dir", tmp_path)

        labels = [f.message for f in config_doctor.check_cwd_relative_paths()]

        assert any("feedback" in message for message in labels), (
            "the ninth silo must be named by the check that exists to name silos"
        )

    def test_a_pinned_feedback_dir_is_not_reported(self, monkeypatch, tmp_path: Path) -> None:
        from hivepilot.services import config_doctor

        monkeypatch.setenv("HIVEPILOT_BASE_DIR", str(tmp_path))

        labels = [f.message for f in config_doctor.check_cwd_relative_paths()]

        assert not any("feedback" in message for message in labels)

    def test_it_is_printed_among_the_resolved_paths(self, monkeypatch, tmp_path: Path) -> None:
        """`describe_resolved_paths` prints unconditionally, so an operator can
        see where the log actually is without first having a problem."""
        from hivepilot.services import config_doctor

        monkeypatch.setattr(config_doctor.settings, "base_dir", tmp_path)

        assert any("feedback" in line for line in config_doctor.describe_resolved_paths())


class TestFeedbackStillRoundTrips:
    """The path moved; the behaviour must not."""

    def test_a_recorded_entry_is_read_back(self, monkeypatch, tmp_path: Path) -> None:
        from hivepilot.services import knowledge_service

        monkeypatch.setattr(knowledge_service.settings, "base_dir", tmp_path)
        project = tmp_path / "proj"
        project.mkdir()

        knowledge_service.append_feedback(project, "build", "it worked")

        assert "it worked" in "".join(knowledge_service._latest_feedback(project))

    def test_the_directory_is_created_on_write_not_on_import(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        from hivepilot.services import knowledge_service

        monkeypatch.setattr(knowledge_service.settings, "base_dir", tmp_path)
        assert not (tmp_path / ".hivepilot" / "feedback").exists()

        knowledge_service.append_feedback(tmp_path, "build", "ok")

        assert (tmp_path / ".hivepilot" / "feedback").is_dir()


def _run(code: str, *, cwd: Path, env: dict[str, str] | None = None) -> str:
    import os

    merged = {**os.environ, **(env or {})}
    if env is None:
        merged.pop("HIVEPILOT_BASE_DIR", None)
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=cwd, capture_output=True, text=True, env=merged
    )
    if result.returncode != 0:
        pytest.fail(result.stderr)
    return result.stdout.strip()
