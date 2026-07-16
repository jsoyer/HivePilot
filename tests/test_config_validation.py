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
    (base_dir / "pipelines.yaml").write_text(yaml.dump({"pipelines": {"demo": {"stages": stages}}}))


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


def test_optional_input_not_flagged_as_dangling_in_keyed_mode(tmp_path: Path, monkeypatch) -> None:
    """A role's `optional_inputs` key that no earlier stage produces must
    NOT be flagged as a dangling input in keyed mode -- unlike a genuinely
    dangling REQUIRED `inputs` key, which must still error (regression
    guard for the existing hard-error behavior)."""
    roles = [
        {"name": "role_a", "inputs": [], "outputs": ["out1"]},
        {
            "name": "role_b",
            "inputs": ["out1"],
            "optional_inputs": ["design_spec"],
            "outputs": ["out2"],
        },
        {
            "name": "role_c",
            "inputs": ["out2", "dangling_required"],
            "outputs": [],
        },
    ]
    tasks = {
        "task-a": {"role": "role_a"},
        "task-b": {"role": "role_b"},
        "task-c": {"role": "role_c"},
    }
    stages = [
        {"name": "Stage A", "task": "task-a"},
        {"name": "Stage B", "task": "task-b"},
        {"name": "Stage C", "task": "task-c"},
    ]
    _write_config(tmp_path, roles=roles, tasks=tasks, stages=stages)

    monkeypatch.setattr(config_validation.settings, "context_routing_mode", "keyed")

    problems = config_validation.validate_config(base_dir=tmp_path)

    assert not any("design_spec" in p for p in problems), (
        f"optional_inputs key must never be flagged as dangling, got: {problems}"
    )
    assert any("dangling_required" in p and "dangling input" in p for p in problems), (
        f"A genuinely dangling REQUIRED input must still error, got: {problems}"
    )


