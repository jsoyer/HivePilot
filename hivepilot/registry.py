from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Protocol, Type, cast

from hivepilot.config import Settings, settings
from hivepilot.models import RunnerDefinition, RunnerKind
from hivepilot.runners.ansible_runner import AnsibleRunner
from hivepilot.runners.base import BaseRunner, RunnerPayload, set_last_usage
from hivepilot.runners.chef_runner import ChefRunner
from hivepilot.runners.claude_runner import ClaudeRunner
from hivepilot.runners.container_runner import ContainerRunner
from hivepilot.runners.cursor_runner import CursorRunner
from hivepilot.runners.helm_runner import HelmRunner
from hivepilot.runners.iac_runner import OpenTofuRunner, PulumiRunner, TerraformRunner
from hivepilot.runners.internal_runner import InternalRunner
from hivepilot.runners.kubectl_runner import KubectlRunner
from hivepilot.runners.kustomize_runner import KustomizeRunner
from hivepilot.runners.langchain_runner import LangChainRunner
from hivepilot.runners.openrouter_runner import OpenRouterRunner
from hivepilot.runners.packer_runner import PackerRunner
from hivepilot.runners.prompt_cli_runner import CodexRunner, VibeRunner
from hivepilot.runners.puppet_runner import PuppetRunner
from hivepilot.runners.salt_runner import SaltRunner
from hivepilot.runners.shell_runner import ShellRunner
from hivepilot.services.agent_checks import AGENT_RUNNER_KINDS

RUNNER_MAP: Dict[str, Type[BaseRunner]] = {}


def active_agent_runner_kinds() -> set[str]:
    """Registered runner kinds that are 'agent' kinds (per AGENT_RUNNER_KINDS).
    Intersection of RUNNER_MAP with the canonical agent-kind set — includes any
    agent plugin that registered itself (gemini/opencode/pi/…), excludes infra
    runners. Used by the fail-closed run_pipeline guard.
    """
    return {kind for kind in RUNNER_MAP if kind in AGENT_RUNNER_KINDS}


class RunnerKindCollisionError(RuntimeError):
    pass


class NoAgentRunnerError(RuntimeError):
    """Raised by PipelineOrchestrator.run_pipeline when NO agent runner kind is
    active in RUNNER_MAP (every built-in agent flag off and no agent plugin
    registered). Fail-closed guard (plugin-arch-overhaul Sprint 01): a pipeline
    with zero agent runners can never make progress, so we refuse to start.
    Message lists only enable-able kind names — never config values or secrets.
    """


class RunnerPluginUnavailableError(RuntimeError):
    """Raised by ``resolve_runner_class`` when *kind* is a KNOWN optional
    agent plugin (gemini/opencode/ollama — see ``_OPTIONAL_AGENT_PLUGIN_KINDS``,
    Sprint 2 of the runner-defaults-plugins-mode PRD) that is NOT currently
    registered in ``RUNNER_MAP``, because either its per-plugin enable flag is
    off or its CLI binary isn't on PATH.

    Deliberately distinct from the plain ``KeyError`` ``resolve_runner_class``
    raises for a genuinely unknown kind: this names the exact enable flag and
    required binary so an operator can fix it immediately (or run
    ``hivepilot plugins health`` / ``hivepilot plugins list`` to diagnose),
    instead of a generic "unknown kind" message that gives no actionable next
    step.
    """


# kind -> (Settings flag name, required CLI binary). Every kind listed here
# was a hardcoded _BUILTIN_RUNNERS entry before Sprint 2 and is now
# registered (or not) by its own plugins/<kind>.py — see that module's
# docstring for the canonical gated-agent-plugin skeleton these all share.
_OPTIONAL_AGENT_PLUGIN_KINDS: Dict[str, tuple[str, str]] = {
    "gemini": ("gemini_enabled", "gemini"),
    "opencode": ("opencode_enabled", "opencode"),
    "ollama": ("ollama_enabled", "ollama"),
    # Sprint 3: new default-on, PATH-gated agent plugins (plugins/pi.py,
    # plugins/qwen_code.py, plugins/kimi_cli.py). Listed here so an inactive
    # one (flag off or binary absent) resolves to the actionable
    # RunnerPluginUnavailableError, not a bare KeyError.
    "pi": ("pi_enabled", "pi"),
    "qwen-code": ("qwen_code_enabled", "qwen"),
    "kimi-cli": ("kimi_cli_enabled", "kimi"),
}


def resolve_runner_class(kind: str) -> Type[BaseRunner]:
    """Look up the runner class registered for *kind* in ``RUNNER_MAP``.

    Raises a clear, descriptive ``KeyError`` naming the unknown kind and the
    currently available registered kinds, instead of the bare ``KeyError``
    callers would otherwise hit on an unguarded ``RUNNER_MAP[kind]``. This
    closes the whole class of "advertised-but-unregistered kind" crash (e.g.
    the historical ``"api"`` orphan — see roadmap Phase 26a) for every
    caller that resolves a kind through this helper.

    For a KNOWN optional agent plugin kind (gemini/opencode/ollama) that
    simply isn't active right now (flag off or binary absent), raises the
    more actionable ``RunnerPluginUnavailableError`` instead — see its
    docstring.
    """
    runner_cls = RUNNER_MAP.get(kind)
    if runner_cls is None:
        if kind in _OPTIONAL_AGENT_PLUGIN_KINDS:
            flag_name, binary = _OPTIONAL_AGENT_PLUGIN_KINDS[kind]
            env_var = f"HIVEPILOT_{flag_name.upper()}"
            raise RunnerPluginUnavailableError(
                f"Runner kind {kind!r} is provided by an optional, PATH-gated "
                f"plugin that is not currently active. Either it was disabled "
                f"(set {env_var}=true to re-enable — default is true) or the "
                f"{binary!r} CLI binary is not on PATH. Run `hivepilot plugins "
                f"health` to check, or `hivepilot plugins list` to see what is "
                f"currently registered."
            )
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
    "container": ContainerRunner,
    "cursor": CursorRunner,
    "vibe": VibeRunner,
    # Sprint 2 (runner-defaults-plugins-mode PRD): the only NEW built-in
    # agent kind — API-only, no CLI binary. gemini/opencode/ollama moved OUT
    # of this dict into gated plugins (plugins/gemini.py / opencode.py /
    # ollama.py); see RunnerPluginUnavailableError above for their
    # resolution-time error and _OPTIONAL_AGENT_PLUGIN_KINDS for the mapping.
    "openrouter": OpenRouterRunner,
    "terraform": TerraformRunner,
    "opentofu": OpenTofuRunner,
    "pulumi": PulumiRunner,
    "kubectl": KubectlRunner,
    "ansible": AnsibleRunner,
    "helm": HelmRunner,
    "kustomize": KustomizeRunner,
    "packer": PackerRunner,
    "salt": SaltRunner,
    "chef": ChefRunner,
    "puppet": PuppetRunner,
}
for _kind, _cls in _BUILTIN_RUNNERS.items():
    if getattr(settings, f"{_kind}_enabled", True):
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
