"""Tests for hivepilot.services.config_validation prompts_dir resolution.

Verifies that when validate_config() is called with the default (no
explicit base_dir), the prompts directory is resolved through the
XDG/config_repo-aware `settings.resolve_config_path`, so an externally
overridden prompts dir is honored -- while explicit base_dir callers
(e.g. tests that point at a tmp_path) keep their existing cwd-relative
behavior untouched.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest
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


# ---------------------------------------------------------------------------
# PRD A2 Sprint 3 -- dangling-input data-flow check
# ---------------------------------------------------------------------------


def _write_config(
    base_dir: Path,
    *,
    roles: list[dict],
    tasks: dict[str, dict],
    stages: list[dict],
) -> None:
    """Write a minimal-but-complete config directory for the dangling-input
    checks: a single pipeline ("demo") wired from *roles*/*tasks*/*stages*.
    No prompt_file is set on any role, so the prompt-file-exists check
    (unrelated to this feature) never fires."""
    (base_dir / "projects.yaml").write_text(yaml.dump({"projects": {}}))
    (base_dir / "roles.yaml").write_text(yaml.dump({"roles": roles}))
    (base_dir / "policies.yaml").write_text(yaml.dump({"policies": {}}))
    (base_dir / "groups.yaml").write_text(yaml.dump({"groups": {}}))
    (base_dir / "tasks.yaml").write_text(yaml.dump({"tasks": tasks}))
    (base_dir / "pipelines.yaml").write_text(
        yaml.dump({"pipelines": {"demo": {"stages": stages}}})
    )


def test_dangling_input_warns_in_full_mode_but_does_not_fail_validate(
    tmp_path: Path,
) -> None:
    """A stage whose role declares an input that no earlier stage produces
    is surfaced as a warning in the default ("full") routing mode, but
    `problems` stays empty -- `config validate` must still report OK."""
    roles = [
        {"name": "role_a", "inputs": [], "outputs": ["out1"]},
        {"name": "role_b", "inputs": ["out1", "out2"], "outputs": []},
    ]
    tasks = {
        "task-a": {"role": "role_a"},
        "task-b": {"role": "role_b"},
    }
    stages = [
        {"name": "Stage A", "task": "task-a"},
        {"name": "Stage B", "task": "task-b"},
    ]
    _write_config(tmp_path, roles=roles, tasks=tasks, stages=stages)

    with pytest.warns(UserWarning, match="out2") as record:
        problems = config_validation.validate_config(base_dir=tmp_path)

    assert problems == [], f"Unexpected hard problems in full mode: {problems}"
    assert any("Stage B" in str(w.message) for w in record)
    assert any("dangling input" in str(w.message) for w in record)


def test_clean_config_has_no_dangling_input_finding(tmp_path: Path) -> None:
    """Every input is produced by an earlier stage's outputs -- no warning,
    no problem."""
    roles = [
        {"name": "role_a", "inputs": [], "outputs": ["out1"]},
        {"name": "role_b", "inputs": ["out1"], "outputs": ["out2"]},
    ]
    tasks = {
        "task-a": {"role": "role_a"},
        "task-b": {"role": "role_b"},
    }
    stages = [
        {"name": "Stage A", "task": "task-a"},
        {"name": "Stage B", "task": "task-b"},
    ]
    _write_config(tmp_path, roles=roles, tasks=tasks, stages=stages)

    with warnings.catch_warnings(record=True) as record:
        warnings.simplefilter("always")
        problems = config_validation.validate_config(base_dir=tmp_path)

    dangling_warnings = [w for w in record if "dangling input" in str(w.message)]
    assert dangling_warnings == [], f"Unexpected dangling-input warnings: {dangling_warnings}"
    assert problems == [], f"Unexpected problems: {problems}"


def test_dangling_input_is_hard_error_in_keyed_mode(tmp_path: Path, monkeypatch) -> None:
    """The same dangling input that is only a warning in `full` mode becomes
    a hard `problems` entry once `context_routing_mode` is `keyed`."""
    roles = [
        {"name": "role_a", "inputs": [], "outputs": ["out1"]},
        {"name": "role_b", "inputs": ["out1", "out2"], "outputs": []},
    ]
    tasks = {
        "task-a": {"role": "role_a"},
        "task-b": {"role": "role_b"},
    }
    stages = [
        {"name": "Stage A", "task": "task-a"},
        {"name": "Stage B", "task": "task-b"},
    ]
    _write_config(tmp_path, roles=roles, tasks=tasks, stages=stages)

    monkeypatch.setattr(config_validation.settings, "context_routing_mode", "keyed")

    problems = config_validation.validate_config(base_dir=tmp_path)

    assert any("out2" in p and "dangling input" in p for p in problems), (
        f"Expected a dangling-input problem for 'out2', got: {problems}"
    )


def test_existing_noxys_style_cosmetic_dangling_inputs_still_pass_in_full_mode(
    tmp_path: Path,
) -> None:
    """Mirrors the bundled Noxys roles.yaml: every role declares `inputs`
    that include upstream-external keys (roadmap, architecture_docs, ...)
    that no role `outputs` -- purely cosmetic documentation. `config
    validate` must still pass (empty `problems`) in the default full mode,
    even though several dangling-input warnings fire."""
    roles = [
        {
            "name": "ceo",
            "inputs": ["roadmap", "metrics", "customer_feedback"],
            "outputs": ["objectives", "priorities", "constraints"],
        },
        {
            "name": "cto",
            "inputs": ["objectives", "architecture_docs", "tech_debt_log"],
            "outputs": ["technical_spec", "adr"],
        },
        {
            "name": "developer",
            "inputs": ["technical_spec", "architecture_docs", "codebase_context"],
            "outputs": ["implementation", "test_suite"],
        },
    ]
    tasks = {
        "ceo-intake": {"role": "ceo"},
        "cto-review": {"role": "cto"},
        "developer": {"role": "developer"},
    }
    stages = [
        {"name": "CEO Intake", "task": "ceo-intake"},
        {"name": "CTO Review", "task": "cto-review"},
        {"name": "Implementation", "task": "developer"},
    ]
    _write_config(tmp_path, roles=roles, tasks=tasks, stages=stages)

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        problems = config_validation.validate_config(base_dir=tmp_path)

    assert problems == [], f"Cosmetic dangling inputs must not fail full-mode validate: {problems}"
