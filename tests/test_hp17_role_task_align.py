"""HP-17 — company-pipeline tasks must match roles.yaml runner kinds.

`roles.yaml` is authoritative for a role-bound step. Stale `runner: claude`
/ `runner_ref: claude-*` overlays mislead operators and can stamp Claude
profile options onto a cursor/opencode/codex step.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from hivepilot.services import config_validation
from hivepilot.services.roster_preset import AGENT_KINDS

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_yaml(name: str) -> dict:
    return yaml.safe_load((REPO_ROOT / name).read_text(encoding="utf-8"))


def test_bundled_company_tasks_match_role_runners() -> None:
    roles = {
        entry["name"]: entry["runner"]
        for entry in _load_yaml("roles.yaml")["roles"]
        if isinstance(entry, dict) and entry.get("name") and entry.get("runner")
    }
    tasks = _load_yaml("tasks.yaml").get("tasks") or {}
    named_kinds = {
        name: defn.get("kind")
        for name, defn in (_load_yaml("tasks.yaml").get("runners") or {}).items()
        if isinstance(defn, dict)
    }

    mismatches: list[str] = []
    for task_name, task in tasks.items():
        if not isinstance(task, dict) or not task.get("role"):
            continue
        role_runner = roles.get(task["role"])
        assert role_runner, f"task '{task_name}' role '{task['role']}' has no runner in roles.yaml"
        for step in task.get("steps") or []:
            if not isinstance(step, dict):
                continue
            runner = step.get("runner")
            if runner in AGENT_KINDS and runner != role_runner:
                mismatches.append(
                    f"{task_name}/{step.get('name')}: runner {runner!r} != role {role_runner!r}"
                )
            runner_ref = step.get("runner_ref")
            ref_kind = named_kinds.get(runner_ref) if runner_ref else None
            if ref_kind in AGENT_KINDS and ref_kind != role_runner:
                mismatches.append(
                    f"{task_name}/{step.get('name')}: runner_ref {runner_ref!r} "
                    f"kind {ref_kind!r} != role {role_runner!r}"
                )
            if step.get("metadata", {}).get("claude_profile") and role_runner != "claude":
                mismatches.append(
                    f"{task_name}/{step.get('name')}: leftover metadata.claude_profile "
                    f"on non-claude role {task['role']!r}"
                )
    assert mismatches == [], "company tasks drifted from roles.yaml:\n" + "\n".join(mismatches)


def test_validate_config_flags_role_runner_drift(tmp_path: Path) -> None:
    (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {"demo": {"path": "~/dev/demo"}}}))
    (tmp_path / "roles.yaml").write_text(
        yaml.dump(
            {
                "roles": [
                    {
                        "name": "planner",
                        "runner": "cursor",
                        "prompt_file": "planner.md",
                    }
                ]
            }
        )
    )
    (tmp_path / "policies.yaml").write_text(yaml.dump({"policies": {}}))
    (tmp_path / "groups.yaml").write_text(yaml.dump({"groups": {}}))
    (tmp_path / "pipelines.yaml").write_text(yaml.dump({"pipelines": {}}))
    (tmp_path / "tasks.yaml").write_text(
        yaml.dump(
            {
                "runners": {"claude-docs": {"kind": "claude", "command": "claude"}},
                "tasks": {
                    "plan": {
                        "role": "planner",
                        "steps": [
                            {
                                "name": "plan",
                                "runner": "claude",
                                "runner_ref": "claude-docs",
                                "prompt_file": "prompts/plan.md",
                            }
                        ],
                    }
                },
            }
        )
    )
    (tmp_path / "prompts" / "agents").mkdir(parents=True)
    (tmp_path / "prompts" / "agents" / "planner.md").write_text("# planner")
    (tmp_path / "prompts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "prompts" / "plan.md").write_text("# plan")

    problems = config_validation.validate_config(base_dir=tmp_path)
    drift = [p for p in problems if "roles.yaml is authoritative" in p]
    assert len(drift) >= 2, f"expected runner + runner_ref drift, got: {problems}"
    assert any("runner 'claude'" in p for p in drift)
    assert any("runner_ref 'claude-docs'" in p for p in drift)


def test_validate_config_accepts_matching_role_runner(tmp_path: Path) -> None:
    (tmp_path / "projects.yaml").write_text(yaml.dump({"projects": {"demo": {"path": "~/dev/demo"}}}))
    (tmp_path / "roles.yaml").write_text(
        yaml.dump({"roles": [{"name": "planner", "runner": "cursor", "prompt_file": "planner.md"}]})
    )
    (tmp_path / "policies.yaml").write_text(yaml.dump({"policies": {}}))
    (tmp_path / "groups.yaml").write_text(yaml.dump({"groups": {}}))
    (tmp_path / "pipelines.yaml").write_text(yaml.dump({"pipelines": {}}))
    (tmp_path / "tasks.yaml").write_text(
        yaml.dump(
            {
                "tasks": {
                    "plan": {
                        "role": "planner",
                        "steps": [
                            {
                                "name": "plan",
                                "runner": "cursor",
                                "prompt_file": "prompts/plan.md",
                            }
                        ],
                    }
                }
            }
        )
    )
    (tmp_path / "prompts" / "agents").mkdir(parents=True)
    (tmp_path / "prompts" / "agents" / "planner.md").write_text("# planner")
    (tmp_path / "prompts" / "plan.md").write_text("# plan")

    problems = config_validation.validate_config(base_dir=tmp_path)
    assert not any("roles.yaml is authoritative" in p for p in problems), problems
