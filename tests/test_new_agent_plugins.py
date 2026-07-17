"""Sprint 3 (runner-defaults-plugins-mode PRD): three NEW default-on,
PATH-gated agent plugins — `pi` / `qwen-code` / `kimi-cli`.

Covers:
- Each new plugin (`plugins/pi.py` / `plugins/qwen_code.py` /
  `plugins/kimi_cli.py`) follows the SAME canonical gated-agent-plugin
  skeleton Sprint 2 established (see `plugins/gemini.py` / this repo's
  `tests/test_agent_plugin_migration.py`): `register()` returns `{}` when
  EITHER its per-plugin enable flag is off OR its CLI binary is absent from
  PATH, else `{"runners": {<kind>: <RunnerClass>}, "health": {...}}`.
- With the flag on (default True) and the binary present, each new kind
  (`pi`, `qwen-code`, `kimi-cli`) resolves via the REAL `PluginManager` to
  its runner class.
- Inactive-kind resolution (flag off or binary absent) raises the actionable
  `RunnerPluginUnavailableError` — naming the enable flag + required binary —
  NOT a bare `KeyError`. `hivepilot.registry._OPTIONAL_AGENT_PLUGIN_KINDS` was
  extended in Sprint 3 to include pi/qwen-code/kimi-cli, matching the
  gemini/opencode/ollama behavior Sprint 2 established.
- The built argv for each new runner matches its confirmed non-interactive
  invocation (esp. kimi-cli: prompt via `-p` on argv, never stdin).
- `qwen-code` in `mode: api` routes through the existing OpenAI-compat
  `_run_api` backend with the configured base URL.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.registry import RUNNER_MAP, resolve_runner_class
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.prompt_cli_runner import KimiCliRunner, PiRunner, QwenCodeRunner

REPO_ROOT = Path(__file__).parent.parent

# (plugin file stem, runner kind, runner class, per-plugin enable flag, CLI binary)
_PLUGIN_SPECS = [
    ("pi", "pi", PiRunner, "pi_enabled", "pi"),
    ("qwen_code", "qwen-code", QwenCodeRunner, "qwen_code_enabled", "qwen"),
    ("kimi_cli", "kimi-cli", KimiCliRunner, "kimi_cli_enabled", "kimi"),
]


def _load_plugin_module(stem: str) -> ModuleType:
    """Load plugins/<stem>.py by file path — same mechanism
    `hivepilot.plugins._scan_local_plugins` uses (mirrors
    tests/test_agent_plugin_migration.py)."""
    path = REPO_ROOT / "plugins" / f"{stem}.py"
    spec = importlib.util.spec_from_file_location(f"hivepilot_plugin_{stem}_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Canonical gated-agent-plugin skeleton (register() gating semantics)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("stem,kind,runner_cls,flag_name,binary", _PLUGIN_SPECS)
class TestGatedAgentPluginSkeleton:
    def test_flag_defaults_to_true(self, stem, kind, runner_cls, flag_name, binary) -> None:
        assert getattr(settings, flag_name) is True

    def test_register_exposes_kind_when_enabled_and_binary_present(
        self, stem, kind, runner_cls, flag_name, binary, monkeypatch
    ) -> None:
        module = _load_plugin_module(stem)
        monkeypatch.setattr(settings, flag_name, True, raising=False)
        with patch.object(module.shutil, "which", return_value=f"/usr/local/bin/{binary}"):
            hooks = module.register()
        assert hooks.get("runners") == {kind: runner_cls}

    def test_register_returns_empty_when_flag_disabled(
        self, stem, kind, runner_cls, flag_name, binary, monkeypatch
    ) -> None:
        module = _load_plugin_module(stem)
        monkeypatch.setattr(settings, flag_name, False, raising=False)
        with patch.object(module.shutil, "which", return_value=f"/usr/local/bin/{binary}"):
            assert module.register() == {}

    def test_register_returns_empty_when_binary_absent(
        self, stem, kind, runner_cls, flag_name, binary, monkeypatch
    ) -> None:
        module = _load_plugin_module(stem)
        monkeypatch.setattr(settings, flag_name, True, raising=False)
        with patch.object(module.shutil, "which", return_value=None):
            assert module.register() == {}

    def test_register_returns_empty_when_both_flag_off_and_binary_absent(
        self, stem, kind, runner_cls, flag_name, binary, monkeypatch
    ) -> None:
        module = _load_plugin_module(stem)
        monkeypatch.setattr(settings, flag_name, False, raising=False)
        with patch.object(module.shutil, "which", return_value=None):
            assert module.register() == {}


# ---------------------------------------------------------------------------
# Backward/forward resolution via the REAL PluginManager
# ---------------------------------------------------------------------------


class TestResolutionViaRealPluginManager:
    @pytest.mark.parametrize("stem,kind,runner_cls,flag_name,binary", _PLUGIN_SPECS)
    def test_kind_resolves_to_runner_class_when_binary_present(
        self, stem, kind, runner_cls, flag_name, binary, monkeypatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        monkeypatch.setattr(settings, flag_name, True, raising=False)
        RUNNER_MAP.pop(kind, None)

        with patch(
            "shutil.which",
            side_effect=lambda name: f"/usr/local/bin/{name}" if name == binary else None,
        ):
            plugins_mod.PluginManager()

        assert resolve_runner_class(kind) is runner_cls

    @pytest.mark.parametrize("stem,kind,runner_cls,flag_name,binary", _PLUGIN_SPECS)
    def test_kind_unregistered_when_flag_disabled(
        self, stem, kind, runner_cls, flag_name, binary, monkeypatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        monkeypatch.setattr(settings, flag_name, False, raising=False)
        RUNNER_MAP.pop(kind, None)

        with patch(
            "shutil.which",
            side_effect=lambda name: f"/usr/local/bin/{name}" if name == binary else None,
        ):
            plugins_mod.PluginManager()

        assert kind not in RUNNER_MAP

    @pytest.mark.parametrize("stem,kind,runner_cls,flag_name,binary", _PLUGIN_SPECS)
    def test_kind_unregistered_when_binary_absent(
        self, stem, kind, runner_cls, flag_name, binary, monkeypatch
    ) -> None:
        from hivepilot import plugins as plugins_mod

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        monkeypatch.setattr(settings, flag_name, True, raising=False)
        RUNNER_MAP.pop(kind, None)

        with patch("shutil.which", return_value=None):
            plugins_mod.PluginManager()

        assert kind not in RUNNER_MAP


class TestInactiveKindRaisesActionableError:
    """Acceptance criterion #2 + INVARIANT "Plugin PATH Gate" postcondition:
    resolving an inactive new kind (flag off or binary absent) yields the
    actionable `RunnerPluginUnavailableError` (naming the enable flag + required
    binary), NOT a bare `KeyError`. `hivepilot.registry._OPTIONAL_AGENT_PLUGIN_KINDS`
    was extended in Sprint 3 to include pi/qwen-code/kimi-cli, so they now match
    gemini/opencode/ollama's actionable-error behavior."""

    @pytest.mark.parametrize("stem,kind,runner_cls,flag_name,binary", _PLUGIN_SPECS)
    def test_inactive_kind_raises_actionable_error(
        self, stem, kind, runner_cls, flag_name, binary, monkeypatch
    ) -> None:
        from hivepilot import plugins as plugins_mod
        from hivepilot.registry import RunnerPluginUnavailableError

        monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
        monkeypatch.setattr(settings, flag_name, False, raising=False)
        RUNNER_MAP.pop(kind, None)
        plugins_mod.PluginManager()

        with pytest.raises(RunnerPluginUnavailableError) as exc_info:
            resolve_runner_class(kind)
        # The actionable error names both the enable flag and the required binary.
        msg = str(exc_info.value)
        assert f"HIVEPILOT_{flag_name.upper()}" in msg
        assert repr(binary) in msg


