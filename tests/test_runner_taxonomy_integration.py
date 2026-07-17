"""Sprint 5 (runner-defaults-plugins-mode PRD): cross-cutting integration
tests binding together the pieces Sprints 1-4 built independently.

Each unit-level behaviour already has focused coverage elsewhere
(`tests/test_pipeline_mode.py`, `tests/test_claude_api_mode.py`,
`tests/test_new_agent_plugins.py`, `tests/test_agent_plugin_migration.py`,
`tests/test_mandatory_agents.py`). This file instead exercises the SEAMS
between them end-to-end, matching the sprint spec's five scenarios:

(a) a two-stage pipeline where one stage explicitly overrides `mode` (the
    other inherits the pipeline default) threads the per-stage RESOLVED mode
    into each stage's own runner dispatch, independently -- not just
    `resolve_mode`'s precedence table in isolation.
(b) `mode: api` on `ClaudeRunner`, driven through the SAME choke point the
    orchestrator uses (`RunnerRegistry.capture_definition`), never leaks the
    resolved `ANTHROPIC_API_KEY` value into the returned detail, the request
    argv (there is none -- api mode never shells out), or a raised
    exception's message.
(c) each of the six PATH-gated agent plugins (gemini/opencode/ollama/pi/
    qwen-code/kimi-cli) is toggled active/inactive purely by monkeypatched
    `shutil.which`, driven through the REAL `PluginManager` + registry --
    present -> resolves to its runner class; absent -> the actionable
    `RunnerPluginUnavailableError` (never a bare `KeyError`).
(d) `kind: gemini` backward-compat: with the plugin active, a `RunnerRegistry`
    built from a plain `{"gemini": RunnerDefinition(kind="gemini")}` config
    (the pre-Sprint-2 config shape) resolves AND dispatches through
    `GeminiRunner`, unchanged.
(e) `hivepilot init`'s REAL, shipped verdict when none of claude/codex/vibe
    are on PATH: exit 0 with a warning (never a hard fail) -- see
    `hivepilot.cli._handle_mandatory_agent_verdict`'s docstring and the
    already-existing `tests/test_mandatory_agents.py`, which this scenario
    intentionally matches rather than the sprint spec's literal "exits
    non-zero" wording (verified against real code -- see Sprint 5 Agent
    Notes for the discrepancy).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub out optional heavy dependencies before importing hivepilot.cli -- same
# approach as tests/test_mandatory_agents.py / tests/test_cli.py, needed
# because hivepilot.cli transitively imports hivepilot.orchestrator, which
# imports several optional extras that may not be installed in this env.
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

from hivepilot import plugins as plugins_mod  # noqa: E402
from hivepilot.cli import app  # noqa: E402
from hivepilot.config import settings  # noqa: E402
from hivepilot.models import (  # noqa: E402
    PipelineConfig,
    PipelineStage,
    ProjectConfig,
    RunnerDefinition,
    TaskConfig,
    TaskStep,
    resolve_mode,
)
from hivepilot.orchestrator import Orchestrator  # noqa: E402
from hivepilot.registry import (  # noqa: E402
    RUNNER_MAP,
    RunnerPluginUnavailableError,
    RunnerRegistry,
    resolve_runner_class,
)
from hivepilot.runners.base import BaseRunner, RunnerPayload  # noqa: E402
from hivepilot.runners.prompt_cli_runner import GeminiRunner  # noqa: E402

REPO_ROOT = Path(__file__).parent.parent
_FAKE_KEY = "sk-ant-TESTKEY-taxonomy-integration-do-not-log"

# (kind, per-plugin enable flag, required CLI binary) -- mirrors
# hivepilot.registry._OPTIONAL_AGENT_PLUGIN_KINDS exactly (single source of
# truth, verified against real code -- see Agent Notes).
_OPTIONAL_PLUGIN_SPECS = [
    ("gemini", "gemini_enabled", "gemini"),
    ("opencode", "opencode_enabled", "opencode"),
    ("ollama", "ollama_enabled", "ollama"),
    ("pi", "pi_enabled", "pi"),
    ("qwen-code", "qwen_code_enabled", "qwen"),
    ("kimi-cli", "kimi_cli_enabled", "kimi"),
]


def _fake_which(present: set[str]):
    def _which(name: str) -> Optional[str]:
        return f"/usr/bin/{name}" if name in present else None

    return _which


def _bare_orchestrator() -> Orchestrator:
    """Construct an Orchestrator with a real (empty) RunnerRegistry and
    stubbed plugins -- mirrors tests/test_agent_checks.py's helper of the
    same name."""
    with (
        patch("hivepilot.orchestrator.load_projects", return_value=MagicMock(projects={})),
        patch("hivepilot.orchestrator.load_tasks", return_value=MagicMock(tasks={}, runners={})),
        patch(
            "hivepilot.orchestrator.load_pipelines",
            return_value=MagicMock(pipelines={}),
        ),
        patch("hivepilot.orchestrator.RunnerRegistry", return_value=RunnerRegistry({})),
        patch("hivepilot.orchestrator.PluginManager", return_value=MagicMock()),
    ):
        orch = Orchestrator()
    orch.plugins = MagicMock()
    return orch


def _payload(tmp_path: Path, runner_name: str = "x", metadata: dict | None = None) -> RunnerPayload:
    pf = tmp_path / "prompt.md"
    pf.write_text("do the thing", encoding="utf-8")
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner=runner_name, prompt_file=str(pf), metadata=metadata or {}),
        metadata={},
        secrets={},
    )


def _fake_response(json_body: dict):
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = json_body
    resp.content = b"x"
    resp.text = ""
    return resp


# ---------------------------------------------------------------------------
# (a) two-stage pipeline: stage-level `mode` override reaches the right
#     runners, independently per stage
# ---------------------------------------------------------------------------

_SEEN_MODES: list[tuple[str, str | None]] = []


class _RecordingRunner(BaseRunner):
    supported_modes = frozenset({"cli", "api"})

    def __init__(self, definition: RunnerDefinition, settings) -> None:  # noqa: ANN001
        self.definition = definition
        self.settings = settings

    def run(self, payload: RunnerPayload) -> None:  # pragma: no cover - unused
        _SEEN_MODES.append((self.definition.name, payload.step.metadata.get("mode")))

    def capture(self, payload: RunnerPayload) -> str:
        _SEEN_MODES.append((self.definition.name, payload.step.metadata.get("mode")))
        return "ok"


def test_two_stage_pipeline_stage_mode_override_reaches_correct_runner(tmp_path: Path) -> None:
    """Pipeline default `cli`; stage 2 explicitly overrides to `api`. Each
    stage's `resolve_mode()` result must independently reach the runner
    dispatch for THAT stage's steps -- proving the stage > pipeline
    precedence Sprint 1 established actually threads through to the runner
    boundary via `Orchestrator._execute_task_body`, not just in
    `resolve_mode`'s own precedence table (already unit-tested in
    tests/test_pipeline_mode.py)."""
    global _SEEN_MODES
    _SEEN_MODES = []
    RunnerRegistry.register("recording-taxonomy", _RecordingRunner, override=True)
    try:
        pipeline = PipelineConfig(
            description="two-stage",
            mode="cli",
            stages=[
                PipelineStage(name="s1", task="t1"),  # inherits pipeline default -> cli
                PipelineStage(name="s2", task="t2", mode="api"),  # explicit override
            ],
        )
        stage1, stage2 = pipeline.stages
        # Sanity: the precedence itself resolves as expected before we even
        # touch a runner.
        assert resolve_mode(pipeline, stage1) == "cli"
        assert resolve_mode(pipeline, stage2) == "api"

        orch = _bare_orchestrator()
        task1 = TaskConfig(description="d", steps=[TaskStep(name="s", runner="recording-taxonomy")])
        task2 = TaskConfig(description="d", steps=[TaskStep(name="s", runner="recording-taxonomy")])
        project = ProjectConfig(path=tmp_path)

        with (
            patch.object(orch, "_resolve_secrets", return_value={}),
            patch("hivepilot.orchestrator.perform_git_actions"),
        ):
            orch._execute_task_body(
                project=project,
                task_name="t1",
                task=task1,
                extra_prompt=None,
                auto_git=True,
                run_id=None,
                policy=None,
                simulate=False,
                dry_run=True,
                mode=resolve_mode(pipeline, stage1),
            )
            orch._execute_task_body(
                project=project,
                task_name="t2",
                task=task2,
                extra_prompt=None,
                auto_git=True,
                run_id=None,
                policy=None,
                simulate=False,
                dry_run=True,
                mode=resolve_mode(pipeline, stage2),
            )

        # Stage 1's step must have seen "cli" (or None, which the runner
        # itself would treat as the cli default -- mirrors
        # test_agent_checks.py::test_default_run_leaves_mode_cli); stage 2's
        # step must have seen "api" explicitly, proving the override reached
        # THAT stage's runner and did not leak into stage 1's.
        assert _SEEN_MODES[0][0] == "recording-taxonomy"
        assert _SEEN_MODES[0][1] in ("cli", None)
        assert _SEEN_MODES[1] == ("recording-taxonomy", "api")
    finally:
        RUNNER_MAP.pop("recording-taxonomy", None)


# ---------------------------------------------------------------------------
# (b) claude api-mode masking, driven through RunnerRegistry.capture_definition
# ---------------------------------------------------------------------------


def test_claude_api_mode_masks_key_through_registry_capture_choke_point(
    tmp_path: Path, monkeypatch
) -> None:
    """`RunnerRegistry.capture_definition` is the single choke point every
    runner's capture() goes through in real pipeline runs. Driving mode:api
    through THAT entry point (not calling ClaudeRunner directly) proves the
    masking survives the actual dispatch path the orchestrator uses."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", _FAKE_KEY)
    registry = RunnerRegistry(
        {"claude": RunnerDefinition(name="claude", kind="claude", model="claude-3-5-sonnet-latest")}
    )
    payload = _payload(tmp_path, runner_name="claude", metadata={"mode": "api"})
    body = {
        "content": [{"type": "text", "text": f"here is your key {_FAKE_KEY} oops"}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    with (
        patch(
            "hivepilot.runners.claude_runner.requests.post",
            return_value=_fake_response(body),
        ),
        patch("hivepilot.runners.claude_runner.subprocess.run") as mock_sub,
    ):
        detail = registry.capture_definition(registry.runner_defs["claude"], payload)

    assert _FAKE_KEY not in detail, "API key must never reach RunResult.detail"
    assert "REDACTED" in detail
    mock_sub.assert_not_called()  # api mode never shells out -- key can't leak via argv either


def test_claude_api_mode_masks_key_in_raised_exception_message(tmp_path: Path, monkeypatch) -> None:
    """A failed API response that reflects request content back (error body)
    must not leak the key through the exception message either -- masking
    covers the failure path, not just the happy path."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", _FAKE_KEY)
    registry = RunnerRegistry({"claude": RunnerDefinition(name="claude", kind="claude", model="m")})
    payload = _payload(tmp_path, runner_name="claude", metadata={"mode": "api"})

    error_resp = MagicMock()
    error_resp.ok = False
    error_resp.status_code = 400
    error_resp.text = f"bad request, saw header x-api-key: {_FAKE_KEY}"
    error_resp.content = b"x"

    with patch("hivepilot.runners.claude_runner.requests.post", return_value=error_resp):
        with pytest.raises(RuntimeError) as exc_info:
            registry.capture_definition(registry.runner_defs["claude"], payload)

    assert _FAKE_KEY not in str(exc_info.value), "API key must never leak via an exception message"


# ---------------------------------------------------------------------------
# (c) plugin activation toggled purely by monkeypatched shutil.which, driven
#     through the REAL PluginManager + registry.resolve_runner_class
# ---------------------------------------------------------------------------


class TestPluginActivationTogglesWithPath:
    @pytest.mark.parametrize("kind,flag_name,binary", _OPTIONAL_PLUGIN_SPECS)
    def test_binary_present_activates_kind(self, kind, flag_name, binary, monkeypatch) -> None:
        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        monkeypatch.setattr(settings, flag_name, True, raising=False)
        RUNNER_MAP.pop(kind, None)
        try:
            with patch("shutil.which", side_effect=_fake_which({binary})):
                plugins_mod.PluginManager()
            assert kind in RUNNER_MAP
            assert resolve_runner_class(kind) is RUNNER_MAP[kind]
        finally:
            RUNNER_MAP.pop(kind, None)

    @pytest.mark.parametrize("kind,flag_name,binary", _OPTIONAL_PLUGIN_SPECS)
    def test_binary_absent_deactivates_kind_with_actionable_error(
        self, kind, flag_name, binary, monkeypatch
    ) -> None:
        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        monkeypatch.setattr(settings, flag_name, True, raising=False)
        RUNNER_MAP.pop(kind, None)
        try:
            with patch("shutil.which", return_value=None):
                plugins_mod.PluginManager()
            assert kind not in RUNNER_MAP

            with pytest.raises(RunnerPluginUnavailableError) as exc_info:
                resolve_runner_class(kind)
            msg = str(exc_info.value)
            assert f"HIVEPILOT_{flag_name.upper()}" in msg
            assert repr(binary) in msg
        finally:
            RUNNER_MAP.pop(kind, None)


# ---------------------------------------------------------------------------
# (d) `kind: gemini` backward-compat: plugin active -> full RunnerRegistry
#     resolution + dispatch through GeminiRunner, unchanged
# ---------------------------------------------------------------------------


def test_kind_gemini_compat_resolves_and_dispatches_through_gemini_runner(
    tmp_path: Path, monkeypatch
) -> None:
    """A config that still says `kind: gemini` (the pre-Sprint-2 shape) must
    keep working end-to-end -- both resolution (RunnerRegistry.get_runner)
    AND dispatch (the built CLI argv) -- as long as the plugin is active
    (flag on, default True, + binary on PATH)."""
    monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
    monkeypatch.setattr(settings, "gemini_enabled", True, raising=False)
    RUNNER_MAP.pop("gemini", None)
    try:
        with patch("shutil.which", side_effect=_fake_which({"gemini"})):
            plugins_mod.PluginManager()

        registry = RunnerRegistry(
            {"gemini": RunnerDefinition(name="gemini", kind="gemini", command="gemini")}
        )
        runner = registry.get_runner("gemini")
        assert isinstance(runner, GeminiRunner)

        payload = _payload(tmp_path, runner_name="gemini")
        with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as mock_run:
            runner.run(payload)
        args = mock_run.call_args.args[0]
        assert args[0] == "gemini"
        assert "-p" in args
        assert args[args.index("-p") + 1] == "do the thing"
    finally:
        RUNNER_MAP.pop("gemini", None)


# ---------------------------------------------------------------------------
# (e) `hivepilot init`'s REAL verdict when no mandatory agent is on PATH:
#     exit 0 + warning, never a hard fail (see module docstring)
# ---------------------------------------------------------------------------


def test_init_exits_zero_with_warning_when_no_mandatory_agent_on_path(
    tmp_path: Path, monkeypatch
) -> None:
    """Sprint spec scenario (e) says "`init` exits non-zero when NO mandatory
    agent is on PATH". Verified against real shipped code
    (`hivepilot.cli._handle_mandatory_agent_verdict`'s docstring: "Warn
    (never hard-fail)") and the pre-existing
    tests/test_mandatory_agents.py::test_init_exits_zero_with_warning_...
    test: `init`'s whole job is to scaffold a working config so an agent CLI
    can be installed INTO it next, so hard-failing here would be a
    chicken-and-egg regression on a fresh machine / CI. This test asserts
    the REAL, intentional behaviour rather than fabricating a non-zero-exit
    assertion that does not hold -- see Sprint 5 Agent Notes."""
    monkeypatch.setattr(shutil, "which", _fake_which(set()))

    runner = CliRunner()
    result = runner.invoke(app, ["init", "--path", str(tmp_path), "--yes"])

    assert result.exit_code == 0, result.output
    lowered = result.output.lower()
    assert "warning" in lowered
    assert "claude" in lowered
    assert "codex" in lowered
    assert "vibe" in lowered


def test_check_mandatory_agents_reports_none_present_when_path_is_empty(monkeypatch) -> None:
    """The underlying scan (what `init`'s warning is based on) really does
    report nothing found -- the warning is not just cosmetic, it reflects a
    real `any_ok is False` verdict."""
    from hivepilot.services import agent_checks

    monkeypatch.setattr(shutil, "which", _fake_which(set()))
    report = agent_checks.check_mandatory_agents()
    assert report.any_ok is False
    assert report.present == []


# ---------------------------------------------------------------------------
# `plugins list` taxonomy (Sprint 5 cli.py change): built-in / plugin-active /
# plugin-inactive / API-only tags, sourced from the REAL registry.
# ---------------------------------------------------------------------------


def test_plugins_list_renders_agent_runner_taxonomy(monkeypatch) -> None:
    """`hivepilot plugins list` must render the new "Agent Runners" table
    with: every built-in agent kind, `openrouter` explicitly tagged
    API-only, and every optional plugin agent kind tagged active/inactive
    by REAL RUNNER_MAP membership (which PluginManager already populated
    correctly per PATH/flag state by the time this command runs)."""
    from hivepilot.registry import _OPTIONAL_AGENT_PLUGIN_KINDS

    runner = CliRunner()
    result = runner.invoke(app, ["plugins", "list"])

    assert result.exit_code == 0, result.output
    assert "Agent Runners" in result.output
    for kind in ("claude", "codex", "vibe", "openrouter"):
        assert kind in result.output
    assert "API-only" in result.output

    for kind, (flag_name, _binary) in _OPTIONAL_AGENT_PLUGIN_KINDS.items():
        assert kind in result.output, f"plugin agent kind {kind!r} missing from plugins list"
        assert f"HIVEPILOT_{flag_name.upper()}" in result.output

    expected_status = "active" if "gemini" in RUNNER_MAP else "inactive"
    assert expected_status in result.output.lower()


def test_plugins_list_agent_runners_table_reflects_inactive_plugin(monkeypatch) -> None:
    """With `gemini` forced inactive (flag off), the Agent Runners table must
    say so explicitly (not silently drop the row) -- an operator scanning
    the list should see it's disabled, not absent.

    Relies on the CLI command's OWN real `Orchestrator()` -> `PluginManager()`
    scan (triggered by `runner.invoke` below) rather than constructing a
    second `PluginManager()` directly in this test -- two real scans in the
    same test collide on every OTHER already-registered plugin kind (e.g.
    `herdr`), since `RunnerRegistry.register` refuses to silently
    re-register without `override=True`."""
    monkeypatch.setattr(settings, "gemini_enabled", False, raising=False)
    RUNNER_MAP.pop("gemini", None)
    try:
        runner = CliRunner()
        result = runner.invoke(app, ["plugins", "list"])

        assert result.exit_code == 0, result.output
        assert "gemini" not in RUNNER_MAP
        assert "gemini" in result.output
        assert "inactive" in result.output.lower()
    finally:
        RUNNER_MAP.pop("gemini", None)