def test_optional_input_also_in_inputs_is_treated_as_optional(tmp_path: Path, monkeypatch) -> None:
    """A key listed in BOTH `inputs` and `optional_inputs` is treated as
    optional -- not produced by any earlier stage, but no dangling-input
    problem in keyed mode."""
    roles = [
        {
            "name": "role_a",
            "inputs": ["shared_key"],
            "optional_inputs": ["shared_key"],
            "outputs": [],
        },
    ]
    tasks = {"task-a": {"role": "role_a"}}
    stages = [{"name": "Stage A", "task": "task-a"}]
    _write_config(tmp_path, roles=roles, tasks=tasks, stages=stages)

    monkeypatch.setattr(config_validation.settings, "context_routing_mode", "keyed")

    problems = config_validation.validate_config(base_dir=tmp_path)

    assert not any("shared_key" in p for p in problems), (
        f"A key in both inputs and optional_inputs must not dangle, got: {problems}"
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


def test_only_tags_defined_in_a_group_produces_no_problem(tmp_path: Path) -> None:
    """A pipeline stage's only_tags value that IS defined in some group's
    tags must not be flagged."""
    _write_minimal_config(tmp_path)
    (tmp_path / "prompts" / "agents").mkdir(parents=True)
    (tmp_path / "prompts" / "agents" / "planner.md").write_text("# planner")

    (tmp_path / "tasks.yaml").write_text(
        yaml.dump({"tasks": {"build": {"description": "build it"}}})
    )
    (tmp_path / "pipelines.yaml").write_text(
        yaml.dump(
            {
                "pipelines": {
                    "default": {
                        "description": "default pipeline",
                        "stages": [{"name": "build", "task": "build", "only_tags": ["ui"]}],
                    }
                }
            }
        )
    )
    (tmp_path / "groups.yaml").write_text(
        yaml.dump({"groups": {"acme": {"tags": {"ui": ["acme-web"]}}}})
    )

    problems = config_validation.validate_config(base_dir=tmp_path)

    assert not any("only_tags" in p for p in problems), f"Unexpected problems: {problems}"


def test_only_tags_not_defined_in_any_group_is_flagged(tmp_path: Path) -> None:
    """A pipeline stage's only_tags value that is NOT defined in any group's
    tags must be reported as a static config problem (catches a typo like
    'only_tags: [uii]' at validate time instead of at run time)."""
    _write_minimal_config(tmp_path)
    (tmp_path / "prompts" / "agents").mkdir(parents=True)
    (tmp_path / "prompts" / "agents" / "planner.md").write_text("# planner")

    (tmp_path / "tasks.yaml").write_text(
        yaml.dump({"tasks": {"build": {"description": "build it"}}})
    )
    (tmp_path / "pipelines.yaml").write_text(
        yaml.dump(
            {
                "pipelines": {
                    "default": {
                        "description": "default pipeline",
                        "stages": [{"name": "build", "task": "build", "only_tags": ["nope"]}],
                    }
                }
            }
        )
    )
    (tmp_path / "groups.yaml").write_text(
        yaml.dump({"groups": {"acme": {"tags": {"ui": ["acme-web"]}}}})
    )

    problems = config_validation.validate_config(base_dir=tmp_path)

    matching = [p for p in problems if "nope" in p]
    assert matching, f"Expected a problem mentioning 'nope', got: {problems}"
    assert "default" in matching[0]
    assert "build" in matching[0]


def test_single_repo_group_components_exempt_from_projects_check(tmp_path: Path) -> None:
    """A single_repo (monorepo) group's `components` are pure scoping labels,
    never resolved as projects, so a component name absent from projects.yaml
    must NOT be flagged -- unlike a normal (multi_repo) group."""
    _write_minimal_config(tmp_path)
    (tmp_path / "prompts" / "agents").mkdir(parents=True)
    (tmp_path / "prompts" / "agents" / "planner.md").write_text("# planner")

    (tmp_path / "groups.yaml").write_text(
        yaml.dump(
            {
                "groups": {
                    "acme": {
                        "hub": "demo",
                        "single_repo": True,
                        "components": ["ui", "api"],
                        "tags": {"frontend": ["ui"]},
                    }
                }
            }
        )
    )

    problems = config_validation.validate_config(base_dir=tmp_path)

    assert not any("component" in p.lower() for p in problems), f"Unexpected problems: {problems}"


def test_single_repo_group_hub_still_required_in_projects(tmp_path: Path) -> None:
    """A single_repo group's `hub` must still resolve to a real project --
    only `components`/`tags` are exempt, not `hub`."""
    _write_minimal_config(tmp_path)
    (tmp_path / "prompts" / "agents").mkdir(parents=True)
    (tmp_path / "prompts" / "agents" / "planner.md").write_text("# planner")

    (tmp_path / "groups.yaml").write_text(
        yaml.dump(
            {
                "groups": {
                    "acme": {
                        "hub": "does-not-exist",
                        "single_repo": True,
                        "components": ["ui", "api"],
                    }
                }
            }
        )
    )

    problems = config_validation.validate_config(base_dir=tmp_path)

    matching = [p for p in problems if "does-not-exist" in p]
    assert matching, f"Expected a problem naming the missing hub, got: {problems}"


def test_multi_repo_group_components_still_validated(tmp_path: Path) -> None:
    """Regression: a normal (single_repo=False / omitted) group's components
    are still validated against projects.yaml exactly as before."""
    _write_minimal_config(tmp_path)
    (tmp_path / "prompts" / "agents").mkdir(parents=True)
    (tmp_path / "prompts" / "agents" / "planner.md").write_text("# planner")

    (tmp_path / "groups.yaml").write_text(
        yaml.dump(
            {
                "groups": {
                    "acme": {
                        "hub": "demo",
                        "components": ["not-a-real-project"],
                    }
                }
            }
        )
    )

    problems = config_validation.validate_config(base_dir=tmp_path)

    matching = [p for p in problems if "not-a-real-project" in p]
    assert matching, f"Expected a problem naming the undefined component, got: {problems}"


# ---------------------------------------------------------------------------
# Phase 21 Sprint 2 -- pipeline CVE gate: block_on_severity validation
# ---------------------------------------------------------------------------


def test_valid_block_on_severity_produces_no_problem(tmp_path: Path) -> None:
    _write_minimal_config(tmp_path)
    (tmp_path / "prompts" / "agents").mkdir(parents=True)
    (tmp_path / "prompts" / "agents" / "planner.md").write_text("# planner")

    (tmp_path / "policies.yaml").write_text(
        yaml.dump({"policies": {"default": {"block_on_severity": "critical"}}})
    )

    problems = config_validation.validate_config(base_dir=tmp_path)

    assert not any("block_on_severity" in p for p in problems), f"Unexpected: {problems}"


def test_invalid_block_on_severity_on_default_is_flagged(tmp_path: Path) -> None:
    _write_minimal_config(tmp_path)
    (tmp_path / "prompts" / "agents").mkdir(parents=True)
    (tmp_path / "prompts" / "agents" / "planner.md").write_text("# planner")

    (tmp_path / "policies.yaml").write_text(
        yaml.dump({"policies": {"default": {"block_on_severity": "super-critical"}}})
    )

    problems = config_validation.validate_config(base_dir=tmp_path)

    matching = [p for p in problems if "block_on_severity" in p and "default" in p]
    assert matching, f"Expected a problem naming the invalid severity, got: {problems}"


def test_invalid_block_on_severity_on_project_override_is_flagged(tmp_path: Path) -> None:
    _write_minimal_config(tmp_path)
    (tmp_path / "prompts" / "agents").mkdir(parents=True)
    (tmp_path / "prompts" / "agents" / "planner.md").write_text("# planner")

    (tmp_path / "policies.yaml").write_text(
        yaml.dump(
            {
                "policies": {
                    "default": {},
                    "projects": {"demo": {"block_on_severity": "nope"}},
                }
            }
        )
    )

    problems = config_validation.validate_config(base_dir=tmp_path)

    matching = [p for p in problems if "block_on_severity" in p and "demo" in p]
    assert matching, f"Expected a problem naming the invalid severity, got: {problems}"