# ---------------------------------------------------------------------------
# Built-argv assertions — non-interactive invocation contract
# ---------------------------------------------------------------------------


def _payload(tmp_path: Path) -> RunnerPayload:
    pf = tmp_path / "prompt.md"
    pf.write_text("do the thing", encoding="utf-8")
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="x", prompt_file=str(pf)),
        metadata={},
        secrets={},
    )


def _run_and_capture_call(cls, kind, command, model, tmp_path):
    runner = cls(RunnerDefinition(name=kind, kind=kind, command=command, model=model), settings)
    with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as m:
        runner.run(_payload(tmp_path))
    return m.call_args


class TestPiRunnerArgv:
    def test_defaults(self) -> None:
        assert PiRunner.command_name == "pi"
        assert PiRunner.prompt_flag == "-p"
        assert "--approve" in PiRunner.cli_flags

    def test_built_argv_contains_prompt_model_and_approve(self, tmp_path: Path) -> None:
        call = _run_and_capture_call(PiRunner, "pi", "pi", "claude-opus-4", tmp_path)
        args = call.args[0]
        assert args[0] == "pi"
        assert "-p" in args
        assert args[args.index("-p") + 1] == "do the thing"
        assert "--model" in args
        assert args[args.index("--model") + 1] == "claude-opus-4"
        assert "--approve" in args


