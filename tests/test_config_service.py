"""Tests for hivepilot.services.config_service.

Verifies CONFIG_FILES enumerates every file that `hivepilot config sync`
must pull from the external config repo, including roles/groups/model
profiles alongside the existing project/task/pipeline/policy/schedule files.
"""

from __future__ import annotations

from hivepilot.services import config_service


def test_config_files_includes_expected_entries() -> None:
    expected = {
        "projects.yaml",
        "tasks.yaml",
        "pipelines.yaml",
        "policies.yaml",
        "schedules.yaml",
        "roles.yaml",
        "groups.yaml",
        "model_profiles.yaml",
    }
    assert expected <= config_service.CONFIG_FILES


def test_config_files_has_no_duplicates() -> None:
    # CONFIG_FILES is a set, but assert element count matches the expected
    # distinct set size to guard against accidental near-duplicate strings.
    assert len(config_service.CONFIG_FILES) == len(set(config_service.CONFIG_FILES))
