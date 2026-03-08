from __future__ import annotations

from typing import Dict, Type

from hivepilot.config import settings
from hivepilot.models import RunnerDefinition
from hivepilot.runners.base import BaseRunner, RunnerPayload
from hivepilot.runners.claude_runner import ClaudeRunner
from hivepilot.runners.shell_runner import ShellRunner
from hivepilot.runners.langchain_runner import LangChainRunner
from hivepilot.runners.internal_runner import InternalRunner
from hivepilot.runners.prompt_cli_runner import CodexRunner, GeminiRunner, OpenCodeRunner, OllamaRunner
from hivepilot.runners.container_runner import ContainerRunner


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
            return RunnerDefinition(name=name, kind=name, command=default_command)
        raise KeyError(f"Runner '{name}' not found in registry.")

    def execute(self, runner_name: str, payload: RunnerPayload) -> None:
        runner = self.get_runner(runner_name)
        runner.run(payload)