class TestQwenCodeRunnerArgv:
    def test_defaults(self) -> None:
        assert QwenCodeRunner.command_name == "qwen"
        assert QwenCodeRunner.prompt_flag == "-p"
        assert QwenCodeRunner.model_flag == "-m"
        assert QwenCodeRunner.cli_flags == ("--approval-mode", "yolo")

    def test_built_argv_uses_default_model_when_none_configured(self, tmp_path: Path) -> None:
        call = _run_and_capture_call(QwenCodeRunner, "qwen-code", None, None, tmp_path)
        args = call.args[0]
        assert args[0] == "qwen"
        assert "-p" in args
        assert args[args.index("-p") + 1] == "do the thing"
        assert "-m" in args
        assert args[args.index("-m") + 1] == "qwen3-coder-plus"
        assert "--approval-mode" in args
        assert args[args.index("--approval-mode") + 1] == "yolo"

    def test_built_argv_explicit_model_overrides_default(self, tmp_path: Path) -> None:
        call = _run_and_capture_call(QwenCodeRunner, "qwen-code", None, "qwen3-max", tmp_path)
        args = call.args[0]
        assert "-m" in args
        assert args[args.index("-m") + 1] == "qwen3-max"


class TestKimiCliRunnerArgv:
    def test_defaults(self) -> None:
        assert KimiCliRunner.command_name == "kimi"
        assert KimiCliRunner.prompt_flag == "-p"
        assert KimiCliRunner.model_flag == "-m"
        assert KimiCliRunner.cli_flags == ("--print", "--yolo")

    def test_built_argv_contains_print_yolo_model_and_prompt_flag(self, tmp_path: Path) -> None:
        call = _run_and_capture_call(KimiCliRunner, "kimi-cli", None, "kimi-k2", tmp_path)
        args = call.args[0]
        assert args[0] == "kimi"
        assert "--print" in args
        assert "--yolo" in args
        assert "-m" in args
        assert args[args.index("-m") + 1] == "kimi-k2"
        assert "-p" in args
        assert args[args.index("-p") + 1] == "do the thing"

    def test_prompt_is_never_piped_via_stdin(self, tmp_path: Path) -> None:
        """CRITICAL: kimi detects non-TTY stdin and silently skips it — the
        prompt must travel on argv via -p only, never as subprocess.run's
        `input=` kwarg."""
        call = _run_and_capture_call(KimiCliRunner, "kimi-cli", None, "kimi-k2", tmp_path)
        assert "input" not in call.kwargs
        args = call.args[0]
        # The prompt text must be the argv element immediately after "-p".
        assert args[args.index("-p") + 1] == "do the thing"


# ---------------------------------------------------------------------------
# qwen-code mode:api — routes through the OpenAI-compat backend
# ---------------------------------------------------------------------------


def _api_payload(tmp_path: Path) -> RunnerPayload:
    pf = tmp_path / "p.md"
    pf.write_text("do it", encoding="utf-8")
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="qwen-code", prompt_file=str(pf), metadata={"mode": "api"}),
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


class TestQwenCodeApiMode:
    def test_routes_through_openai_compat_backend_with_configured_base_url(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        monkeypatch.setenv("OPENAI_BASE_URL", "https://my-qwen-proxy.example.com/v1")
        payload = _api_payload(tmp_path)
        runner = QwenCodeRunner(RunnerDefinition(name="qwen-code", kind="qwen-code"), settings)
        body = {"choices": [{"message": {"content": "OK FROM QWEN"}}]}

        with patch(
            "hivepilot.runners.prompt_cli_runner.requests.post",
            return_value=_fake_response(body),
        ) as mock_post:
            out = runner.capture(payload)

        assert out == "OK FROM QWEN"
        call = mock_post.call_args
        url = call.args[0] if call.args else call.kwargs.get("url", "")
        assert url == "https://my-qwen-proxy.example.com/v1/chat/completions"
        sent_payload = call.kwargs.get("json", {})
        assert sent_payload.get("model") == "qwen3-coder-plus"

    def test_api_provider_defaults_to_openai(self) -> None:
        runner = QwenCodeRunner(RunnerDefinition(name="qwen-code", kind="qwen-code"), settings)
        assert runner.definition.options["api_provider"] == "openai"

    def test_construction_does_not_mutate_the_original_definition(self) -> None:
        original = RunnerDefinition(name="qwen-code", kind="qwen-code")
        QwenCodeRunner(original, settings)
        assert "api_provider" not in original.options
        assert original.model is None

    def test_missing_key_fails_closed_without_http_call(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        payload = _api_payload(tmp_path)
        runner = QwenCodeRunner(RunnerDefinition(name="qwen-code", kind="qwen-code"), settings)
        with patch("hivepilot.runners.prompt_cli_runner.requests.post") as mock_post:
            with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
                runner.capture(payload)
        mock_post.assert_not_called()
