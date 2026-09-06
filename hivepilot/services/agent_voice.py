"""Agent voice (HP-49 slice 2) — the runner-backed intelligence that makes a
role actually reply.

Registers a reply generator into the Espaces dépose/relève loop (HP-46) and a
subagent executor into the delegation primitives (HP-48): both voice a role by
running its OWN runner + model + prompt (the same `capture_definition` path the
concierge uses) against the conversation. Fail-closed — a model that can't be
reached returns None (no reply), never crashes the loop.

The runner invocation is behind a `capture` seam so the wiring is unit-testable
without a live model; `register()` defaults it to the shared orchestrator's
registry.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, cast

from hivepilot import roles
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

#: (runner_def, payload) -> raw model text.
Capture = Callable[[Any, Any], str]

#: How many trailing messages of the thread to feed the model.
_MAX_THREAD = 20


def _default_capture(runner_def: Any, payload: Any) -> str:
    from hivepilot.services.chatops_service import _get_orchestrator

    return _get_orchestrator().registry.capture_definition(runner_def, payload)


def _thread_text(thread: list[dict]) -> str:
    lines = []
    for message in thread[-_MAX_THREAD:]:
        if message.get("sender_type") == "human":
            who = "Human"
        else:
            who = message.get("sender_id") or message.get("sender_type") or "agent"
        lines.append(f"{who}: {message.get('body', '')}")
    return "\n".join(lines)


def _run_role(role: str, prompt: str, capture: Capture) -> str | None:
    """Run `role`'s own runner+model+prompt against `prompt` and return the
    stripped text. Fail-closed → None (unknown role, unresolvable runner, or any
    capture error)."""
    from hivepilot.models import ProjectConfig, RunnerDefinition, RunnerKind, TaskStep
    from hivepilot.runners.base import RunnerPayload

    try:
        role_obj = roles.get_role(role)
    except KeyError:
        return None
    try:
        kind, model, _effort = roles.resolve_runner(role)
    except Exception as exc:  # noqa: BLE001 — unresolvable runner fails closed
        logger.warning("agent_voice.resolve_failed", role=role, error=str(exc))
        return None

    try:
        runner_def = RunnerDefinition(
            name=f"voice:{role}",
            kind=cast(RunnerKind, kind),
            model=model,
            options={},
        )
        step = TaskStep(name="reply", runner=kind, prompt_file=str(role_obj.prompt_file))
        payload = RunnerPayload(
            project_name="espaces",
            project=ProjectConfig(path=Path(".")),
            task_name="reply",
            step=step,
            metadata={"extra_prompt": prompt, "prior_context": ""},
            secrets={},
        )
        raw = capture(runner_def, payload)
    except Exception as exc:  # noqa: BLE001 — fail closed, never break the loop
        logger.warning("agent_voice.capture_failed", role=role, error=str(exc))
        return None

    text = (raw or "").strip()
    return text or None


def build_reply(space: dict, role: str, thread: list[dict], *, capture: Capture) -> str | None:
    """Voice `role`'s reply to the latest message in the conversation."""
    prompt = (
        "You are participating in a team chat as this agent. Reply concisely and "
        "in character to the latest message. Conversation so far:\n\n" + _thread_text(thread)
    )
    return _run_role(role, prompt, capture)


def make_generator(
    capture: Capture | None = None,
) -> Callable[[dict, str, list[dict]], "str | None"]:
    cap = capture or _default_capture

    def _generator(space: dict, role: str, thread: list[dict]) -> str | None:
        return build_reply(space, role, thread, capture=cap)

    return _generator


def make_subagent(capture: Capture | None = None) -> Callable[[str, str], "str | None"]:
    cap = capture or _default_capture

    def _subagent(role: str, prompt: str) -> str | None:
        return _run_role(role, prompt, cap)

    return _subagent


def register(capture: Capture | None = None) -> None:
    """Wire the runner-backed voice into the Espaces reply loop (HP-46) and the
    delegation subagent primitive (HP-48). Called at API startup; `capture` is
    injectable for tests."""
    from hivepilot.services import delegation, spaces_responder

    spaces_responder.register_reply_generator(make_generator(capture))
    delegation.register_subagent_executor(make_subagent(capture))
