"""Agent Studio Phase 3 (HP-27): natural-language role authoring.

`draft_role(spec)` asks the concierge/OSS model (same no-tools path as
`concierge_service.route`) to turn a free-text spec into a RoleWrite-shaped
**proposal**. Nothing is persisted — a human admin reviews the draft in the
Pollen builder and saves via the Phase 1 CRUD.

Fail-closed throughout:

- the LLM session is structurally tool-less (`--tools ""` in cli mode; the
  api path has no tools). `permission_mode` is never set.
- a cli invariant miss refuses the call rather than spawning a tool-capable
  session on attacker-controlled spec text.
- `allowed_tools` / `bypassPermissions` in the model JSON are stripped.
- any LLM/transport/parse failure raises `RoleDraftError` — we never invent
  a role we could not validate.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, RunnerKind, TaskStep
from hivepilot.plugins import PluginManager
from hivepilot.registry import RUNNER_MAP
from hivepilot.roles import Role, api_roster, validate_role_fields
from hivepilot.runners.base import RunnerPayload
from hivepilot.services import concierge_service
from hivepilot.services.agent_checks import AGENT_RUNNER_KINDS
from hivepilot.services.profile_service import load_claude_profiles
from hivepilot.services.roster_preset import AGENT_KINDS
from hivepilot.utils.logging import get_logger
from hivepilot.utils.validation import sanitize_prompt

logger = get_logger(__name__)

_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "role_draft.md"
_HARDCODED_FALLBACK_PROMPT_TEXT = (
    "You draft a HivePilot role from a natural-language spec. Respond with "
    "one JSON object: name, title, display_name, model_profile, runner, "
    "model, prompt_text, inputs, outputs, can_block, order. Never include "
    "allowed_tools or permission_mode. This is a proposal, not a save."
)
try:
    _DRAFT_PROMPT_TEXT = _PROMPT_FILE.read_text(encoding="utf-8")
except OSError:
    _DRAFT_PROMPT_TEXT = _HARDCODED_FALLBACK_PROMPT_TEXT

_prompt_fallback_path: str | None = None
_prompt_fallback_lock = threading.Lock()

_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,39}$")
_SLUG_RE = re.compile(r"[^a-z0-9_]+")
_DEFAULT_PROFILE = "architecture"
_DEFAULT_RUNNER = "claude"
_FALLBACK_PROFILES = frozenset(
    {"architecture", "coding", "automation", "hermes-4", "hermes-4-405b"}
)


class RoleDraftError(Exception):
    """The model did not produce a usable draft. Never persist on this path."""


@dataclass(frozen=True)
class RoleDraftResult:
    fields: dict[str, Any]
    lint: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    saved: bool = False


def _resolve_prompt_file() -> str:
    if _PROMPT_FILE.exists():
        return str(_PROMPT_FILE)
    global _prompt_fallback_path
    with _prompt_fallback_lock:
        if _prompt_fallback_path is None or not Path(_prompt_fallback_path).exists():
            logger.warning("role_draft.prompt_file_missing_using_temp_fallback")
            fd, path = tempfile.mkstemp(prefix="hivepilot-role-draft-prompt-", suffix=".md")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(_DRAFT_PROMPT_TEXT)
            _prompt_fallback_path = path
        return _prompt_fallback_path


def _known_profiles() -> set[str]:
    try:
        loaded = set(load_claude_profiles())
    except Exception as exc:  # noqa: BLE001 — lint must not fail open on IO
        logger.warning("role_draft.load_profiles_error", error=str(exc))
        loaded = set()
    return loaded or set(_FALLBACK_PROFILES)


def _known_runners() -> set[str]:
    PluginManager()
    return set(RUNNER_MAP) | set(AGENT_KINDS) | set(AGENT_RUNNER_KINDS)


def _slugify(raw: str, *, fallback: str = "agent") -> str:
    slug = _SLUG_RE.sub("_", (raw or "").strip().lower()).strip("_")
    if slug and slug[0].isdigit():
        slug = f"role_{slug}"
    return slug[:40] if slug else fallback


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
        return [part for part in parts if part]
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            text = str(item).strip()
            if text:
                out.append(_slugify(text, fallback=text) if " " in text else text)
        return out
    return []


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1"}:
            return True
        if lowered in {"false", "no", "0"}:
            return False
    return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _existing_role_names() -> set[str]:
    try:
        return {str(row.get("name")) for row in api_roster() if row.get("name")}
    except Exception as exc:  # noqa: BLE001
        logger.warning("role_draft.roster_error", error=str(exc))
        return set()


def _build_extra_prompt(spec: str) -> str:
    profiles = sorted(_known_profiles())
    runners = sorted(_known_runners() & (set(AGENT_KINDS) | set(AGENT_RUNNER_KINDS))) or [
        _DEFAULT_RUNNER
    ]
    taken = ", ".join(sorted(_existing_role_names())) or "(none)"
    return (
        f"Operator spec:\n{spec}\n\n"
        f"Known model_profiles: {', '.join(profiles)}\n"
        f"Known agent runners: {', '.join(runners)}\n"
        f"Existing role names (pick a different name): {taken}"
    )


def _parse_proposal(raw: str) -> dict[str, Any] | None:
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(concierge_service._strip_code_fence(raw))
    except Exception:  # noqa: BLE001 — fail-closed to None
        return None
    return data if isinstance(data, dict) else None


def _sanitize_proposal(raw: dict[str, Any], *, spec: str) -> tuple[dict[str, Any], list[str]]:
    notes: list[str] = []
    title = str(raw.get("title") or "").strip() or "Untitled role"
    name = str(raw.get("name") or "").strip()
    name = name if _NAME_RE.fullmatch(name) else _slugify(name or title)

    profiles = _known_profiles()
    profile = str(raw.get("model_profile") or "").strip()
    if profile not in profiles:
        notes.append(f"model_profile {profile!r} coerced to {_DEFAULT_PROFILE}")
        profile = _DEFAULT_PROFILE if _DEFAULT_PROFILE in profiles else next(iter(sorted(profiles)))

    runners = _known_runners()
    runner = str(raw.get("runner") or "").strip() or None
    if runner and runner not in runners:
        notes.append(f"runner {runner!r} coerced to {_DEFAULT_RUNNER}")
        runner = _DEFAULT_RUNNER
    if not runner:
        runner = _DEFAULT_RUNNER

    prompt_text = str(raw.get("prompt_text") or "").strip()
    if not prompt_text:
        prompt_text = (
            f"You are {title}. Follow this mission:\n\n{spec}\n\n"
            "Stay inside the declared inputs/outputs. Do not claim tools you "
            "were not granted. A human reviews your work."
        )
        notes.append("prompt_text was missing; synthesized from the spec")

    for forbidden in ("allowed_tools", "permission_mode"):
        if raw.get(forbidden) not in (None, "", [], {}):
            notes.append(f"{forbidden} stripped (fail-closed; human admin decides)")

    model = raw.get("model")
    model_s = str(model).strip() if model not in (None, "") else None
    display = raw.get("display_name")
    display_s = str(display).strip() if display not in (None, "") else None

    fields = {
        "name": name,
        "title": title,
        "display_name": display_s,
        "model_profile": profile,
        "runner": runner,
        "model": model_s,
        "prompt_text": prompt_text,
        "prompt_file": None,
        "inputs": _as_str_list(raw.get("inputs")),
        "outputs": _as_str_list(raw.get("outputs")) or ["report"],
        "can_block": _as_bool(raw.get("can_block"), False),
        "order": _as_int(raw.get("order"), 0),
    }
    return fields, notes


def lint_role_draft(fields: dict[str, Any]) -> list[str]:
    """Validate a proposed role the same way a save would: Role schema,
    known profile/runner, no self-granted dangerous capabilities."""
    errors: list[str] = []
    candidate = {k: v for k, v in fields.items() if v is not None}
    if not (candidate.get("prompt_text") or candidate.get("prompt_file")):
        errors.append("a role needs prompt_text or prompt_file")
    try:
        validate_role_fields(candidate)
    except Exception as exc:  # noqa: BLE001 — surface as lint, not a crash
        errors.append(f"invalid role: {exc}")

    name = str(candidate.get("name") or "")
    if name and not _NAME_RE.fullmatch(name):
        errors.append(f"name {name!r} must be a snake_case identifier")
    if name and name in _existing_role_names():
        errors.append(f"role '{name}' already exists — rename before saving")

    profile = candidate.get("model_profile")
    profiles = _known_profiles()
    if profile and profile not in profiles:
        errors.append(f"unknown model_profile {profile!r}")

    runner = candidate.get("runner")
    if runner and runner not in _known_runners():
        errors.append(f"unknown runner {runner!r}")

    if candidate.get("permission_mode") == "bypassPermissions":
        if not settings.allow_dangerous_role_capabilities:
            errors.append("permission_mode='bypassPermissions' is refused (fail-closed)")
    if candidate.get("allowed_tools"):
        errors.append("allowed_tools is refused on a draft (fail-closed no-tools path)")

    # Drop keys the Role model does not own so a later createRole payload
    # stays honest — lint does not mutate `fields`, it only reports.
    unknown = set(candidate) - set(Role.model_fields) - {"prompt_text"}
    if unknown:
        errors.append(f"unsupported fields: {', '.join(sorted(unknown))}")
    return errors


def _call_no_tools_llm(spec: str) -> str:
    """One-shot concierge-model call. Fail-closed: raises RoleDraftError."""
    model = settings.chatops_concierge_model or concierge_service._DEFAULT_CONCIERGE_MODEL
    mode = concierge_service._resolve_mode()
    options = concierge_service._build_classifier_options(mode)

    # HARD INVARIANT (same as concierge_service.route): untrusted spec text
    # must never reach a tool-capable cli session. Not an `assert` — those
    # disappear under `python -O`.
    if mode == "cli" and options.get("tools") != concierge_service._CLASSIFIER_NO_TOOLS:
        logger.error("role_draft.cli_no_tools_invariant_violated_refusing")
        raise RoleDraftError("refused to call the model with tools enabled")

    runner_def = RunnerDefinition(
        name="role_draft",
        kind=cast(RunnerKind, "claude"),
        model=model,
        options=options,
        timeout_seconds=concierge_service._classifier_timeout_seconds(),
    )
    step = TaskStep(name="role_draft", runner="claude", prompt_file=_resolve_prompt_file())
    payload = RunnerPayload(
        project_name="role_draft",
        project=ProjectConfig(path=Path(".")),
        task_name="role_draft",
        step=step,
        metadata={"extra_prompt": _build_extra_prompt(spec), "prior_context": ""},
        secrets={},
    )
    try:
        orch = concierge_service._get_orchestrator()
        raw = orch.registry.capture_definition(runner_def, payload)
    except Exception as exc:  # noqa: BLE001 — never raise the runner error
        logger.warning("role_draft.classify_error", error=str(exc))
        raise RoleDraftError("the authoring model did not return a draft") from exc
    if not isinstance(raw, str) or not raw.strip():
        raise RoleDraftError("the authoring model returned an empty draft")
    return raw


def draft_role(spec: str) -> RoleDraftResult:
    """Turn *spec* into a linter-checked role proposal. Never writes the store."""
    cleaned = sanitize_prompt(spec or "").strip()
    if not cleaned:
        raise RoleDraftError("empty spec")

    raw = _call_no_tools_llm(cleaned)
    parsed = _parse_proposal(raw)
    if parsed is None:
        raise RoleDraftError("unparseable model output")

    fields, notes = _sanitize_proposal(parsed, spec=cleaned)
    try:
        validate_role_fields(fields)
    except Exception as exc:  # noqa: BLE001
        raise RoleDraftError(f"invalid role skeleton: {exc}") from exc

    lint = lint_role_draft(fields)
    return RoleDraftResult(fields=fields, lint=lint, notes=notes, saved=False)
