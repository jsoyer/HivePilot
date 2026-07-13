"""Tests for hivepilot.services.config_writer.

Covers the round-trip load/dump primitives, the validate-then-write gate in
`apply_and_validate`, the read-only `resolve_reference` membership checks,
and the TTY-aware `prompt_or_refuse` helper that Sprints 2 and 3 build on.
"""

from __future__ import annotations

import difflib
import sys
from pathlib import Path

import yaml

from hivepilot.config import Settings
from hivepilot.services import config_writer

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _write_minimal_config(base_dir: Path) -> None:
    """Same minimal-but-valid config shape used by test_config_validation.py."""
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
    (base_dir / "prompts" / "agents").mkdir(parents=True)
    (base_dir / "prompts" / "agents" / "planner.md").write_text("# planner")


class _NonTtyStdin:
    def isatty(self) -> bool:
        return False


class _TtyStdin:
    def isatty(self) -> bool:
        return True


class _FakeSelect:
    def __init__(self, value: str | None) -> None:
        self._value = value

    def ask(self) -> str | None:
        return self._value


class _FakeQuestionary:
    def __init__(self, value: str | None) -> None:
        self._value = value
        self.calls: list[tuple[str, list[str]]] = []

    def select(self, label: str, choices: list[str]) -> _FakeSelect:
        self.calls.append((label, choices))
        return _FakeSelect(self._value)


# ---------------------------------------------------------------------------
# load_roundtrip / dump_roundtrip
# ---------------------------------------------------------------------------


