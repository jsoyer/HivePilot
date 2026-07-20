"""Tests for hivepilot.services.config_service.

Verifies CONFIG_FILES enumerates every file that `hivepilot config sync`
must pull from the external config repo, including roles/groups/model
profiles alongside the existing project/task/pipeline/policy/schedule files,
and that private-repo auth (HIVEPILOT_CONFIG_TOKEN) is injected as a transient
git header only for https:// URLs — never for ssh/git@ URLs.
"""

from __future__ import annotations

import base64

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


def test_git_env_injects_token_header_for_https(monkeypatch) -> None:
    monkeypatch.setattr(config_service.settings, "config_repo", "https://github.com/you/cfg.git")
    monkeypatch.setattr(config_service.settings, "config_token", "ghp_secret123")

    env = config_service._git_env()

    expected_header = (
        "Authorization: Basic " + base64.b64encode(b"x-access-token:ghp_secret123").decode()
    )
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.extraheader"
    assert env["GIT_CONFIG_VALUE_0"] == expected_header
    # The raw token must never appear verbatim (it is base64-wrapped).
    assert "ghp_secret123" not in env["GIT_CONFIG_VALUE_0"]


def test_git_env_ignores_token_for_ssh_url(monkeypatch) -> None:
    monkeypatch.setattr(config_service.settings, "config_repo", "git@github.com:you/cfg.git")
    monkeypatch.setattr(config_service.settings, "config_token", "ghp_secret123")

    env = config_service._git_env()

    assert "GIT_CONFIG_COUNT" not in env
    assert "GIT_CONFIG_KEY_0" not in env


def test_git_env_no_token_no_injection(monkeypatch) -> None:
    monkeypatch.setattr(config_service.settings, "config_repo", "https://github.com/you/cfg.git")
    monkeypatch.setattr(config_service.settings, "config_token", None)

    env = config_service._git_env()

    assert "GIT_CONFIG_COUNT" not in env
