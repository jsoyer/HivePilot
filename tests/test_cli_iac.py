"""
Tests for `hivepilot iac` closing the orchestrator-bypass gap (Part B-cli):

`hivepilot iac apply/destroy/...` used to call `runner.run(payload)` directly,
bypassing the orchestrator entirely — no destructive-op approval gate, no
audit log, and no `${secret:}` resolution. These tests cover the fix:

1. Secret resolution: a `${secret:NAME}` reference in `project.env` must be
   resolved into `payload.secrets` (same mechanism as
   `Orchestrator._resolve_secrets`) before the runner ever executes, and the
   resolved value must never leak into CLI stdout.
2. Destructive-op gate: for any operation the runner's `is_destructive(payload)`
   reports True (apply/destroy), the CLI must require an explicit interactive
   confirmation OR `--yes` before invoking the runner. Declining aborts with a
   non-zero exit code and the runner is NOT invoked.
3. Audit log: a confirmed destructive operation (via prompt or `--yes`) is
   always recorded via `state_service.record_interaction`.
4. Non-destructive ops (plan/output/drift/cost) run without any confirmation
   prompt, but still resolve secrets.
5. Runner definition resolution: a named runner entry in `tasks.yaml`'s
   top-level `runners:` block (carrying `options`/`env`) is resolved through
   the same canonical registry resolver `Orchestrator` uses, so the project's
   real IaC options (`workspace`/`var_file`/etc.) reach the runner instead of
   being silently dropped by a hand-built empty `RunnerDefinition`. With no
   matching named runner, behavior falls back byte-identically to the
   pre-fix empty-default synthesis.

The runner class is mocked (via `hivepilot.registry.resolve_runner_class`) so
no real terraform/opentofu/pulumi binary is ever invoked.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub out optional heavy dependencies before importing hivepilot.cli
# (mirrors tests/test_cli.py so this file can run standalone).
# ---------------------------------------------------------------------------

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

import hivepilot.cli as cli_module  # noqa: E402
import hivepilot.orchestrator as orchestrator_module  # noqa: E402
from hivepilot.cli import app  # noqa: E402
from hivepilot.models import ProjectConfig, ProjectsFile, RunnerDefinition, TasksFile  # noqa: E402
from hivepilot.services import state_service  # noqa: E402

SECRET_ENV_VAR = "HIVEPILOT_TEST_CLOUD_TOKEN"
SECRET_VALUE = "super-secret-cloud-token-value"  # noqa: S105 (test fixture value)


@pytest.fixture()
def project_with_secret(tmp_path: Path) -> ProjectConfig:
    """A project whose `env` references a `${secret:}` catalog entry backed
    by an environment variable — the simplest end-to-end secret path."""
    return ProjectConfig(
        path=tmp_path,
        env={"TF_VAR_token": "${secret:CLOUD_TOKEN}"},
        secrets={"CLOUD_TOKEN": {"source": "env", "key": SECRET_ENV_VAR}},
    )


@pytest.fixture()
def fake_runner(monkeypatch: pytest.MonkeyPatch):
    """Patch `hivepilot.registry.resolve_runner_class` to return a MagicMock
    runner class whose instance records `run()` calls and answers
    `is_destructive()` per test. Returns the mock runner instance."""
    instance = MagicMock(name="runner_instance")
    runner_cls = MagicMock(name="runner_cls", return_value=instance)
    monkeypatch.setattr("hivepilot.registry.resolve_runner_class", lambda kind: runner_cls)
    return instance


@pytest.fixture()
def fake_runner_cls(monkeypatch: pytest.MonkeyPatch):
    """Like `fake_runner`, but also exposes the mocked runner *class* itself
    so a test can inspect the `definition=` kwarg it was constructed with."""
    instance = MagicMock(name="runner_instance")
    runner_cls = MagicMock(name="runner_cls", return_value=instance)
    monkeypatch.setattr("hivepilot.registry.resolve_runner_class", lambda kind: runner_cls)
    return runner_cls, instance


@pytest.fixture(autouse=True)
def patch_projects(monkeypatch: pytest.MonkeyPatch, project_with_secret: ProjectConfig) -> None:
    projects = ProjectsFile(projects={"proj": project_with_secret})
    monkeypatch.setattr(cli_module, "load_projects", lambda: projects)


@pytest.fixture(autouse=True)
def _default_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every fixture project references `${secret:CLOUD_TOKEN}` -> resolve it
    by default so tests unrelated to secret resolution don't have to care.
    The dedicated TestSecretResolution tests re-set this explicitly too."""
    monkeypatch.setenv(SECRET_ENV_VAR, SECRET_VALUE)


