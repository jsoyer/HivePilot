"""Tests for the `pr_ready` swarm-bus wiring into
`hivepilot.services.git_service.create_pr`/`promote_pr` (Swarm Phase 1,
build item 7): publishing is BEST-EFFORT — a bus/publish failure must never
break the PR action that triggered it, and a successful open/promote
publishes exactly once with the branch/repo/sha the PR was opened for.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

from hivepilot.models import GitActions, ProjectConfig
from hivepilot.services import git_service


def _project(tmp_path: Path, **kwargs: Any) -> ProjectConfig:
    defaults: dict[str, Any] = dict(path=tmp_path, owner_repo="acme/widgets")
    defaults.update(kwargs)
    return ProjectConfig(**defaults)


class TestCreatePrPublishesPrReady:
    def test_successful_open_publishes_pr_ready(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        with (
            patch("hivepilot.services.git_service.subprocess.run"),
            patch("hivepilot.services.swarm_service.publish_pr_ready") as mock_publish,
        ):
            git_service.create_pr(project=project, branch="hivepilot/x", git=GitActions())
        mock_publish.assert_called_once()
        call_kwargs = mock_publish.call_args.kwargs
        assert call_kwargs["project_name"] == tmp_path.name
        assert call_kwargs["owner_repo"] == "acme/widgets"
        assert call_kwargs["branch"] == "hivepilot/x"
        assert call_kwargs["kind"] == "opened"

    def test_publish_failure_never_breaks_create_pr(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        with (
            patch("hivepilot.services.git_service.subprocess.run"),
            patch(
                "hivepilot.services.swarm_service.publish_pr_ready",
                side_effect=RuntimeError("swarm bus is down"),
            ),
        ):
            # Must not raise — publish is best-effort.
            git_service.create_pr(project=project, branch="hivepilot/x", git=GitActions())

    def test_failed_pr_open_never_publishes(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        with (
            patch("hivepilot.services.git_service.subprocess.run", side_effect=OSError("no gh")),
            patch("hivepilot.services.swarm_service.publish_pr_ready") as mock_publish,
        ):
            try:
                git_service.create_pr(project=project, branch="hivepilot/x", git=GitActions())
            except RuntimeError:
                pass
        mock_publish.assert_not_called()


class TestPromotePrPublishesPrReady:
    def test_successful_promote_publishes_pr_ready_kind_promoted(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        with (
            patch("hivepilot.services.git_service.subprocess.run"),
            patch("hivepilot.services.swarm_service.publish_pr_ready") as mock_publish,
        ):
            git_service.promote_pr(project=project, branch="hivepilot/x", git=GitActions())
        mock_publish.assert_called_once()
        assert mock_publish.call_args.kwargs["kind"] == "promoted"

    def test_publish_failure_never_breaks_promote_pr(self, tmp_path: Path) -> None:
        project = _project(tmp_path)
        with (
            patch("hivepilot.services.git_service.subprocess.run"),
            patch(
                "hivepilot.services.swarm_service.publish_pr_ready",
                side_effect=RuntimeError("swarm bus is down"),
            ),
        ):
            git_service.promote_pr(project=project, branch="hivepilot/x", git=GitActions())