def test_roundtrip_preserves_comments_and_key_order_with_targeted_diff(tmp_path: Path) -> None:
    original = tmp_path / "roles.yaml"
    original.write_text(
        "# top comment\nroles:\n  - name: ceo  # inline comment\n    title: CEO\n    order: 1\n"
    )

    data = config_writer.load_roundtrip(original)
    data["roles"][0]["order"] = 2

    output = tmp_path / "roles_out.yaml"
    config_writer.dump_roundtrip(data, output)

    original_lines = original.read_text().splitlines()
    new_lines = output.read_text().splitlines()

    diff = list(difflib.unified_diff(original_lines, new_lines, lineterm=""))
    changed = [
        line for line in diff if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    assert changed == ["-    order: 1", "+    order: 2"]
    # comments and key order untouched everywhere else
    assert new_lines[0] == original_lines[0] == "# top comment"
    assert new_lines[2] == original_lines[2] == "  - name: ceo  # inline comment"


def test_load_roundtrip_missing_file_raises(tmp_path: Path) -> None:
    try:
        config_writer.load_roundtrip(tmp_path / "does-not-exist.yaml")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("expected FileNotFoundError for a missing path")


# ---------------------------------------------------------------------------
# apply_and_validate
# ---------------------------------------------------------------------------


def test_apply_and_validate_dry_run_writes_nothing_but_returns_diff(tmp_path: Path) -> None:
    _write_minimal_config(tmp_path)
    original_bytes = (tmp_path / "projects.yaml").read_bytes()

    def mutate(data):
        data["projects"]["demo2"] = {"path": "~/dev/demo2"}
        return data

    result = config_writer.apply_and_validate(
        "projects.yaml", mutate, dry_run=True, base_dir=tmp_path
    )

    assert result.errors == []
    assert result.written is False
    assert result.diff
    assert "demo2" in result.diff
    assert (tmp_path / "projects.yaml").read_bytes() == original_bytes


def test_apply_and_validate_writes_when_valid(tmp_path: Path) -> None:
    _write_minimal_config(tmp_path)

    def mutate(data):
        data["projects"]["demo2"] = {"path": "~/dev/demo2"}
        return data

    result = config_writer.apply_and_validate(
        "projects.yaml", mutate, dry_run=False, base_dir=tmp_path
    )

    assert result.errors == []
    assert result.written is True
    written_text = (tmp_path / "projects.yaml").read_text()
    assert "demo2" in written_text

    from hivepilot.services.config_validation import validate_config

    assert validate_config(base_dir=tmp_path) == []


def test_apply_and_validate_rejects_invalid_mutation_and_leaves_file_untouched(
    tmp_path: Path,
) -> None:
    _write_minimal_config(tmp_path)
    original_bytes = (tmp_path / "tasks.yaml").read_bytes()

    def mutate(data):
        data["tasks"]["broken"] = {"role": "does-not-exist"}
        return data

    result = config_writer.apply_and_validate(
        "tasks.yaml", mutate, dry_run=False, base_dir=tmp_path
    )

    assert result.written is False
    assert result.errors
    assert any("does-not-exist" in e for e in result.errors)
    assert (tmp_path / "tasks.yaml").read_bytes() == original_bytes


def test_apply_and_validate_malformed_original_file_returns_error_without_raising(
    tmp_path: Path,
) -> None:
    """A pre-existing, already-corrupt on-disk file must surface as an error
    entry (not crash the caller) — the original file is untouched."""
    _write_minimal_config(tmp_path)
    (tmp_path / "tasks.yaml").write_text("tasks:\n  - [unterminated\n")

    def mutate(data):
        return data

    result = config_writer.apply_and_validate(
        "tasks.yaml", mutate, dry_run=False, base_dir=tmp_path
    )

    assert result.written is False
    assert result.errors
    assert result.diff == ""


def test_apply_and_validate_non_core_file_rejects_invalid_mutated_content(
    tmp_path: Path, monkeypatch
) -> None:
    """Files outside the 6-file `validate_config` set (e.g. model_profiles.yaml)
    still get a parse-check gate: invalid mutated YAML must not be written."""
    _write_minimal_config(tmp_path)

    def mutate(data):
        data["default"] = "gpt"
        return data

    monkeypatch.setattr(
        config_writer,
        "_dump_roundtrip_to_string",
        lambda data: "profiles: [unterminated\n",
    )

    result = config_writer.apply_and_validate(
        "model_profiles.yaml", mutate, dry_run=False, base_dir=tmp_path
    )

    assert result.written is False
    assert result.errors
    assert not (tmp_path / "model_profiles.yaml").exists()


def test_apply_and_validate_does_not_mutate_callers_loaded_map(tmp_path: Path) -> None:
    """The immutability rule: a failed validation must not leave the in-memory
    map (or the file) mutated for the caller."""
    _write_minimal_config(tmp_path)

    captured: dict = {}

    def mutate(data):
        captured["seen_projects_before"] = dict(data["projects"])
        data["projects"]["demo2"] = {"path": "~/dev/demo2"}
        return data

    config_writer.apply_and_validate("projects.yaml", mutate, dry_run=True, base_dir=tmp_path)

    # A second, independent load must not see the mutation that only ever
    # happened dry-run (nothing was written to disk).
    reloaded = config_writer.load_roundtrip(tmp_path / "projects.yaml")
    assert "demo2" not in reloaded["projects"]
    assert captured["seen_projects_before"] == {"demo": {"path": "~/dev/demo"}}


# ---------------------------------------------------------------------------
# resolve_reference
# ---------------------------------------------------------------------------


def test_resolve_reference_role(monkeypatch) -> None:
    monkeypatch.setattr("hivepilot.roles.load_roles", lambda: {"developer": object()})
    assert config_writer.resolve_reference("role", "developer") is True
    assert config_writer.resolve_reference("role", "nope") is False


def test_resolve_reference_project(monkeypatch) -> None:
    from hivepilot.models import ProjectConfig, ProjectsFile

    fake = ProjectsFile(projects={"acme-api": ProjectConfig(path=Path("~/dev/acme-api"))})
    monkeypatch.setattr("hivepilot.services.project_service.load_projects", lambda path=None: fake)
    assert config_writer.resolve_reference("project", "acme-api") is True
    assert config_writer.resolve_reference("project", "missing") is False


def test_resolve_reference_task(monkeypatch) -> None:
    from hivepilot.models import TaskConfig, TasksFile

    fake = TasksFile(tasks={"developer": TaskConfig(description="d")})
    monkeypatch.setattr("hivepilot.services.project_service.load_tasks", lambda path=None: fake)
    assert config_writer.resolve_reference("task", "developer") is True
    assert config_writer.resolve_reference("task", "missing") is False


def test_resolve_reference_prompt_file(monkeypatch, tmp_path: Path) -> None:
    prompts_agents = tmp_path / "prompts" / "agents"
    prompts_agents.mkdir(parents=True)
    (prompts_agents / "developer.md").write_text("# dev")

    def fake_resolve_config_path(self, filename):
        return tmp_path / "prompts" if str(filename) == "prompts" else Path(filename)

    monkeypatch.setattr(Settings, "resolve_config_path", fake_resolve_config_path)

    assert config_writer.resolve_reference("prompt_file", "developer.md") is True
    assert config_writer.resolve_reference("prompt_file", "missing.md") is False


# ---------------------------------------------------------------------------
# prompt_or_refuse
# ---------------------------------------------------------------------------


def test_prompt_or_refuse_returns_none_when_not_a_tty_and_never_imports_questionary(
    monkeypatch,
) -> None:
    monkeypatch.setattr(sys, "stdin", _NonTtyStdin())
    # If the non-TTY path ever tries `import questionary`, this sabotages it
    # loudly (ImportError) instead of silently succeeding.
    monkeypatch.setitem(sys.modules, "questionary", None)

    result = config_writer.prompt_or_refuse(["a", "b"], "pick one")

    assert result is None


def test_prompt_or_refuse_returns_selection_when_tty(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stdin", _TtyStdin())
    fake_questionary = _FakeQuestionary("role-b")
    monkeypatch.setitem(sys.modules, "questionary", fake_questionary)

    result = config_writer.prompt_or_refuse(["role-a", "role-b"], "Pick a role")

    assert result == "role-b"
    assert fake_questionary.calls == [("Pick a role", ["role-a", "role-b"])]


# ---------------------------------------------------------------------------
# Module hygiene
# ---------------------------------------------------------------------------


def test_no_safe_dump_used_in_module() -> None:
    source = Path(config_writer.__file__).read_text(encoding="utf-8")
    assert "safe_dump" not in source