class TestDestructiveGate:
    def test_apply_without_yes_and_declined_aborts_and_does_not_invoke_runner(
        self, fake_runner: MagicMock
    ) -> None:
        fake_runner.is_destructive.return_value = True
        runner = CliRunner()
        result = runner.invoke(app, ["iac", "apply", "--project", "proj"], input="n\n")
        assert result.exit_code != 0
        fake_runner.run.assert_not_called()

    def test_destroy_without_yes_and_declined_aborts_and_does_not_invoke_runner(
        self, fake_runner: MagicMock
    ) -> None:
        fake_runner.is_destructive.return_value = True
        runner = CliRunner()
        result = runner.invoke(app, ["iac", "destroy", "--project", "proj"], input="n\n")
        assert result.exit_code != 0
        fake_runner.run.assert_not_called()

    def test_apply_with_yes_invokes_runner_and_records_audit(
        self, fake_runner: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_runner.is_destructive.return_value = True
        recorded: list[dict] = []

        def _fake_record_interaction(**kwargs):
            recorded.append(kwargs)
            return 1

        monkeypatch.setattr(state_service, "record_interaction", _fake_record_interaction)

        runner = CliRunner()
        result = runner.invoke(app, ["iac", "apply", "--project", "proj", "--yes"])
        assert result.exit_code == 0, result.output
        fake_runner.run.assert_called_once()
        assert len(recorded) == 1
        entry = recorded[0]
        assert entry["target"] == "proj"
        assert "apply" in entry["action"]
        assert entry.get("metadata", {}).get("confirmed_via") == "--yes"
        assert SECRET_VALUE not in str(entry)

    def test_apply_confirmed_interactively_invokes_runner_and_records_audit(
        self, fake_runner: MagicMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake_runner.is_destructive.return_value = True
        recorded: list[dict] = []

        def _fake_record_interaction(**kwargs):
            recorded.append(kwargs)
            return 1

        monkeypatch.setattr(state_service, "record_interaction", _fake_record_interaction)

        runner = CliRunner()
        result = runner.invoke(app, ["iac", "destroy", "--project", "proj"], input="y\n")
        assert result.exit_code == 0, result.output
        fake_runner.run.assert_called_once()
        assert len(recorded) == 1
        assert recorded[0].get("metadata", {}).get("confirmed_via") == "interactive prompt"


class TestNonDestructiveOps:
    @pytest.mark.parametrize("op,args", [("plan", ["plan"]), ("output", ["output"])])
    def test_non_destructive_op_runs_without_confirmation(
        self, fake_runner: MagicMock, op: str, args: list[str]
    ) -> None:
        fake_runner.is_destructive.return_value = False
        runner = CliRunner()
        result = runner.invoke(app, ["iac", *args, "--project", "proj"])
        assert result.exit_code == 0, result.output
        fake_runner.run.assert_called_once()


class TestSecretResolution:
    def test_secret_ref_resolved_into_payload_secrets_and_never_printed(
        self,
        fake_runner: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(SECRET_ENV_VAR, SECRET_VALUE)
        fake_runner.is_destructive.return_value = False

        cli_runner = CliRunner()
        result = cli_runner.invoke(app, ["iac", "plan", "--project", "proj"])

        assert result.exit_code == 0, result.output
        fake_runner.run.assert_called_once()
        payload = fake_runner.run.call_args.args[0]
        assert payload.secrets.get("TF_VAR_token") == SECRET_VALUE
        assert SECRET_VALUE not in result.output

    def test_apply_gets_resolved_secret_in_payload(
        self,
        fake_runner: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv(SECRET_ENV_VAR, SECRET_VALUE)
        fake_runner.is_destructive.return_value = True

        cli_runner = CliRunner()
        result = cli_runner.invoke(app, ["iac", "apply", "--project", "proj", "--yes"])

        assert result.exit_code == 0, result.output
        payload = fake_runner.run.call_args.args[0]
        assert payload.secrets.get("TF_VAR_token") == SECRET_VALUE
        assert SECRET_VALUE not in result.output


class TestNamedRunnerDefinitionResolution:
    """`_run_iac_operation` used to synthesize an EMPTY `RunnerDefinition`
    for every `iac plan/apply/destroy/drift/output/cost` call, silently
    ignoring a project's real `options`/`env` declared under a named entry in
    `tasks.yaml`'s top-level `runners:` block. These tests cover the fix:
    resolution now goes through `RunnerRegistry._definition_for` (the same
    resolver `Orchestrator` uses), with a byte-identical fallback to the old
    empty default when no matching named runner exists."""

    def test_named_runner_options_reach_the_runner_definition(
        self,
        fake_runner_cls: tuple[MagicMock, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner_cls, instance = fake_runner_cls
        instance.is_destructive.return_value = False

        named_definition = RunnerDefinition(
            kind="opentofu",
            options={"workspace": "prod", "var_file": "prod.tfvars"},
            env={"TF_LOG": "INFO"},
        )
        fake_tasks = TasksFile(tasks={}, runners={"opentofu": named_definition})
        monkeypatch.setattr(orchestrator_module, "load_tasks", lambda *a, **k: fake_tasks)

        cli_runner = CliRunner()
        result = cli_runner.invoke(
            app, ["iac", "plan", "--project", "proj", "--runner", "opentofu"]
        )

        assert result.exit_code == 0, result.output
        runner_cls.assert_called_once()
        passed_definition = runner_cls.call_args.kwargs["definition"]
        assert passed_definition.options == {"workspace": "prod", "var_file": "prod.tfvars"}
        assert passed_definition.env == {"TF_LOG": "INFO"}
        # `command` is always overwritten to the resolved operation; options/
        # env from the named definition carry through untouched.
        assert passed_definition.command == "plan"

    def test_no_named_runner_falls_back_to_empty_default_definition(
        self,
        fake_runner_cls: tuple[MagicMock, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        runner_cls, instance = fake_runner_cls
        instance.is_destructive.return_value = False

        # No "opentofu" entry in `runners:` -> falls back to the pre-fix
        # synthesized empty RunnerDefinition (no options/env), matching
        # today's exact default behavior for a project with no named runner.
        fake_tasks = TasksFile(tasks={}, runners={})
        monkeypatch.setattr(orchestrator_module, "load_tasks", lambda *a, **k: fake_tasks)

        cli_runner = CliRunner()
        result = cli_runner.invoke(
            app, ["iac", "plan", "--project", "proj", "--runner", "opentofu"]
        )

        assert result.exit_code == 0, result.output
        runner_cls.assert_called_once()
        passed_definition = runner_cls.call_args.kwargs["definition"]
        assert passed_definition.options == {}
        assert passed_definition.env == {}
        assert passed_definition.command == "plan"


class TestMissingTasksYamlDoesNotCrash:
    """Regression test: `_resolve_iac_runner_definition` constructs a real
    `Orchestrator()`, whose `_load()` calls `load_tasks()` -- and
    `TasksFile.tasks` is a REQUIRED pydantic field, so a project directory
    with a `projects.yaml` but NO `tasks.yaml` on disk (a config shape that
    worked fine for `iac`/`drift` before this resolver was added, since
    those commands previously only ever read `projects.yaml`) raises
    `pydantic.ValidationError` from inside `Orchestrator()` construction.
    This must degrade to the same empty-default `RunnerDefinition`, never an
    uncaught traceback. Deliberately does NOT monkeypatch `load_tasks` (the
    other tests in this file do) so it exercises the REAL missing-file load
    path through `hivepilot.services.project_service.load_tasks`.
    """

    def test_iac_plan_with_no_tasks_yaml_on_disk_falls_back_and_exits_zero(
        self,
        fake_runner_cls: tuple[MagicMock, MagicMock],
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        runner_cls, instance = fake_runner_cls
        instance.is_destructive.return_value = False

        # A real projects.yaml exists at this base_dir, but tasks.yaml does
        # NOT -- resolve_config_path() falls through to base_dir/tasks.yaml,
        # _read_yaml() sees no file and returns {}, and
        # TasksFile.model_validate({}) raises ValidationError (tasks: is
        # required) from inside Orchestrator()'s _load().
        (tmp_path / "projects.yaml").write_text(
            f"projects:\n  proj:\n    path: {tmp_path}\n", encoding="utf-8"
        )
        assert not (tmp_path / "tasks.yaml").exists()

        from hivepilot.config import settings

        monkeypatch.setattr(settings, "base_dir", tmp_path, raising=False)
        monkeypatch.setattr(settings, "config_repo", None, raising=False)

        cli_runner = CliRunner()
        result = cli_runner.invoke(app, ["iac", "plan", "--project", "proj"])

        assert result.exit_code == 0, result.output
        runner_cls.assert_called_once()
        passed_definition = runner_cls.call_args.kwargs["definition"]
        assert passed_definition.options == {}
        assert passed_definition.env == {}
        assert passed_definition.command == "plan"
