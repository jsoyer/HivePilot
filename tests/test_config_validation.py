"""Tests for hivepilot.services.config_validation prompts_dir resolution.

Verifies that when validate_config() is called with the default (no
explicit base_dir), the prompts directory is resolved through the
XDG/config_repo-aware `settings.resolve_config_path`, so an externally
overridden prompts dir is honored -- while explicit base_dir callers
(e.g. tests that point at a tmp_path) keep their existing cwd-relative
behavior untouched.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from hivepilot.config import Settings
from hivepilot.services import config_validation


def _write_minimal_config(base_dir: Path) -> None:
    (base_dir / "projects.yaml").write_text(
        yaml.dump({"projects": {"demo": {"path": "~/dev/demo"}}})
    )
    (base_dir / "roles.yaml").write_text(
        yaml.dump({"roles": [{"name": "planner", "prompt_file": "planner.md"}]})
    )
    (base_dir / "policies.yaml").write_text(yaml.dump({"policies": {}}))
    (base_dir / "groups.yaml").write_text(yaml.dump({"groups": {}}))
    (base_dir / "tasks.yaml").write_text(yaml.dump({"tasks": {}}))
    (base_dir / "pipelines.yaml").write_text(yaml.dump({"pipelines": {}}))


def test_explicit_base_dir_still_resolves_prompts_relative_to_it(tmp_path: Path) -> None:
    """Explicit base_dir callers (existing test suite pattern) must keep
    resolving prompts/agents relative to that base_dir, not settings.base_dir."""
    _write_minimal_config(tmp_path)
    (tmp_path / "prompts" / "agents").mkdir(parents=True)
    (tmp_path / "prompts" / "agents" / "planner.md").write_text("# planner")

    problems = config_validation.validate_config(base_dir=tmp_path)

    assert problems == [], f"Unexpected problems: {problems}"


def test_default_base_dir_uses_resolve_config_path(monkeypatch, tmp_path: Path) -> None:
    """When base_dir is omitted, prompts_dir must come from
    settings.resolve_config_path('prompts'), not a hardcoded cwd join."""
    override_prompts = tmp_path / "external-prompts"
    (override_prompts / "agents").mkdir(parents=True)
    (override_prompts / "agents" / "planner.md").write_text("# planner")

    cwd_dir = tmp_path / "cwd"
    cwd_dir.mkdir()
    _write_minimal_config(cwd_dir)
    monkeypatch.chdir(cwd_dir)

    calls: list[str] = []

    def fake_resolve_config_path(self, filename):
        calls.append(str(filename))
        return override_prompts

    # Settings is a pydantic BaseSettings instance; instance attributes can't
    # be reassigned arbitrarily, so patch the method on the class instead.
    monkeypatch.setattr(Settings, "resolve_config_path", fake_resolve_config_path)

    problems = config_validation.validate_config()

    assert "prompts" in calls, "resolve_config_path('prompts') was never called"
    assert problems == [], f"Unexpected problems: {problems}"
