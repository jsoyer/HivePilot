from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Protocol, Type, cast

from hivepilot.config import Settings, settings
from hivepilot.models import RunnerDefinition, RunnerKind
from hivepilot.runners.base import BaseRunner, RunnerPayload, set_last_usage
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

RUNNER_MAP: Dict[str, Type[BaseRunner]] = {}


class RunnerKindCollisionError(RuntimeError):
    pass


def resolve_runner_class(kind: str) -> Type[BaseRunner]:
    """Look up the runner class registered for *kind* in ``RUNNER_MAP``.

    Raises a clear, descriptive ``KeyError`` naming the unknown kind and the
    currently available registered kinds, instead of the bare ``KeyError``
    callers would otherwise hit on an unguarded ``RUNNER_MAP[kind]``. This
    closes the whole class of "advertised-but-unregistered kind" crash (e.g.
    the historical ``"api"`` orphan — see roadmap Phase 26a) for every
    caller that resolves a kind through this helper.
    """
    runner_cls = RUNNER_MAP.get(kind)
    if runner_cls is None:
        raise KeyError(f"Unknown runner kind {kind!r}; available: {sorted(RUNNER_MAP)}")
    return runner_cls


class RunnerRegistry:
    def __init__(self, runner_defs: dict[str, RunnerDefinition]) -> None:
        self.runner_defs = runner_defs

    @staticmethod
    def register(kind: str, cls: type[BaseRunner], *, override: bool = False) -> None:
        if kind in RUNNER_MAP and RUNNER_MAP[kind] is not cls and not override:
            raise RunnerKindCollisionError(
                f"Runner kind '{kind}' is already registered to {RUNNER_MAP[kind].__name__}; "
                f"refusing to silently replace it with {cls.__name__}"
            )
        RUNNER_MAP[kind] = cls

    @staticmethod
    def known_kinds() -> frozenset[str]:
        return frozenset(RUNNER_MAP)

    def get_runner(self, runner_name: str) -> BaseRunner:
        definition = self._definition_for(runner_name)
        runner_cls = resolve_runner_class(definition.kind)
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

    @staticmethod
    def _is_worker_host(definition: RunnerDefinition) -> bool:
        return bool(definition.host) and definition.host.startswith(("http://", "https://"))

    def execute_definition(self, definition: RunnerDefinition, payload: RunnerPayload) -> None:
        if self._is_worker_host(definition):
            from hivepilot.runners.worker_runner import RemoteWorkerRunner

            try:
                RemoteWorkerRunner(definition, settings).run(payload)
                return
            except Exception:
                if not settings.worker_fallback_local:
                    raise
                definition = definition.model_copy(update={"host": None})  # W3: run locally
        runner_cls = resolve_runner_class(definition.kind)
        runner_cls(definition, settings).run(payload)

    def capture_definition(self, definition: RunnerDefinition, payload: RunnerPayload) -> str:
        # Phase 24b.2a follow-up: clear any usage stashed by an EARLIER,
        # unrelated capture (e.g. a debate/rebuttal/challenge-resolution call
        # that never pops it — see hivepilot/orchestrator.py's non-main-loop
        # capture_definition call sites) before this call's runner does
        # anything. capture_definition is the single choke point every
        # runner's capture() goes through, so clearing here guarantees no
        # step's usage can ever be misattributed to a LATER, unrelated step
        # that pops via pop_last_usage() after this call returns. The runner
        # itself (e.g. ClaudeRunner.capture()) still sets fresh usage when it
        # actually captures it — this is belt-and-suspenders with that
        # runner-level clear-at-top, not a replacement for it.
        set_last_usage(None)
        if self._is_worker_host(definition):
            from hivepilot.runners.worker_runner import RemoteWorkerRunner

            try:
                return RemoteWorkerRunner(definition, settings).capture(payload)
            except Exception:
                if not settings.worker_fallback_local:
                    raise
                definition = definition.model_copy(update={"host": None})  # W3: run locally
        runner_cls = resolve_runner_class(definition.kind)
        runner = runner_cls(definition, settings)
        capture = getattr(runner, "capture", None)
        if capture is None:
            raise RuntimeError(f"Runner kind '{definition.kind}' does not support capture.")
        return capture(payload)


_BUILTIN_RUNNERS: Dict[str, Type[BaseRunner]] = {
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
for _kind, _cls in _BUILTIN_RUNNERS.items():
    RunnerRegistry.register(_kind, _cls)


# ---------------------------------------------------------------------------
# SecretsRegistry — mirrors RunnerRegistry above, but for secrets backends
# (Phase 19 Sprint 1). Concrete builtin backend implementations live in
# hivepilot.services.secrets_service (which imports SecretsRegistry from this
# module and performs its own `_BUILTIN_SECRETS` registration loop there) so
# that this module never has to import hivepilot.services.secrets_service —
# avoiding a circular import while keeping the registration mechanism
# co-located with RunnerRegistry.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SecretRef:
    """A parsed secret spec: which backend resolves it, and its raw spec dict."""

    source: str
    spec: Dict[str, Any]


class SecretsBackend(Protocol):
    """Structural interface for a secrets backend (env/file/vault/sops/...)."""

    def resolve(self, ref: SecretRef, settings: Settings) -> str: ...


SECRETS_MAP: Dict[str, SecretsBackend] = {}


class SecretsBackendCollisionError(RuntimeError):
    pass


class SecretsRegistry:
    @staticmethod
    def register(name: str, backend: SecretsBackend, *, override: bool = False) -> None:
        if name in SECRETS_MAP and SECRETS_MAP[name] is not backend and not override:
            raise SecretsBackendCollisionError(
                f"Secrets backend '{name}' is already registered to "
                f"{type(SECRETS_MAP[name]).__name__}; refusing to silently "
                f"replace it with {type(backend).__name__}"
            )
        SECRETS_MAP[name] = backend

    @staticmethod
    def known_kinds() -> frozenset[str]:
        return frozenset(SECRETS_MAP)


KNOWN_SECRET_BACKENDS: tuple[str, ...] = ("env", "file", "vault", "sops")
