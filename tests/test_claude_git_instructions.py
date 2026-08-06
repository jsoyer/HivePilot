"""Drop Claude Code's git instructions for roles that never touch git.

`CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS=1` removes roughly 1 800 tokens of
system prompt teaching the agent to stage, commit and push. HivePilot does
its own git — `perform_git_actions` promotes and merges, and no role in the
reference config is granted a `git` command in `allowed_tools`. For a
reviewer handed a diff and told not to explore, those tokens buy nothing and
are paid on every single call.

**Per-role, not global.** The obvious version is one setting, default on.
That would have been wrong here: two prompts in the noxys config
(`refactor.md`, `docs_rewrite.md`) do instruct their agent to commit, so a
global switch would quietly strip the instructions out from under them. It
resolves like `permission_mode` and `allowed_tools` — step metadata over
runner definition — so the expensive reviewers can drop it while the roles
that genuinely commit keep it.

Absent by default: a deployment that sets nothing gets a byte-identical
invocation, as before this existed.
"""

from __future__ import annotations

from pathlib import Path

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.claude_runner import ClaudeRunner

_VAR = "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS"


def _payload(tmp_path: Path, **metadata) -> RunnerPayload:
    prompt = tmp_path / "prompt.md"
    prompt.write_text("do the thing", encoding="utf-8")
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude", prompt_file=str(prompt), metadata=dict(metadata)),
        metadata={},
        secrets={},
    )


def _env(runner: ClaudeRunner, payload: RunnerPayload) -> dict:
    return runner._build_invocation(payload)[1]


def _runner(**options) -> ClaudeRunner:
    return ClaudeRunner(
        RunnerDefinition(name="claude", kind="claude", command=None, options=dict(options)),
        settings,
    )


class TestItIsOffUnlessAsked:
    def test_an_untouched_deployment_is_unchanged(self, tmp_path) -> None:
        """The variable must be absent, not set to 0: an explicit 0 is still
        a behaviour change to justify, and this must be a no-op by default."""
        assert _VAR not in _env(_runner(), _payload(tmp_path))


class TestItCanBeAskedFor:
    def test_the_runner_definition_can_set_it(self, tmp_path) -> None:
        env = _env(_runner(disable_git_instructions=True), _payload(tmp_path))

        assert env[_VAR] == "1"

    def test_step_metadata_wins_over_the_definition(self, tmp_path) -> None:
        """Same precedence as permission_mode and allowed_tools — a role that
        genuinely commits must be able to override a runner-wide default."""
        env = _env(
            _runner(disable_git_instructions=True),
            _payload(tmp_path, disable_git_instructions=False),
        )

        assert _VAR not in env

    def test_a_step_can_turn_it_on_alone(self, tmp_path) -> None:
        env = _env(_runner(), _payload(tmp_path, disable_git_instructions=True))

        assert env[_VAR] == "1"


class TestItNeverOverridesTheOperator:
    def test_an_explicit_value_in_the_environment_wins(self, tmp_path) -> None:
        """A project or secret that names the variable outranks our
        optimisation — the same rule `ANTHROPIC_BASE_URL` follows just
        above it."""
        payload = _payload(tmp_path)
        payload.project.env = {_VAR: "0"}

        env = _env(_runner(disable_git_instructions=True), payload)

        assert env[_VAR] == "0"
