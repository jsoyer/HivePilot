"""CLI tests for `project add/rm`, `task set-role`, `role wire` (Sprint 3 of
the config-edit-commands PRD).

Follows tests/test_cli.py's pattern: stub heavy optional deps in sys.modules
before importing hivepilot.cli so the suite stays lightweight. Each test
points the CLI's config-resolution chain (settings.base_dir / XDG override)
at an isolated tmp_path containing a minimal-but-valid HivePilot config, the
same technique tests/test_cli_config_get.py uses for its XDG-override case.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_STUBS = [
    "langchain",
    "langchain.text_splitter",
    "langchain_community",
    "langchain_community.embeddings",
    "langchain_community.vectorstores",
    "langchain_openai",
    "openai",
    "boto3",
    "docker",
    "telegram",
    "telegram.ext",
    "fastapi",
    "fastapi.responses",
    "fastapi.security",
    "uvicorn",
    "textual",
    "slack_bolt",
    "slack_bolt.adapter",
    "slack_bolt.adapter.fastapi",
    "slack_bolt.adapter.socket_mode",
    "discord",
    "PyNaCl",
    "nacl",
    "nacl.exceptions",
    "nacl.signing",
]

import importlib  # noqa: E402

for _mod in _STUBS:
    if _mod in sys.modules:
        continue
    try:
        importlib.import_module(_mod)
    except Exception:
        sys.modules[_mod] = MagicMock()

from typer.testing import CliRunner  # noqa: E402

from hivepilot.cli import app  # noqa: E402
from hivepilot.config import settings  # noqa: E402
from hivepilot.services.config_validation import validate_config  # noqa: E402

runner = CliRunner()


# ---------------------------------------------------------------------------
# TTY fakes (mirrors tests/test_config_writer.py's helpers)
# ---------------------------------------------------------------------------


class _TtyStdin:
    def isatty(self) -> bool:
        return True


class _FakeSysModule:
    """Stand-in for the `sys` name bound inside hivepilot.services.config_writer.

    typer's CliRunner isolates the *real* `sys.stdin` during `invoke()` (always
    non-tty), so a plain `monkeypatch.setattr(sys, "stdin", ...)` never reaches
    `prompt_or_refuse`'s `sys.stdin.isatty()` check once inside a CLI
    invocation. Rebinding the `sys` name within config_writer's own module
    namespace sidesteps that isolation entirely.
    """

    def __init__(self, stdin: object) -> None:
        self.stdin = stdin


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
# Fixture: isolated, minimal-but-valid config directory
# ---------------------------------------------------------------------------


@pytest.fixture()
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point settings.resolve_config_path's chain at tmp_path (XDG override
    pointed at an empty dir, config_repo disabled) so every CLI command under
    test reads/writes the fixture files below instead of any real config."""
    xdg_empty = tmp_path / "xdg-empty"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_empty))
    monkeypatch.setattr(settings, "base_dir", tmp_path)
    monkeypatch.setattr(settings, "config_repo", None)

    (tmp_path / "projects.yaml").write_text(
        "# projects file\nprojects:\n  acme:\n    path: ~/dev/acme\n", encoding="utf-8"
    )
    (tmp_path / "roles.yaml").write_text(
        "# HivePilot role definitions\n"
        "roles:\n"
        "  - name: developer  # primary coder\n"
        "    title: Developer\n"
        "    prompt_file: developer.md\n"
        "    model_profile: coding\n"
        "    inputs:\n"
        "      - spec\n"
        "    outputs:\n"
        "      - code\n"
        "    can_block: false\n"
        "    order: 1\n"
        "    runner: claude\n"
        "    permission_mode: bypassPermissions\n"
        "  - name: reviewer\n"
        "    title: Reviewer\n"
        "    prompt_file: reviewer.md\n"
        "    model_profile: coding\n"
        "    inputs:\n"
        "      - code\n"
        "    outputs:\n"
        "      - review\n"
        "    can_block: true\n"
        "    order: 2\n"
        "    runner: codex\n",
        encoding="utf-8",
    )
    (tmp_path / "tasks.yaml").write_text(
        "runners: {}\ntasks:\n  dev-task:\n    description: Do dev work\n    role: developer\n",
        encoding="utf-8",
    )
    (tmp_path / "policies.yaml").write_text("policies: {}\n", encoding="utf-8")
    (tmp_path / "groups.yaml").write_text("groups: {}\n", encoding="utf-8")
    (tmp_path / "pipelines.yaml").write_text(
        "pipelines:\n"
        "  demo-pipeline:\n"
        "    description: Demo pipeline\n"
        "    stages:\n"
        "      - name: Stage One\n"
        "        task: dev-task\n",
        encoding="utf-8",
    )
    (tmp_path / "model_profiles.yaml").write_text(
        "claude_profiles:\n  coding:\n    model: sonnet\n  architecture:\n    model: opus\n",
        encoding="utf-8",
    )
    prompts_agents = tmp_path / "prompts" / "agents"
    prompts_agents.mkdir(parents=True)
    (prompts_agents / "developer.md").write_text("# developer\n", encoding="utf-8")
    (prompts_agents / "reviewer.md").write_text("# reviewer\n", encoding="utf-8")

    return tmp_path


