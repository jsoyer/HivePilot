"""S3 (runner-defaults-plugins-mode PRD, follow-on): the `antigravity` agent
runner plugin for Google Antigravity's CLI (binary `agy`).

Mirrors `tests/test_new_agent_plugins.py`'s coverage for the pi/qwen-code/
kimi-cli plugins:

- `register()` follows the canonical gated-agent-plugin skeleton: `{}` when
  EITHER `antigravity_enabled` is off OR `agy` is absent from PATH, else
  `{"runners": {"antigravity": AntigravityRunner}, "health": {...}}`.
- `health()`: ok when `agy` is on PATH, degraded otherwise.
- Built argv matches the confirmed non-interactive invocation: `agy -p
  "<prompt>" --no-color --yes` (+ `--model <m>` when a model is configured).
- `payload.secrets` land in the subprocess env (like the other agent
  runners — via `merge_environments`).
- Resolution via the REAL `PluginManager`/`resolve_runner_class` when the
  flag is on and the binary is present.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.registry import RUNNER_MAP, resolve_runner_class
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.prompt_cli_runner import AntigravityRunner

REPO_ROOT = Path(__file__).parent.parent


def _load_plugin_module():
    path = REPO_ROOT / "plugins" / "antigravity.py"
    spec = importlib.util.spec_from_file_location("hivepilot_plugin_antigravity_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Canonical gated-agent-plugin skeleton (register() gating semantics)
# ---------------------------------------------------------------------------


def test_flag_defaults_to_true() -> None:
    assert settings.antigravity_enabled is True


def test_register_returns_antigravity_runner_when_active(monkeypatch) -> None:
    module = _load_plugin_module()
    monkeypatch.setattr(settings, "antigravity_enabled", True, raising=False)
    with patch.object(module.shutil, "which", return_value="/usr/local/bin/agy"):
        hooks = module.register()
    assert hooks.get("runners") == {"antigravity": AntigravityRunner}
    assert "antigravity" in hooks.get("health", {})


def test_register_returns_empty_when_flag_disabled(monkeypatch) -> None:
    module = _load_plugin_module()
    monkeypatch.setattr(settings, "antigravity_enabled", False, raising=False)
    with patch.object(module.shutil, "which", return_value="/usr/local/bin/agy"):
        assert module.register() == {}


def test_register_returns_empty_when_binary_absent(monkeypatch) -> None:
    module = _load_plugin_module()
    monkeypatch.setattr(settings, "antigravity_enabled", True, raising=False)
    with patch.object(module.shutil, "which", return_value=None):
        assert module.register() == {}


def test_register_returns_empty_when_both_flag_off_and_binary_absent(monkeypatch) -> None:
    module = _load_plugin_module()
    monkeypatch.setattr(settings, "antigravity_enabled", False, raising=False)
    with patch.object(module.shutil, "which", return_value=None):
        assert module.register() == {}


# ---------------------------------------------------------------------------
# health()
# ---------------------------------------------------------------------------


def test_health_ok_when_binary_present(monkeypatch) -> None:
    module = _load_plugin_module()
    with patch.object(module.shutil, "which", return_value="/usr/local/bin/agy"):
        status = module.health()
    assert status.status == "ok"


def test_health_degraded_when_binary_absent(monkeypatch) -> None:
    module = _load_plugin_module()
    with patch.object(module.shutil, "which", return_value=None):
        status = module.health()
    assert status.status == "degraded"


# ---------------------------------------------------------------------------
# Resolution via the REAL PluginManager
# ---------------------------------------------------------------------------


def test_kind_resolves_to_runner_class_when_binary_present(monkeypatch) -> None:
    from hivepilot import plugins as plugins_mod

    monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
    monkeypatch.setattr(settings, "antigravity_enabled", True, raising=False)
    RUNNER_MAP.pop("antigravity", None)

    with patch(
        "shutil.which",
        side_effect=lambda name: "/usr/local/bin/agy" if name == "agy" else None,
    ):
        plugins_mod.PluginManager()

    assert resolve_runner_class("antigravity") is AntigravityRunner


def test_kind_unregistered_when_flag_disabled(monkeypatch) -> None:
    from hivepilot import plugins as plugins_mod

    monkeypatch.setattr(plugins_mod.settings, "base_dir", REPO_ROOT, raising=False)
    monkeypatch.setattr(settings, "antigravity_enabled", False, raising=False)
    RUNNER_MAP.pop("antigravity", None)

    with patch(
        "shutil.which",
        side_effect=lambda name: "/usr/local/bin/agy" if name == "agy" else None,
    ):
        plugins_mod.PluginManager()

    assert "antigravity" not in RUNNER_MAP


# ---------------------------------------------------------------------------
# Built-argv assertions — non-interactive invocation contract
# ---------------------------------------------------------------------------


def _payload(tmp_path: Path, secrets: dict | None = None) -> RunnerPayload:
    pf = tmp_path / "prompt.md"
    pf.write_text("do the thing", encoding="utf-8")
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="x", prompt_file=str(pf)),
        metadata={},
        secrets=secrets or {},
    )


def _run_and_capture_call(command, model, tmp_path, secrets=None):
    runner = AntigravityRunner(
        RunnerDefinition(name="antigravity", kind="antigravity", command=command, model=model),
        settings,
    )
    with patch("hivepilot.runners.prompt_cli_runner.subprocess.run") as m:
        runner.run(_payload(tmp_path, secrets))
    return m.call_args


class TestAntigravityRunnerArgv:
    def test_defaults(self) -> None:
        assert AntigravityRunner.command_name == "agy"
        assert AntigravityRunner.prompt_flag == "-p"
        assert AntigravityRunner.model_flag == "--model"
        assert AntigravityRunner.cli_flags == ("--no-color", "--yes")

    def test_built_argv_matches_documented_invocation(self, tmp_path: Path) -> None:
        call = _run_and_capture_call("agy", None, tmp_path)
        args = call.args[0]
        assert args[0] == "agy"
        assert "-p" in args
        assert args[args.index("-p") + 1] == "do the thing"
        assert "--no-color" in args
        assert "--yes" in args

    def test_built_argv_includes_model_flag_when_configured(self, tmp_path: Path) -> None:
        call = _run_and_capture_call(None, "gemini-3-pro", tmp_path)
        args = call.args[0]
        assert "--model" in args
        assert args[args.index("--model") + 1] == "gemini-3-pro"

    def test_prompt_is_never_piped_via_stdin(self, tmp_path: Path) -> None:
        call = _run_and_capture_call(None, "gemini-3-pro", tmp_path)
        assert "input" not in call.kwargs

    def test_secrets_land_in_subprocess_env(self, tmp_path: Path) -> None:
        call = _run_and_capture_call(None, None, tmp_path, secrets={"AGY_API_KEY": "sekret"})
        env = call.kwargs.get("env")
        assert env is not None
        assert env.get("AGY_API_KEY") == "sekret"
