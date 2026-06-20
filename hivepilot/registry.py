from __future__ import annotations

from typing import Dict, Type, cast

from hivepilot.config import settings
from hivepilot.models import RunnerDefinition, RunnerKind
from hivepilot.runners.base import BaseRunner, RunnerPayload
from hivepilot.runners.claude_runner import ClaudeRunner
from hivepilot.runners.container_runner import ContainerRunner
from hivepilot.runners.cursor_runner import CursorRunner
from hivepilot.runners.internal_runner import InternalRunner
from hivepilot.runners.langchain_runner import LangChainRunner
from hivepilot.runners.prompt_cli_runner import (
    CodexRunner,
    GeminiRunner,
    OllamaRunner,
    OpenCodeRunner,
    VibeRunner,
)
from hivepilot.runners.shell_runner import ShellRunner

RUNNER_MAP: Dict[str, Type[BaseRunner]] = {
    "claude": ClaudeRunner,
    "shell": ShellRunner,
    "langchain": LangChainRunner,
    "internal": InternalRunner,
    "codex": CodexRunner,
    "gemini": GeminiRunner,
    "opencode": OpenCodeRunner,
    "ollama": OllamaRunner,
    "container": ContainerRunner,
    "cursor": CursorRunner,
    "vibe": VibeRunner,
}


class RunnerRegistry:
    def __init__(self, runner_defs: dict[str, RunnerDefinition]) -> None:
        self.runner_defs = runner_defs

    def get_runner(self, runner_name: str) -> BaseRunner:
        definition = self._definition_for(runner_name)
        runner_cls = RUNNER_MAP.get(definition.kind)
        if not runner_cls:
            raise KeyError(f"No runner implementation for kind '{definition.kind}'")
        return runner_cls(definition, settings)

    def _definition_for(self, name: str) -> RunnerDefinition:
        if name in self.runner_defs:
            return self.runner_defs[name]
        if name in RUNNER_MAP:
            default_command = settings.claude_command if name == "claude" else None
            return RunnerDefinition(name=name, kind=cast(RunnerKind, name), command=default_command)
        raise KeyError(f"Runner '{name}' not found in registry.")

    def execute(self, runner_name: str, payload: RunnerPayload) -> None:
        runner = self.get_runner(runner_name)
        runner.run(payload)

    def execute_definition(self, definition: RunnerDefinition, payload: RunnerPayload) -> None:
        runner_cls = RUNNER_MAP.get(definition.kind)
        if not runner_cls:
            raise KeyError(f"No runner implementation for kind '{definition.kind}'")
        runner_cls(definition, settings).run(payload)

    def capture_definition(self, definition: RunnerDefinition, payload: RunnerPayload) -> str:
        runner_cls = RUNNER_MAP.get(definition.kind)
        if not runner_cls:
            raise KeyError(f"No runner implementation for kind '{definition.kind}'")
        runner = runner_cls(definition, settings)
        capture = getattr(runner, "capture", None)
        if capture is None:
            raise RuntimeError(f"Runner kind '{definition.kind}' does not support capture.")
        return capture(payload)