# ---------------------------------------------------------------------------
# project add / rm
# ---------------------------------------------------------------------------


class TestProjectAdd:
    def test_add_new_project_writes_and_validates(self, config_dir: Path) -> None:
        result = runner.invoke(app, ["project", "add", "newproj", "~/dev/newproj"])
        assert result.exit_code == 0, result.output
        text = (config_dir / "projects.yaml").read_text()
        assert "newproj" in text
        assert validate_config(base_dir=config_dir) == []

    def test_add_is_idempotent_on_rerun(self, config_dir: Path) -> None:
        first = runner.invoke(app, ["project", "add", "newproj", "~/dev/newproj"])
        assert first.exit_code == 0, first.output
        second = runner.invoke(app, ["project", "add", "newproj", "~/dev/newproj"])
        assert second.exit_code == 0, second.output
        assert "no changes" in second.output.lower()

    def test_add_dry_run_writes_nothing(self, config_dir: Path) -> None:
        before = (config_dir / "projects.yaml").read_bytes()
        result = runner.invoke(app, ["project", "add", "dryproj", "~/dev/dryproj", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert (config_dir / "projects.yaml").read_bytes() == before
        assert result.output  # diff was printed

    def test_add_preserves_comments_and_key_order_on_real_write(self, config_dir: Path) -> None:
        result = runner.invoke(app, ["project", "add", "newproj", "~/dev/newproj"])
        assert result.exit_code == 0, result.output
        text = (config_dir / "projects.yaml").read_text()
        assert text.startswith("# projects file\n")
        assert "acme:" in text and "newproj:" in text
        # Original entry untouched.
        assert "path: ~/dev/acme" in text

    def test_add_warns_when_rerun_would_drop_previously_set_fields(self, config_dir: Path) -> None:
        first = runner.invoke(
            app, ["project", "add", "acme", "~/dev/acme", "--description", "Acme Corp"]
        )
        assert first.exit_code == 0, first.output

        second = runner.invoke(app, ["project", "add", "acme", "~/dev/acme"])
        assert second.exit_code == 0, second.output
        assert "warning" in second.output.lower()
        assert "description" in second.output.lower()
        # Declarative replace is still by design -- the field IS cleared.
        assert "description" not in (config_dir / "projects.yaml").read_text()

    def test_add_dry_run_also_warns_about_dropped_fields(self, config_dir: Path) -> None:
        first = runner.invoke(
            app, ["project", "add", "acme", "~/dev/acme", "--description", "Acme Corp"]
        )
        assert first.exit_code == 0, first.output
        before = (config_dir / "projects.yaml").read_bytes()

        second = runner.invoke(app, ["project", "add", "acme", "~/dev/acme", "--dry-run"])
        assert second.exit_code == 0, second.output
        assert "warning" in second.output.lower()
        assert "description" in second.output.lower()
        assert (config_dir / "projects.yaml").read_bytes() == before


class TestProjectRm:
    def test_rm_missing_name_lists_valid_names(self, config_dir: Path) -> None:
        result = runner.invoke(app, ["project", "rm", "does-not-exist"])
        assert result.exit_code == 1
        assert "acme" in result.output

    def test_rm_removes_existing_then_rerun_is_missing(self, config_dir: Path) -> None:
        first = runner.invoke(app, ["project", "rm", "acme"])
        assert first.exit_code == 0, first.output
        assert "acme" not in (config_dir / "projects.yaml").read_text()

        second = runner.invoke(app, ["project", "rm", "acme"])
        assert second.exit_code == 1
        assert "does not exist" not in second.output  # sanity: real message below
        assert "unknown project" in second.output.lower()

    def test_rm_dry_run_writes_nothing(self, config_dir: Path) -> None:
        before = (config_dir / "projects.yaml").read_bytes()
        result = runner.invoke(app, ["project", "rm", "acme", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert (config_dir / "projects.yaml").read_bytes() == before


# ---------------------------------------------------------------------------
# task set-role
# ---------------------------------------------------------------------------


class TestTaskSetRole:
    def test_unknown_task_lists_valid_tasks(self, config_dir: Path) -> None:
        result = runner.invoke(app, ["task", "set-role", "no-such-task", "developer"])
        assert result.exit_code == 1
        assert "dev-task" in result.output

    def test_invalid_role_non_tty_refuses_and_lists_valid_roles(self, config_dir: Path) -> None:
        result = runner.invoke(app, ["task", "set-role", "dev-task", "ghost-role"])
        assert result.exit_code == 1
        assert "developer" in result.output
        assert "reviewer" in result.output

    def test_invalid_role_with_no_input_refuses_even_at_a_tty(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("hivepilot.services.config_writer.sys", _FakeSysModule(_TtyStdin()))
        result = runner.invoke(app, ["task", "set-role", "dev-task", "ghost-role", "--no-input"])
        assert result.exit_code == 1
        assert "developer" in result.output

    def test_valid_role_writes_and_is_idempotent(self, config_dir: Path) -> None:
        result = runner.invoke(app, ["task", "set-role", "dev-task", "reviewer"])
        assert result.exit_code == 0, result.output
        assert "role: reviewer" in (config_dir / "tasks.yaml").read_text()
        assert validate_config(base_dir=config_dir) == []

        rerun = runner.invoke(app, ["task", "set-role", "dev-task", "reviewer"])
        assert rerun.exit_code == 0, rerun.output
        assert "no changes" in rerun.output.lower()

    def test_invalid_role_at_tty_uses_interactive_picker(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("hivepilot.services.config_writer.sys", _FakeSysModule(_TtyStdin()))
        monkeypatch.setitem(sys.modules, "questionary", _FakeQuestionary("reviewer"))
        result = runner.invoke(app, ["task", "set-role", "dev-task", "ghost-role"])
        assert result.exit_code == 0, result.output
        assert "role: reviewer" in (config_dir / "tasks.yaml").read_text()

    def test_dry_run_writes_nothing(self, config_dir: Path) -> None:
        before = (config_dir / "tasks.yaml").read_bytes()
        result = runner.invoke(app, ["task", "set-role", "dev-task", "reviewer", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert (config_dir / "tasks.yaml").read_bytes() == before

    def test_task_missing_from_raw_map_exits_clean_no_traceback(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`load_tasks()` (pydantic-normalized) reports the task as present,
        but the raw round-trip map used for the actual mutation does not
        contain it (e.g. a numeric-looking task key that YAML parses as an
        int while pydantic's `dict[str, TaskConfig]` coerces to a str key).
        This must be a clean exit 1, never an uncaught KeyError."""
        import hivepilot.services.project_service as project_service_module
        from hivepilot.services.project_service import TasksFile

        real_load_tasks = project_service_module.load_tasks

        def fake_load_tasks(path=None):
            result = real_load_tasks(path)
            merged = dict(result.tasks)
            merged["ghost-task"] = next(iter(result.tasks.values()))
            return TasksFile(runners=result.runners, tasks=merged)

        monkeypatch.setattr(project_service_module, "load_tasks", fake_load_tasks)
        # cli.py imports `load_tasks` lazily inside the function, from this
        # same module -- patching the module attribute is enough.

        before = (config_dir / "tasks.yaml").read_bytes()
        result = runner.invoke(app, ["task", "set-role", "ghost-task", "reviewer"])
        assert result.exit_code == 1
        assert "Traceback" not in result.output
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "ghost-task" in result.output
        assert "dev-task" in result.output  # lists a valid (raw) task
        assert (config_dir / "tasks.yaml").read_bytes() == before


# ---------------------------------------------------------------------------
# role wire
# ---------------------------------------------------------------------------


class TestRoleWire:
    def test_unknown_role_lists_valid_roles(self, config_dir: Path) -> None:
        result = runner.invoke(app, ["role", "wire", "ghost", "order", "5"])
        assert result.exit_code == 1
        assert "developer" in result.output

    def test_unknown_field_lists_valid_fields(self, config_dir: Path) -> None:
        result = runner.invoke(app, ["role", "wire", "developer", "not_a_field", "x"])
        assert result.exit_code == 1
        assert "order" in result.output  # a real Role field is listed as valid

    def test_int_field_coercion_success_and_idempotent(self, config_dir: Path) -> None:
        result = runner.invoke(app, ["role", "wire", "developer", "order", "5"])
        assert result.exit_code == 0, result.output
        assert "order: 5" in (config_dir / "roles.yaml").read_text()
        assert validate_config(base_dir=config_dir) == []

        rerun = runner.invoke(app, ["role", "wire", "developer", "order", "5"])
        assert rerun.exit_code == 0, rerun.output
        assert "no changes" in rerun.output.lower()

    def test_int_field_bad_value_rejected(self, config_dir: Path) -> None:
        before = (config_dir / "roles.yaml").read_bytes()
        result = runner.invoke(app, ["role", "wire", "developer", "order", "not-a-number"])
        assert result.exit_code == 1
        assert (config_dir / "roles.yaml").read_bytes() == before

    @pytest.mark.parametrize("raw_value", ["true", "1", "yes"])
    def test_bool_field_truthy_coercion(self, config_dir: Path, raw_value: str) -> None:
        result = runner.invoke(app, ["role", "wire", "developer", "can_block", raw_value])
        assert result.exit_code == 0, result.output
        assert "can_block: true" in (config_dir / "roles.yaml").read_text()

    @pytest.mark.parametrize("raw_value", ["false", "0", "no"])
    def test_bool_field_falsy_coercion(self, config_dir: Path, raw_value: str) -> None:
        result = runner.invoke(app, ["role", "wire", "reviewer", "can_block", raw_value])
        assert result.exit_code == 0, result.output
        assert "can_block: false" in (config_dir / "roles.yaml").read_text()

    def test_bool_field_bad_value_rejected(self, config_dir: Path) -> None:
        result = runner.invoke(app, ["role", "wire", "developer", "can_block", "maybe"])
        assert result.exit_code == 1

    def test_list_field_comma_split_coercion(self, config_dir: Path) -> None:
        result = runner.invoke(
            app, ["role", "wire", "developer", "inputs", "spec,architecture_docs,codebase"]
        )
        assert result.exit_code == 0, result.output
        text = (config_dir / "roles.yaml").read_text()
        assert "architecture_docs" in text and "codebase" in text
        assert validate_config(base_dir=config_dir) == []

    def test_permission_mode_valid_value(self, config_dir: Path) -> None:
        result = runner.invoke(app, ["role", "wire", "reviewer", "permission_mode", "acceptEdits"])
        assert result.exit_code == 0, result.output
        assert "permission_mode: acceptEdits" in (config_dir / "roles.yaml").read_text()

    def test_permission_mode_invalid_enum_rejected(self, config_dir: Path) -> None:
        before = (config_dir / "roles.yaml").read_bytes()
        result = runner.invoke(app, ["role", "wire", "developer", "permission_mode", "sudo-mode"])
        assert result.exit_code == 1
        assert (config_dir / "roles.yaml").read_bytes() == before

    def test_prompt_file_valid_reference(self, config_dir: Path) -> None:
        result = runner.invoke(app, ["role", "wire", "reviewer", "prompt_file", "developer.md"])
        assert result.exit_code == 0, result.output
        assert validate_config(base_dir=config_dir) == []

    def test_prompt_file_dangling_reference_rejected(self, config_dir: Path) -> None:
        before = (config_dir / "roles.yaml").read_bytes()
        result = runner.invoke(
            app, ["role", "wire", "developer", "prompt_file", "does-not-exist.md"]
        )
        assert result.exit_code == 1
        assert (config_dir / "roles.yaml").read_bytes() == before

    def test_runner_valid_kind(self, config_dir: Path) -> None:
        # Sprint 2 (runner-defaults-plugins-mode PRD): "opencode" moved from
        # an unconditional built-in into a PATH-gated plugin, and
        # `config_dir` (this fixture) points settings.base_dir at a bare
        # tmp_path with no plugins/ directory — so the real PluginManager
        # scan this command runs (see cli.py's role_wire "runner" field
        # validation) would never see it regardless of what's on the host's
        # PATH. Use "codex" instead: a kind that stays unconditionally
        # built-in, so this test asserts what it actually means to (a valid
        # kind is accepted) without depending on plugin/environment specifics.
        result = runner.invoke(app, ["role", "wire", "developer", "runner", "codex"])
        assert result.exit_code == 0, result.output
        assert "runner: codex" in (config_dir / "roles.yaml").read_text()

    def test_runner_invalid_kind_rejected(self, config_dir: Path) -> None:
        result = runner.invoke(app, ["role", "wire", "developer", "runner", "not-a-runner"])
        assert result.exit_code == 1

    def test_runner_orphan_api_kind_rejected(self, config_dir: Path) -> None:
        """Roadmap Phase 26a: `"api"` is advertised nowhere but was
        previously accepted by `role wire` (validated against
        KNOWN_RUNNER_KINDS, not the live registry). It must now be
        rejected, same as any other unregistered kind."""
        before = (config_dir / "roles.yaml").read_bytes()
        result = runner.invoke(app, ["role", "wire", "developer", "runner", "api"])
        assert result.exit_code == 1
        assert (config_dir / "roles.yaml").read_bytes() == before

    def test_runner_plugin_registered_kind_accepted(self, config_dir: Path) -> None:
        """Behavior-true regression test: `role wire` must discover plugins
        itself (via a real `PluginManager()` construction) BEFORE validating
        `runner` — a kind contributed by a genuine, on-disk local-file
        plugin (discovered through the real `plugins/` directory scan, not a
        manual `RunnerRegistry.register()` shortcut) must be accepted,
        exactly as a fresh `hivepilot role wire` CLI invocation would see
        it. `config_dir` already points `settings.base_dir` at this
        tmp_path, so writing a real plugin file under `plugins/` here is
        genuine on-disk discovery, not a pre-registration shortcut."""
        from hivepilot.registry import RUNNER_MAP

        plugin_dir = config_dir / "plugins"
        plugin_dir.mkdir()
        (plugin_dir / "role_wire_fixture.py").write_text(
            """
class RoleWireFixtureRunner:
    def __init__(self, definition, settings):
        pass

    def run(self, payload):
        return None


def register():
    return {"runners": {"dummy-plugin-runner": RoleWireFixtureRunner}}
""",
            encoding="utf-8",
        )

        result = runner.invoke(app, ["role", "wire", "developer", "runner", "dummy-plugin-runner"])

        assert result.exit_code == 0, result.output
        assert "runner: dummy-plugin-runner" in (config_dir / "roles.yaml").read_text()
        # Sanity: the plugin was genuinely discovered (not pre-registered by the test).
        assert "dummy-plugin-runner" in RUNNER_MAP

    def test_model_profile_valid_value(self, config_dir: Path) -> None:
        result = runner.invoke(app, ["role", "wire", "developer", "model_profile", "architecture"])
        assert result.exit_code == 0, result.output

    def test_model_profile_invalid_rejected(self, config_dir: Path) -> None:
        result = runner.invoke(app, ["role", "wire", "developer", "model_profile", "not-a-profile"])
        assert result.exit_code == 1

    def test_dry_run_writes_nothing(self, config_dir: Path) -> None:
        before = (config_dir / "roles.yaml").read_bytes()
        result = runner.invoke(app, ["role", "wire", "developer", "order", "9", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert (config_dir / "roles.yaml").read_bytes() == before

    def test_comment_and_key_order_preserved_on_real_write(self, config_dir: Path) -> None:
        result = runner.invoke(app, ["role", "wire", "developer", "order", "3"])
        assert result.exit_code == 0, result.output
        text = (config_dir / "roles.yaml").read_text()
        assert text.startswith("# HivePilot role definitions\n")
        assert "- name: developer  # primary coder" in text

    def test_models_field_comma_split_coercion(self, config_dir: Path) -> None:
        result = runner.invoke(app, ["role", "wire", "developer", "models", "sonnet,opus"])
        assert result.exit_code == 0, result.output
        text = (config_dir / "roles.yaml").read_text()
        assert "sonnet" in text and "opus" in text
        assert validate_config(base_dir=config_dir) == []

    def test_role_valid_per_load_roles_but_absent_from_raw_map_errors(
        self, config_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`load_roles()` reports the role as valid (e.g. it was merged in
        from somewhere `_load_raw_config_file` doesn't see), but no entry in
        the raw roles.yaml list matches its name. `mutate()`'s for/break loop
        then silently no-ops -- this must surface as an explicit error, never
        a false "success" with nothing actually changed."""
        import hivepilot.roles as roles_module

        real_load_roles = roles_module.load_roles

        def fake_load_roles():
            roles = dict(real_load_roles())
            ghost = next(iter(roles.values())).model_copy(update={"name": "ghost"})
            roles["ghost"] = ghost
            return roles

        monkeypatch.setattr(roles_module, "load_roles", fake_load_roles)
        # cli.py's role_wire imports `load_roles` lazily from this module.

        before = (config_dir / "roles.yaml").read_bytes()
        result = runner.invoke(app, ["role", "wire", "ghost", "order", "5"])
        assert result.exit_code == 1
        assert "ghost" in result.output
        assert "developer" in result.output  # lists a valid (raw) role
        assert (config_dir / "roles.yaml").read_bytes() == before


# ---------------------------------------------------------------------------
# stage attach-skill / detach-skill (Sprint 5 of skill-plugin-type PRD)
# ---------------------------------------------------------------------------


def _register_skill_plugin(config_dir: Path, name: str = "known-skill") -> None:
    """Register a single skill via a real local-file plugin under
    config_dir/plugins/ -- the same directory `settings.base_dir` (already
    pointed at config_dir by the `config_dir` fixture) is scanned from,
    mirroring TestRoleWire.test_runner_plugin_registered_kind_accepted's
    genuine on-disk plugin discovery pattern."""
    pdir = config_dir / "plugins"
    pdir.mkdir(exist_ok=True)
    stem = name.replace("-", "_")
    (pdir / f"{stem}_skill.py").write_text(
        "def register():\n"
        f"    return {{'skills': [{{'name': {name!r}, 'description': 'D', "
        f"'provider': 'acme', 'files': {{'SKILL.md': 'hello'}}}}]}}\n",
        encoding="utf-8",
    )


class TestStageAttachDetachSkill:
    def test_attach_unknown_pipeline_lists_valid_pipelines(self, config_dir: Path) -> None:
        result = runner.invoke(
            app, ["stage", "attach-skill", "ghost-pipeline", "Stage One", "known-skill"]
        )
        assert result.exit_code == 1
        assert "demo-pipeline" in result.output

    def test_attach_unknown_stage_lists_valid_stages(self, config_dir: Path) -> None:
        result = runner.invoke(
            app, ["stage", "attach-skill", "demo-pipeline", "ghost-stage", "known-skill"]
        )
        assert result.exit_code == 1
        assert "Stage One" in result.output

    def test_attach_unknown_skill_is_hard_error_and_writes_nothing(self, config_dir: Path) -> None:
        """Fail-closed: an unregistered skill name refuses the write. This
        reuses (never reimplements) Sprint 3's
        `config_validation.validate_config` cross-reference check -- run by
        `apply_and_validate` against the prospective pipelines.yaml before
        anything is persisted."""
        before = (config_dir / "pipelines.yaml").read_bytes()
        result = runner.invoke(
            app, ["stage", "attach-skill", "demo-pipeline", "Stage One", "does-not-exist"]
        )
        assert result.exit_code == 1
        assert "unknown skill" in result.output.lower()
        assert "does-not-exist" in result.output
        assert (config_dir / "pipelines.yaml").read_bytes() == before

    def test_attach_known_skill_writes_and_is_idempotent(self, config_dir: Path) -> None:
        _register_skill_plugin(config_dir)

        result = runner.invoke(
            app, ["stage", "attach-skill", "demo-pipeline", "Stage One", "known-skill"]
        )
        assert result.exit_code == 0, result.output
        text = (config_dir / "pipelines.yaml").read_text()
        assert "known-skill" in text
        assert validate_config(base_dir=config_dir) == []

        rerun = runner.invoke(
            app, ["stage", "attach-skill", "demo-pipeline", "Stage One", "known-skill"]
        )
        assert rerun.exit_code == 0, rerun.output
        assert "no changes" in rerun.output.lower()

    def test_attach_dry_run_writes_nothing(self, config_dir: Path) -> None:
        _register_skill_plugin(config_dir)
        before = (config_dir / "pipelines.yaml").read_bytes()

        result = runner.invoke(
            app,
            ["stage", "attach-skill", "demo-pipeline", "Stage One", "known-skill", "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        assert (config_dir / "pipelines.yaml").read_bytes() == before

    def test_detach_removes_skill_and_round_trips_clean(self, config_dir: Path) -> None:
        _register_skill_plugin(config_dir)
        attach = runner.invoke(
            app, ["stage", "attach-skill", "demo-pipeline", "Stage One", "known-skill"]
        )
        assert attach.exit_code == 0, attach.output

        detach = runner.invoke(
            app, ["stage", "detach-skill", "demo-pipeline", "Stage One", "known-skill"]
        )
        assert detach.exit_code == 0, detach.output
        text = (config_dir / "pipelines.yaml").read_text()
        assert "known-skill" not in text
        assert "skills:" not in text  # empty list dropped, not left as `skills: []`
        assert validate_config(base_dir=config_dir) == []

    def test_detach_already_absent_is_noop(self, config_dir: Path) -> None:
        result = runner.invoke(
            app, ["stage", "detach-skill", "demo-pipeline", "Stage One", "ghost-skill"]
        )
        assert result.exit_code == 0, result.output
        assert "no changes" in result.output.lower()

    def test_detach_dry_run_writes_nothing(self, config_dir: Path) -> None:
        _register_skill_plugin(config_dir)
        attach = runner.invoke(
            app, ["stage", "attach-skill", "demo-pipeline", "Stage One", "known-skill"]
        )
        assert attach.exit_code == 0, attach.output
        before = (config_dir / "pipelines.yaml").read_bytes()

        result = runner.invoke(
            app,
            ["stage", "detach-skill", "demo-pipeline", "Stage One", "known-skill", "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        assert (config_dir / "pipelines.yaml").read_bytes() == before

    def test_detach_unknown_pipeline_lists_valid_pipelines(self, config_dir: Path) -> None:
        result = runner.invoke(
            app, ["stage", "detach-skill", "ghost-pipeline", "Stage One", "known-skill"]
        )
        assert result.exit_code == 1
        assert "demo-pipeline" in result.output


# ---------------------------------------------------------------------------
# Module hygiene: never bypass the round-trip writer
# ---------------------------------------------------------------------------


def test_no_safe_dump_used_in_cli_module() -> None:
    import hivepilot.cli as cli_module

    source = Path(cli_module.__file__).read_text(encoding="utf-8")
    assert "safe_dump" not in source


def test_sprint3_config_commands_never_bypass_the_writer() -> None:
    """Extra hardening: the project/task/role edit command bodies (Sprint 3)
    must never call yaml.dump(...) directly, nor open a config path in write
    mode -- every mutation must flow through
    hivepilot.services.config_writer.apply_and_validate."""
    import hivepilot.cli as cli_module

    source = Path(cli_module.__file__).read_text(encoding="utf-8")
    start = source.index("Guided config mutations (Sprint 3")
    end = source.index('@telegram_app.command("start")')
    region = source[start:end]
    assert "yaml.dump(" not in region
    assert "open(" not in region
