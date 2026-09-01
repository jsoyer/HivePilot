"""HP-73 audit quick-win — native local Ollama.

CLI mode must pass the model POSITIONALLY (`ollama run <model> <prompt>`), never
as a `--model` flag (Ollama rejects it — the base `PromptCliRunner` would emit
it). `mode: api` must default to Ollama's OpenAI-compatible local endpoint with
zero extra config."""

from __future__ import annotations

from pathlib import Path

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.prompt_cli_runner import OllamaRunner


def _payload(tmp_path: Path, *, step_model: str | None = None) -> RunnerPayload:
    pf = tmp_path / "p.md"
    pf.write_text("do it", encoding="utf-8")
    metadata = {"model": step_model} if step_model else {}
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="ollama", prompt_file=str(pf), metadata=metadata),
        metadata={},
        secrets={},
    )


def _runner(model: str | None = None, command: str | None = None, **options) -> OllamaRunner:
    definition = RunnerDefinition(
        name="r", kind="ollama", command=command, model=model, options=options
    )
    return OllamaRunner(definition, settings)


class TestCliPositionalModel:
    def test_model_is_positional_never_a_flag(self, tmp_path):
        args = _runner(model="mistral")._build_cli_args(_payload(tmp_path), "hello")
        assert args == ["ollama", "run", "mistral", "hello"]
        assert "--model" not in args  # regression: Ollama has no --model flag

    def test_defaults_to_the_class_default_model(self, tmp_path):
        args = _runner()._build_cli_args(_payload(tmp_path), "hello")
        assert args == ["ollama", "run", "llama3.2", "hello"]

    def test_step_metadata_model_wins(self, tmp_path):
        args = _runner(model="mistral")._build_cli_args(_payload(tmp_path, step_model="phi3"), "hi")
        assert args[:3] == ["ollama", "run", "phi3"]

    def test_explicit_command_override_is_respected(self, tmp_path):
        args = _runner(model="mistral", command="ollama run")._build_cli_args(
            _payload(tmp_path), "hi"
        )
        assert args == ["ollama", "run", "mistral", "hi"]


class TestApiDefaults:
    def test_api_provider_and_model_default(self, tmp_path):
        r = _runner(model="qwen2.5-coder")
        assert r.definition.options["api_provider"] == "openai"
        assert r.definition.options["api_model"] == "qwen2.5-coder"

    def test_api_model_falls_back_to_default_when_unset(self):
        assert _runner().definition.options["api_model"] == "llama3.2"

    def test_local_endpoint_and_placeholder_key_injected(self):
        env = _runner().definition.env
        assert env["OPENAI_BASE_URL"] == "http://localhost:11434/v1"
        assert env["OPENAI_API_KEY"] == "ollama"

    def test_explicit_values_are_not_overridden(self):
        r = _runner(
            api_provider="custom",
        )
        # options set by caller win
        assert r.definition.options["api_provider"] == "custom"

    def test_caller_base_url_is_preserved(self):
        definition = RunnerDefinition(
            name="r",
            kind="ollama",
            command=None,
            env={"OPENAI_BASE_URL": "http://remote:11434/v1"},
        )
        r = OllamaRunner(definition, settings)
        assert r.definition.env["OPENAI_BASE_URL"] == "http://remote:11434/v1"

    def test_api_mode_is_supported(self):
        assert "api" in OllamaRunner.supported_modes
