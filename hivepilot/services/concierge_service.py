"""Natural-language concierge — classifies a free-text chat message into an
ANSWER / ROUTE / ACTION decision (slice 1: core service).

Opt-in (`settings.chatops_concierge_enabled`, default off). Fail-closed
throughout: any LLM error, timeout, or malformed response degrades to a
friendly `answer` — never a silently-fabricated action. This is a normal
service module (not a plugin file loaded via importlib), so
`@dataclass(frozen=True)` is safe here — the CPython 3.14 dataclass-loader
bug only affects plugin files.

Design notes (deviations from the literal integration-seam sketch, both
required to avoid crashing `ClaudeRunner`):

1. `RunnerPayload.project` is a REAL minimal `ProjectConfig` here, not
   `None`. `ClaudeRunner._build_prompt`/`_run_api` unconditionally read
   `payload.project.path`/`.description`/`.claude_md` in BOTH cli and api
   mode — `Orchestrator.human_challenge`'s `project=None` pattern only
   "works" in this repo because Chief-of-Staff is bound to a non-Claude
   runner (`cursor`) in `roles.yaml`; the concierge always dispatches to
   `kind="claude"`, so it needs a real (if trivial) project.
2. `TaskStep.prompt_file` points at a real, checked-in file
   (`prompts/agents/concierge.md`) with the STABLE classifier instructions
   (output contract + destructive-action table), not `""`.
   `ClaudeRunner._assemble_prompt` raises `ValueError` on an empty
   `prompt_file` in both modes — mirroring the volatile `extra_prompt`-only
   pattern from `human_challenge` would always crash. The stable file also
   lets Anthropic prompt-caching cover the same across every classify call.
   The per-message roster/user-text/grounding snapshot is the VOLATILE part,
   threaded through `metadata["extra_prompt"]` exactly as specced.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, RunnerKind, TaskStep
from hivepilot.runners.base import RunnerPayload
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# Stable classifier instructions (destructive-action table + JSON output
# contract) — see module docstring point 2. Repo-relative:
# <repo_root>/prompts/agents/concierge.md, mirroring `roles._PROMPTS_DIR`.
_PROMPT_FILE = Path(__file__).resolve().parent.parent.parent / "prompts" / "agents" / "concierge.md"

# Sensible cheap/fast default when settings.chatops_concierge_model is unset.
# "haiku" is a recognised model alias in this codebase's automation tier
# (see model_profiles.yaml) — cheap and fast, appropriate for a per-message
# classifier that runs on every free-text chat message when enabled.
_DEFAULT_CONCIERGE_MODEL = "haiku"

_FALLBACK_ANSWER = (
    "I didn't quite get that. Try rephrasing your request, "
    "or use /help to see the available commands."
)

_KNOWN_KINDS = {"answer", "route", "action"}
_KNOWN_ACTIONS = {"run", "run_pipeline", "approve", "deny"}
# Every currently-known route/action kind is destructive per the hardcoded
# table (see `_clamp`) — the concierge OWNS this decision and never trusts
# the model's self-reported `destructive` field as authoritative.


@dataclass(frozen=True)
class ConciergeDecision:
    kind: str  # "answer" | "route" | "action"
    answer_text: str | None = None
    role_key: str | None = None
    target: str | None = None
    order: str | None = None
    action: str | None = None
    params: dict | None = None
    destructive: bool = False


_orchestrator: Any = None
_orchestrator_lock = threading.Lock()


def _get_orchestrator() -> Any:
    """Lazy module-level Orchestrator singleton — mirrors
    `chatops_service._get_orchestrator()` exactly (separate instance: this
    module must stay independently importable and not couple to chatops
    internals)."""
    global _orchestrator
    if _orchestrator is None:
        with _orchestrator_lock:
            if _orchestrator is None:
                from hivepilot.orchestrator import Orchestrator

                _orchestrator = Orchestrator()
    return _orchestrator


# ---------------------------------------------------------------------------
# Roster + grounding snapshot (read-only)
# ---------------------------------------------------------------------------


def _mission_line(prompt_file: Path | None) -> str:
    """Best-effort parse of the one-liner following '## Mission' in
    *prompt_file*. Never raises — returns "" on any error or absence."""
    if not prompt_file:
        return ""
    try:
        path = Path(prompt_file)
        if not path.exists():
            return ""
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001 — best-effort, never block roster building
        return ""
    for i, line in enumerate(lines):
        if line.strip() == "## Mission":
            for next_line in lines[i + 1 :]:
                stripped = next_line.strip()
                if stripped:
                    return stripped
            break
    return ""


def _build_roster() -> list[dict[str, str]]:
    """Human-readable role roster for the classifier prompt: role_key, title,
    display name, and best-effort Mission one-liner. Never raises."""
    from hivepilot.roles import list_roles

    try:
        roles = list_roles()
    except Exception as exc:  # noqa: BLE001
        logger.warning("concierge.roster_build_error", error=str(exc))
        return []

    roster: list[dict[str, str]] = []
    for role in roles:
        try:
            mission = _mission_line(getattr(role, "prompt_file", None))
            roster.append(
                {
                    "role_key": role.name,
                    "title": role.title,
                    "display": role.display_name or role.name,
                    "mission": mission,
                }
            )
        except Exception as exc:  # noqa: BLE001 — one bad role entry must not drop the roster
            logger.warning("concierge.roster_entry_error", error=str(exc))
    return roster


def _known_projects() -> set[str] | None:
    """Return the set of known project names, or None if the project list
    could not be loaded (validation is then skipped, not fail-open on
    execution — the downstream orchestrator call still validates for real)."""
    from hivepilot.services.project_service import load_projects

    try:
        return set(load_projects().projects.keys())
    except Exception as exc:  # noqa: BLE001
        logger.warning("concierge.load_projects_error", error=str(exc))
        return None


def _grounding_snapshot() -> str:
    """Short read-only snapshot (recent runs + pending approvals) so the
    classifier can ground ANSWER / approve / deny requests. Never raises."""
    from hivepilot.services import state_service

    lines: list[str] = []
    try:
        for r in state_service.list_recent_runs(limit=5):
            lines.append(
                f"run: [{r.get('status')}] {r.get('project')}/{r.get('task')} "
                f"@ {r.get('started_at')}"
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("concierge.list_recent_runs_error", error=str(exc))
    try:
        for a in state_service.get_pending_approvals():
            lines.append(
                f"pending_approval: run_id={a.get('run_id')} "
                f"project={a.get('project')} task={a.get('task')}"
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("concierge.get_pending_approvals_error", error=str(exc))
    return "\n".join(lines) if lines else "(no recent runs or pending approvals)"


def _build_classifier_prompt(text: str, roster: list[dict[str, str]], snapshot: str) -> str:
    roster_lines = (
        "\n".join(
            f"- {r['role_key']}: {r['display']} ({r['title']}) — "
            f"{r['mission'] or 'no mission on file'}"
            for r in roster
        )
        or "(no roles configured on this deployment)"
    )
    return (
        f"User message: {text}\n\nAvailable roles:\n{roster_lines}\n\nRecent context:\n{snapshot}"
    )


# ---------------------------------------------------------------------------
# JSON parsing (fail-closed)
# ---------------------------------------------------------------------------


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if "\n" in text:
            first, rest = text.split("\n", 1)
            if first.strip().lower() in ("json", ""):
                text = rest
    return text.strip()


def _parse_raw(raw: str) -> ConciergeDecision | None:
    """Strictly parse *raw* as the classifier's JSON contract. Returns None
    on ANY parse failure or unrecognised `kind`/`action` — callers must
    treat None as fail-closed (degrade to a friendly answer)."""
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(_strip_code_fence(raw))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    kind = data.get("kind")
    if kind not in _KNOWN_KINDS:
        return None

    if kind == "answer":
        answer_text = data.get("answer_text")
        if not isinstance(answer_text, str) or not answer_text.strip():
            answer_text = _FALLBACK_ANSWER
        return ConciergeDecision(kind="answer", answer_text=answer_text)

    if kind == "route":
        role_key = data.get("role_key")
        target = data.get("target")
        order = data.get("order")
        return ConciergeDecision(
            kind="route",
            role_key=role_key if isinstance(role_key, str) else None,
            target=target if isinstance(target, str) else None,
            order=order if isinstance(order, str) else None,
        )

    # kind == "action"
    action = data.get("action")
    if action not in _KNOWN_ACTIONS:
        return None
    params = data.get("params")
    if not isinstance(params, dict):
        params = None
    target = data.get("target")
    return ConciergeDecision(
        kind="action",
        action=action,
        target=target if isinstance(target, str) else None,
        params=params,
    )


def _unknown_role_answer(known_roles: set[str]) -> str:
    if known_roles:
        names = ", ".join(sorted(known_roles))
        return f"I don't recognise that agent. Available agents: {names}. Try /help."
    return "No agents are configured on this deployment yet. Try /help."


def _unknown_target_answer(known_projects: set[str]) -> str:
    if known_projects:
        names = ", ".join(sorted(known_projects))
        return f"I don't recognise that project. Known projects: {names}. Try /projects."
    return "No projects are configured on this deployment yet. Try /projects."


def _clamp(
    decision: ConciergeDecision, *, default_role: str, default_target: str | None
) -> ConciergeDecision:
    """Validate/clamp a parsed decision against what's actually known
    (roster + projects), substitute defaults, and hardcode `destructive`
    (the concierge OWNS this — never trusts the model's self-reported
    value as authoritative for a kind/action already in the table)."""
    if decision.kind == "answer":
        return decision

    from hivepilot.roles import list_roles

    try:
        known_roles = {r.name for r in list_roles()}
    except Exception as exc:  # noqa: BLE001
        logger.warning("concierge.clamp_list_roles_error", error=str(exc))
        known_roles = set()
    known_projects = _known_projects()

    if decision.kind == "route":
        role_key = decision.role_key or default_role
        if role_key not in known_roles:
            return ConciergeDecision(kind="answer", answer_text=_unknown_role_answer(known_roles))
        target = decision.target or default_target
        if target is not None and known_projects is not None and target not in known_projects:
            return ConciergeDecision(
                kind="answer", answer_text=_unknown_target_answer(known_projects)
            )
        return ConciergeDecision(
            kind="route",
            role_key=role_key,
            target=target,
            order=decision.order or "",
            destructive=True,
        )

    # kind == "action"
    if decision.action not in _KNOWN_ACTIONS:
        return ConciergeDecision(kind="answer", answer_text=_FALLBACK_ANSWER)

    if decision.action in ("approve", "deny"):
        params = decision.params or {}
        if "run_id" not in params:
            return ConciergeDecision(
                kind="answer",
                answer_text=(
                    "I need a run id to approve or deny — check /approvals for pending runs."
                ),
            )
        return ConciergeDecision(
            kind="action", action=decision.action, params=params, destructive=True
        )

    # run / run_pipeline
    target = decision.target or default_target
    if target is not None and known_projects is not None and target not in known_projects:
        return ConciergeDecision(kind="answer", answer_text=_unknown_target_answer(known_projects))
    return ConciergeDecision(
        kind="action",
        action=decision.action,
        target=target,
        params=decision.params or {},
        destructive=True,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def route(text: str, *, default_role: str, default_target: str | None) -> ConciergeDecision:
    """Classify *text* into an ANSWER / ROUTE / ACTION decision.

    Fail-closed: any LLM error, timeout, malformed response, or reference to
    an unknown role/project degrades to a friendly `answer` — this function
    NEVER fabricates a route/action it cannot validate. Synchronous/blocking
    (one LLM call) — callers on an event loop must run it in an executor.
    """
    roster = _build_roster()
    snapshot = _grounding_snapshot()
    prompt = _build_classifier_prompt(text, roster, snapshot)

    model = settings.chatops_concierge_model or _DEFAULT_CONCIERGE_MODEL
    runner_def = RunnerDefinition(
        name="concierge",
        kind=cast(RunnerKind, "claude"),
        model=model,
        options={"mode": "api"},
    )
    prompt_file = str(_PROMPT_FILE) if _PROMPT_FILE.exists() else ""
    step = TaskStep(name="concierge", runner="claude", prompt_file=prompt_file)
    payload = RunnerPayload(
        project_name="concierge",
        project=ProjectConfig(path=Path(".")),
        task_name="concierge",
        step=step,
        metadata={"extra_prompt": prompt, "prior_context": ""},
        secrets={},
    )

    try:
        orch = _get_orchestrator()
        raw = orch.registry.capture_definition(runner_def, payload)
    except Exception as exc:  # noqa: BLE001 — fail closed, never raise to the caller
        logger.warning("concierge.classify_error", error=str(exc))
        return ConciergeDecision(kind="answer", answer_text=_FALLBACK_ANSWER)

    parsed = _parse_raw(raw)
    if parsed is None:
        return ConciergeDecision(kind="answer", answer_text=_FALLBACK_ANSWER)

    return _clamp(parsed, default_role=default_role, default_target=default_target)
