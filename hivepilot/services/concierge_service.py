"""Natural-language concierge — classifies a free-text chat message into an
ANSWER / ROUTE / ACTION decision (slice 1: core service).

Opt-in (`settings.chatops_concierge_enabled`, default off). Fail-closed
throughout: any LLM error, timeout, or malformed response degrades to a
friendly `answer` — never a silently-fabricated action. This is a normal
service module (not a plugin file loaded via importlib), so
`@dataclass(frozen=True)` is safe here — the CPython 3.14 dataclass-loader
bug only affects plugin files.

Two deliberate deviations from the literal integration-seam sketch, both
required because `ClaudeRunner` (and, in fact, every runner — see
`hivepilot.orchestrator._synthetic_project`) reads them unconditionally in
CLI *and* API mode:

1. `RunnerPayload.project` is a real minimal `ProjectConfig`, not `None`
   (`_build_prompt`/`_run_api` read `payload.project.path` unconditionally).
2. `TaskStep.prompt_file` points at a real, packaged file
   (`hivepilot/prompts/concierge.md`, the STABLE classifier instructions —
   output contract + destructive-action table), not `""`
   (`_assemble_prompt` raises `ValueError` on an empty `prompt_file`). The
   per-message roster/user-text/grounding snapshot is the VOLATILE part,
   threaded through `metadata["extra_prompt"]` as specced.

PACKAGING NOTE: this prompt lives INSIDE the `hivepilot` package
(`hivepilot/prompts/concierge.md`), unlike the user-editable role prompts in
the top-level `prompts/agents/` tree (`roles._PROMPTS_DIR`), which is seeded
by the installer and deliberately NOT shipped by pip
(`[tool.setuptools.packages.find]` only includes `hivepilot*`). The
classifier prompt is an internal, code-owned instruction set — not something
an operator is meant to edit — so it MUST ship inside the wheel or every
pip-installed box silently fails-closed on every chat message (see
`_PROMPT_FILE` and `_resolve_prompt_file` below for the belt-and-suspenders
fallback in case this ever regresses).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, RunnerKind, TaskStep
from hivepilot.runners.base import RunnerPayload
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# Stable classifier instructions (destructive-action table + JSON output
# contract) — see module docstring point 2 and the PACKAGING NOTE above.
# Package-relative (NOT repo-relative): `hivepilot/prompts/concierge.md`,
# `parent.parent` from this file (`hivepilot/services/concierge_service.py`)
# is the `hivepilot/` package dir — this resolves correctly both in a repo
# checkout AND inside an installed wheel, because `hivepilot/prompts/*.md`
# is declared in `[tool.setuptools.package-data]`.
_PROMPT_FILE = Path(__file__).resolve().parent.parent / "prompts" / "concierge.md"

# Minimal, hardcoded last-resort fallback — only used if reading the
# packaged `_PROMPT_FILE` at import time also fails (should never happen in
# a correctly built wheel; this exists purely so `_CLASSIFIER_PROMPT_TEXT`
# is never empty, which would defeat the point of the temp-file fallback in
# `_resolve_prompt_file`).
_HARDCODED_FALLBACK_PROMPT_TEXT = (
    "You are the HivePilot concierge classifier. Read the user's message "
    "and the roster/context supplied via extra_prompt, then respond with a "
    'single JSON object: {"kind": "answer"|"route"|"action", '
    '"answer_text": str|null, "role_key": str|null, "target": '
    'str|null, "order": str|null, "action": str|null, "params": '
    'object|null}. If uncertain, always respond with kind="answer".'
)

# Read the packaged prompt file ONCE at import time so `_resolve_prompt_file`
# never needs to touch disk on the hot path when `_PROMPT_FILE` exists (the
# common case). Read from the SAME `_PROMPT_FILE` used at call time — never
# hand-maintain a second copy of the text, which could silently drift from
# the checked-in file.
try:
    _CLASSIFIER_PROMPT_TEXT = _PROMPT_FILE.read_text(encoding="utf-8")
except OSError:
    _CLASSIFIER_PROMPT_TEXT = _HARDCODED_FALLBACK_PROMPT_TEXT

# Cached path to a temp-file copy of `_CLASSIFIER_PROMPT_TEXT`, created lazily
# (once) by `_resolve_prompt_file` only if `_PROMPT_FILE` is ever missing at
# call time. Guarded by `_prompt_fallback_lock` like `_orchestrator` below.
_prompt_fallback_path: str | None = None
_prompt_fallback_lock = threading.Lock()


def _resolve_prompt_file() -> str:
    """Return a path to the classifier prompt that is GUARANTEED to exist
    and be non-empty — never `""` (an empty `prompt_file` makes
    `ClaudeRunner` raise, which is exactly the packaging bug this guards
    against; see module PACKAGING NOTE). Prefers the packaged
    `_PROMPT_FILE`; falls back to a cached temp-file copy of
    `_CLASSIFIER_PROMPT_TEXT` (read from that same packaged file at import
    time) if `_PROMPT_FILE` is somehow missing at runtime."""
    if _PROMPT_FILE.exists():
        return str(_PROMPT_FILE)

    global _prompt_fallback_path
    with _prompt_fallback_lock:
        if _prompt_fallback_path is None or not Path(_prompt_fallback_path).exists():
            logger.warning("concierge.prompt_file_missing_using_temp_fallback")
            fd, path = tempfile.mkstemp(prefix="hivepilot-concierge-prompt-", suffix=".md")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(_CLASSIFIER_PROMPT_TEXT)
            _prompt_fallback_path = path
        return _prompt_fallback_path


# Sensible cheap/fast default when settings.chatops_concierge_model is unset.
# "haiku" is a recognised model alias in this codebase's automation tier
# (see model_profiles.yaml) — cheap and fast, appropriate for a per-message
# classifier that runs on every free-text chat message when enabled.
_DEFAULT_CONCIERGE_MODEL = "haiku"

# Env var ClaudeRunner._run_api reads the Anthropic API key from (via
# merge_environments, which starts from os.environ.copy()). The concierge's
# own RunnerDefinition/RunnerPayload never set project.env/definition.env/
# secrets, so checking os.environ directly here mirrors EXACTLY what
# _run_api would see for this call — no separate resolution path to drift.
_API_KEY_ENV_VAR = "ANTHROPIC_API_KEY"

# SECURITY (cli mode only — the api path never has tool access at all, see
# `_run_api`, so this doesn't apply there): the classifier prompt embeds
# UNTRUSTED, attacker-controlled input — the free-text chat message being
# classified — inside a live `claude` session. A crafted message is a
# prompt-injection vector: if that session had ANY tool available, an
# injected instruction could make it invoke Bash/Edit/WebFetch/etc, which is
# remote code execution triggered by a chat message, with no confirmation
# gate in front of it (the confirm/approval flow only runs AFTER this call
# returns a *parsed* decision — it cannot protect against something that
# already executed inside the subprocess).
#
# The fix is structural, not permission-based: `--tools ""` (see
# `hivepilot.runners.claude_runner.ClaudeRunner._resolve_tools`) makes NO
# tools available to the session — not merely gated behind a permission
# prompt, but absent from the tool set entirely, so there is nothing for an
# injected instruction to invoke. Because nothing can ever require
# approval, no `--permission-mode` is needed either (there is nothing to
# prompt for) — `bypassPermissions` (or any other permission_mode) MUST
# NEVER be set on this path; that would grant exactly the blanket tool
# authority this no-tools restriction exists to deny. Use the runner's
# ordinary default permission-mode resolution (i.e. don't touch it here).
_CLASSIFIER_NO_TOOLS = ""  # claude --tools "": "Use \"\" to disable all tools" (claude --help)

# Per-call ceiling so a hung/slow `claude` CLI (or a slow API response)
# degrades to the fail-closed answer instead of blocking the chat bot
# process indefinitely — this is a per-message classification, not a
# multi-minute agent task.
_CLASSIFIER_TIMEOUT_SECONDS = 30

# Reserved for genuinely empty/unparseable model output ONLY (LLM error,
# timeout, malformed JSON, or a malformed kind/action with no salvageable
# `answer_text` alongside it — see `_salvageable_answer_text`). A substantive
# question the model DID understand must get a genuine LLM answer instead of
# this generic filler — see `hivepilot/prompts/concierge.md`'s "Deciding the
# kind" section for the classifier-side half of this contract.
_FALLBACK_ANSWER = (
    "I didn't quite get that. Try rephrasing your request, "
    "or use /help to see the available commands."
)

_KNOWN_KINDS = {"answer", "route", "action", "multi_route"}
_KNOWN_ACTIONS = {"run", "run_pipeline", "approve", "deny"}
# Every currently-known route/action/multi_route kind is destructive per the
# hardcoded table (see `_clamp`) — the concierge OWNS this decision and never
# trusts the model's self-reported `destructive` field as authoritative.


@dataclass(frozen=True)
class DispatchOrder:
    """One agent's order within a `multi_route` batch — see
    `ConciergeDecision.dispatches`. Each entry is independently `_clamp`-
    validated (known role + known project) AND grounding-checked (the role
    must have actually been named somewhere in this chat's recent
    conversation history) before it ever reaches confirmation."""

    role_key: str
    target: str | None
    order: str


@dataclass(frozen=True)
class ConciergeDecision:
    kind: str  # "answer" | "route" | "action" | "multi_route"
    answer_text: str | None = None
    role_key: str | None = None
    target: str | None = None
    order: str | None = None
    action: str | None = None
    params: dict | None = None
    destructive: bool = False
    # Only for kind="multi_route" — one order per agent in the batch. A
    # single explicit human confirmation gates the WHOLE batch (see
    # telegram_bot._send_concierge_keyboard_message /
    # _execute_concierge_decision) — no partial auto-run.
    dispatches: list[DispatchOrder] | None = None
    # Only meaningful for `kind="answer"`: a STRUCTURED, executable next step
    # the classifier proposes alongside its prose answer ("groomer-scan
    # failed. Want me to investigate?"). See the "Pending follow-up offers"
    # section below — `route()` consumes this field, validates it through the
    # same `_clamp` as any other decision, and either turns it into a pending
    # offer (rendering its OWN invitation line) or drops it silently. It is
    # never returned to a caller: callers only ever see the answer, or, once
    # the operator affirms, the executable decision itself.
    follow_up: ConciergeDecision | None = None


def _get_orchestrator() -> Any:
    """Reuse the process-wide shared Orchestrator (chatops_service's
    singleton, the same one telegram_bot._get_orch uses). Building a SECOND
    Orchestrator here spawns a second PluginManager that re-scans plugins
    and collides on the process-global RUNNER_MAP/NOTIFIER_MAP/SECRETS_MAP —
    see orchestrator.py's `_load()` comment. This was the root cause of the
    production regression `telegram.cmd_ask.error`: "Runner kind 'gh' is
    already registered to GhRunner; refusing to silently replace it with
    GhRunner" — the Telegram process holds a shared Orchestrator (used by
    both telegram_bot and chatops_service) plus this module's OWN separate
    one, so the second PluginManager scan collided with the first. A lazy
    function-level import keeps this module independently importable
    without a hard module-level coupling to chatops."""
    from hivepilot.services.chatops_service import _get_orchestrator as _shared

    return _shared()


# ---------------------------------------------------------------------------
# Execution mode resolution — api (Anthropic Messages API) vs cli (the
# operator's local `claude` CLI, subscription/OAuth-authenticated).
# ---------------------------------------------------------------------------

# Logged once (not per-message) so an always-on classifier on a
# subscription-only box doesn't spam INFO on every chat message.
_cli_fallback_logged = False
_cli_fallback_lock = threading.Lock()


def _has_api_key() -> bool:
    """True if an Anthropic API key is present in the environment the
    classifier's `claude` call would actually use — see `_API_KEY_ENV_VAR`."""
    return bool(os.environ.get(_API_KEY_ENV_VAR))


def _resolve_mode() -> str:
    """Resolve the effective execution mode for the classifier's one-shot
    `claude` invocation.

    `settings.chatops_concierge_mode` ("api" default, or "cli") is the
    configured mode. When it resolves to "api" but no `ANTHROPIC_API_KEY` is
    present, this AUTO-FALLS-BACK to "cli" so a subscription/OAuth-only
    deployment (no API key at all) works out of the box via the operator's
    local `claude` CLI instead of always hitting the fail-closed fallback
    answer. An explicit "cli" always stays "cli" regardless of key presence.
    """
    global _cli_fallback_logged
    configured = (settings.chatops_concierge_mode or "api").strip().lower()
    if configured not in ("api", "cli"):
        logger.warning("concierge.unknown_mode_defaulting_to_api", mode=configured)
        configured = "api"
    if configured == "api" and not _has_api_key():
        if not _cli_fallback_logged:
            with _cli_fallback_lock:
                if not _cli_fallback_logged:
                    logger.info(
                        "concierge: no ANTHROPIC_API_KEY, using claude CLI for classification"
                    )
                    _cli_fallback_logged = True
        return "cli"
    return configured


def _build_classifier_options(mode: str) -> dict[str, Any]:
    """Build the `RunnerDefinition.options` for the classifier's `claude`
    call given the resolved *mode*.

    A separate, independently-testable function (not inlined into `route()`)
    so `route()`'s hard no-tools invariant check (see its body) is checking
    this function's ACTUAL output rather than trusting that a shared flag
    was set correctly — a genuine regression here (e.g. a future edit that
    drops the `tools` assignment) is caught by that check rather than
    silently producing a tool-capable cli session on untrusted input.
    """
    options: dict[str, Any] = {"mode": mode}
    if mode == "cli":
        # See `_CLASSIFIER_NO_TOOLS` above — untrusted chat text reaches this
        # session, so it must NEVER have tool access. Deliberately NOT
        # setting permission_mode here (in particular, never
        # "bypassPermissions") — with no tools available there is nothing to
        # gate behind a permission mode in the first place.
        options["tools"] = _CLASSIFIER_NO_TOOLS
    return options


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
    classifier can ground ANSWER / approve / deny requests. Never raises.

    ``started_at`` is rendered via ``display_time.to_display`` (local,
    marked) rather than interpolated raw — the classifier's NL answer often
    echoes this snapshot back to the operator verbatim (e.g. "failed this
    morning at 09:08"), so a raw UTC value here reproduces the exact
    production incident this fixes at the LLM-prompt layer.
    """
    from hivepilot.services import state_service
    from hivepilot.utils import display_time

    lines: list[str] = []
    try:
        for r in state_service.list_recent_runs(limit=5):
            lines.append(
                f"run: [{r.get('status')}] {r.get('project')}/{r.get('task')} "
                f"@ {display_time.to_display(r.get('started_at'))}"
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


def _build_classifier_prompt(
    text: str, roster: list[dict[str, str]], snapshot: str, history_text: str
) -> str:
    roster_lines = (
        "\n".join(
            f"- {r['role_key']}: {r['display']} ({r['title']}) — "
            f"{r['mission'] or 'no mission on file'}"
            for r in roster
        )
        or "(no roles configured on this deployment)"
    )
    return (
        f"User message: {text}\n\n"
        f"Available roles:\n{roster_lines}\n\n"
        f"Recent context:\n{snapshot}\n\n"
        f"Recent conversation (this chat only):\n{history_text}"
    )


# ---------------------------------------------------------------------------
# Conversation memory — a short, bounded, per-chat rolling history (BOTH the
# user's messages and the concierge's own answers) so a follow-up message
# ("give them the orders", "do it", "them") can be resolved against what was
# just discussed. In-process only (a bounded dict, deque-per-chat) — the
# simplest storage that survives within a single running process, which is
# all that's needed here: the concierge's own classifier call is itself
# per-process (see `_get_orchestrator`'s shared-Orchestrator rationale), and
# nothing about routing/dispatch depends on this memory surviving a restart.
# Chat-scoped by construction (keyed by `chat_id`) — never merged or looked
# up across chats, so one tenant/chat's history can never leak into or
# resolve referents for another (see `_clamp`'s grounding check below, which
# only ever consults the CURRENT chat's history text).
# ---------------------------------------------------------------------------

_MAX_HISTORY_TURNS = 6


@dataclass(frozen=True)
class _Turn:
    user_text: str
    concierge_text: str


_history: dict[int, deque[_Turn]] = {}
_history_lock = threading.Lock()


def _record_turn(chat_id: int, user_text: str, concierge_text: str) -> None:
    """Append one turn (user message + the concierge's own reply/summary) to
    *chat_id*'s bounded history. Never raises."""
    with _history_lock:
        buf = _history.setdefault(chat_id, deque(maxlen=_MAX_HISTORY_TURNS))
        buf.append(_Turn(user_text=user_text, concierge_text=concierge_text))


def _get_history(chat_id: int | None) -> list[_Turn]:
    """Return a COPY of *chat_id*'s recent turns (oldest first), or `[]` if
    *chat_id* is None or has no recorded history. Chat-scoped: only ever
    returns entries recorded under the exact same key."""
    if chat_id is None:
        return []
    with _history_lock:
        buf = _history.get(chat_id)
        return list(buf) if buf else []


def clear_history(chat_id: int | None = None) -> None:
    """Ops/test helper: clear conversation memory for one chat, or every
    chat when *chat_id* is None."""
    with _history_lock:
        if chat_id is None:
            _history.clear()
        else:
            _history.pop(chat_id, None)


def _format_history(history: list[_Turn]) -> str:
    if not history:
        return "(no prior conversation this session)"
    lines: list[str] = []
    for turn in history:
        lines.append(f"User: {turn.user_text}")
        lines.append(f"Concierge: {turn.concierge_text}")
    return "\n".join(lines)


def _history_summary(decision: ConciergeDecision) -> str:
    """Compact textual summary of *decision*, suitable for storing as the
    "concierge" side of a recorded turn — used as the grounding text a
    LATER turn's `multi_route` dispatches are checked against (see
    `_clamp`). For `kind="answer"` this is simply the genuine answer text
    (the common case: the concierge names agents/roles in its own answer,
    which a follow-up like "give them the orders" then resolves)."""
    if decision.kind == "answer":
        return decision.answer_text or ""
    if decision.kind == "route":
        return f"[dispatched {decision.role_key} on {decision.target}: {decision.order}]"
    if decision.kind == "multi_route":
        parts = [f"{d.role_key} on {d.target}: {d.order}" for d in (decision.dispatches or [])]
        return "[dispatched " + "; ".join(parts) + "]" if parts else "[dispatched nothing]"
    if decision.kind == "action":
        return f"[action {decision.action} on {decision.target}]"
    return ""


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


def _salvageable_answer_text(data: dict[str, Any]) -> str | None:
    """Return a usable `answer_text` from *data* if one is present and
    non-blank, else None.

    Used when the model's top-level `kind` or `action` is malformed/
    unrecognised: a model sometimes still engages with a substantive
    question — it "understood", it just reached for the wrong `kind`/
    `action` name (e.g. a made-up `action: "plan"` for a strategy question
    that isn't one of the four real actions) — and may defensively populate
    `answer_text` alongside that mistake anyway. Discarding a genuine answer
    the model already wrote, in favour of the generic dismissive fallback,
    is exactly the UX gap this module exists to avoid — so salvage it
    rather than nuke it. This does NOT weaken fail-closed behaviour: the
    result is still always `kind="answer"` (never a route/action), and
    truly empty/unparseable output still falls through to `None` (the
    caller's `_FALLBACK_ANSWER` degrade) unchanged."""
    answer_text = data.get("answer_text")
    if isinstance(answer_text, str) and answer_text.strip():
        return answer_text
    return None


def _parse_raw(raw: str) -> ConciergeDecision | None:
    """Strictly parse *raw* as the classifier's JSON contract. Returns None
    on ANY parse failure or unrecognised `kind`/`action` with no salvageable
    `answer_text` alongside it — callers must treat None as fail-closed
    (degrade to the generic fallback answer). An unrecognised `kind`/
    `action` that DOES carry real `answer_text` degrades to a genuine
    `kind="answer"` decision using that text instead (see
    `_salvageable_answer_text`) — reserving the generic fallback for
    genuinely empty/unparseable model output.

    An `answer` may additionally carry a `follow_up` object (see
    `_parse_follow_up`) — the structured form of "want me to investigate?".
    """
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(_strip_code_fence(raw))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    decision = _parse_decision_obj(data)
    if decision is None or decision.kind != "answer":
        return decision
    follow_up = _parse_follow_up(data)
    return decision if follow_up is None else replace(decision, follow_up=follow_up)


def _parse_follow_up(data: dict[str, Any]) -> ConciergeDecision | None:
    """Parse `data["follow_up"]` — the classifier's proposed next step — into
    an EXECUTABLE decision, or None.

    Fail-closed by construction: anything that is not a well-formed
    route/action/multi_route object is dropped (a missing, non-object, or
    `answer`-shaped follow-up is not an offer at all). A follow-up nested
    inside a follow-up is ignored — `_parse_decision_obj` never reads the
    field, so offers can never chain."""
    raw_follow_up = data.get("follow_up")
    if not isinstance(raw_follow_up, dict):
        return None
    parsed = _parse_decision_obj(raw_follow_up)
    if parsed is None or parsed.kind == "answer":
        return None
    return parsed


def _parse_decision_obj(data: dict[str, Any]) -> ConciergeDecision | None:
    """Parse one already-decoded decision object (the top-level classifier
    response, or a nested `follow_up`) — see `_parse_raw` for the contract."""
    kind = data.get("kind")
    if kind not in _KNOWN_KINDS:
        salvaged = _salvageable_answer_text(data)
        return ConciergeDecision(kind="answer", answer_text=salvaged) if salvaged else None

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

    if kind == "multi_route":
        raw_dispatches = data.get("dispatches")
        if not isinstance(raw_dispatches, list):
            salvaged = _salvageable_answer_text(data)
            return ConciergeDecision(kind="answer", answer_text=salvaged) if salvaged else None
        parsed_dispatches: list[DispatchOrder] = []
        for item in raw_dispatches:
            if not isinstance(item, dict):
                continue
            item_role_key = item.get("role_key")
            if not isinstance(item_role_key, str) or not item_role_key.strip():
                # Never guess a missing role — drop this entry, not the batch.
                continue
            item_target = item.get("target")
            item_order = item.get("order")
            parsed_dispatches.append(
                DispatchOrder(
                    role_key=item_role_key,
                    target=item_target if isinstance(item_target, str) else None,
                    order=item_order if isinstance(item_order, str) else "",
                )
            )
        if not parsed_dispatches:
            salvaged = _salvageable_answer_text(data)
            return ConciergeDecision(kind="answer", answer_text=salvaged) if salvaged else None
        return ConciergeDecision(kind="multi_route", dispatches=parsed_dispatches)

    # kind == "action"
    action = data.get("action")
    if action not in _KNOWN_ACTIONS:
        salvaged = _salvageable_answer_text(data)
        return ConciergeDecision(kind="answer", answer_text=salvaged) if salvaged else None
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


# ---------------------------------------------------------------------------
# Pending follow-up offers
#
# THE DEFECT THIS FIXES: the classifier's `answer_text` is free-form prose, so
# the model could end an answer with "Want me to investigate?" — an invitation
# the dispatcher had no record of and no way to honour. The operator replied
# "yes" and got `_FALLBACK_ANSWER` ("I didn't quite get that"). Inviting a
# reply and then not understanding it is worse than never offering.
#
# The fix is structural, not prose-level: the model may no longer INVITE
# anything in `answer_text` (see `hivepilot/prompts/concierge.md`). It declares
# its proposed next step in the STRUCTURED `follow_up` field instead; `route()`
# validates that through the same `_clamp` as any other decision, and only if
# it survives — i.e. only if the router can actually execute it — do we store
# it and render our OWN invitation line. The set of offers the bot can make is
# therefore exactly the set the router can execute, by construction.
#
# Scoping mirrors `slack_bot._PendingChallenge` (the F3 fix): an offer is bound
# to the conversation AND to the person who was asked, and it expires. Without
# owner binding, a colleague's unrelated "yes" in a shared channel fires
# someone else's action; without a TTL, an hours-old offer does the same in
# slow motion.
#
# FAIL-CLOSED: a missing conversation id, a missing owner, or an unusable
# expiry each mean "do not execute" — never "execute anyway". An offer is not
# even *rendered* when it could not be honoured.
#
# In-process only (a plain dict), like `_history` above: nothing about
# correctness depends on an offer surviving a restart — a lost offer simply
# falls through to normal handling, which is the safe direction.
# ---------------------------------------------------------------------------

# An offer answers a question the operator is looking at RIGHT NOW; a one-word
# reply needs far less time than composing a Challenge follow-up (Slack's
# `_CHALLENGE_TTL_SECONDS`, 15 min). Ten minutes is short enough that an
# unrelated "yes" later in the day can never fire it — the exact production
# failure mode — and long enough for an operator who gets pulled away
# mid-conversation and comes back to answer.
_OFFER_TTL_SECONDS = 10 * 60

_OFFER_DECLINED_TEXT = "Okay, dropped it. Tell me if you change your mind."


@dataclass(frozen=True)
class _PendingOffer:
    """One follow-up the concierge offered and can honour. *decision* is
    already `_clamp`-validated and destructive, so answering "yes" hands it
    straight to the caller's ordinary destructive-confirmation path (the
    operator sees exactly WHAT will run before it runs) — an affirmative
    resolves the offer, it does not bypass any gate.

    NOT migrated onto `hivepilot.services.pending_confirmation.
    PendingConfirmationStore` (extracted later, closing the THIRD instance of
    this exact owner+TTL bug class in the bot modules' `_pending_concierge`,
    and the FOURTH/FIFTH in `telegram_bot._pending_challenges` /
    `chatops_service._pending_concierge_text`): this implementation is
    correct, shipped, and has its own comprehensive test coverage — the
    churn/regression risk of rewriting a working owner+TTL primitive
    outweighs the DRY benefit. If you are about to add ANOTHER hand-rolled
    owner+TTL pending-confirmation dict anywhere in this codebase, STOP: use
    `PendingConfirmationStore` instead of copying the shape below.
    """

    conversation_id: str
    owner_id: str
    decision: ConciergeDecision
    expires_at: float


_pending_offers: dict[str, _PendingOffer] = {}
_offers_lock = threading.Lock()


# Whole-message affirmatives/negatives, normalised by `_normalise_reply`
# (lowercased, accent-stripped, apostrophes dropped, punctuation flattened to
# spaces). Deliberately matched against the ENTIRE message and never a
# substring: "yes but check the logs first" / "oui mais pas maintenant" are
# NOT affirmatives, and must fall through to normal handling rather than
# execute anything on a maybe.
_AFFIRMATIVE_REPLIES = frozenset(
    {
        # English
        "yes",
        "y",
        "yes please",
        "yeah",
        "yep",
        "yup",
        "ok",
        "okay",
        "sure",
        "go",
        "go ahead",
        "go for it",
        "do it",
        "please do",
        "proceed",
        "confirm",
        "confirmed",
        "affirmative",
        "lets go",
        "sounds good",
        "absolutely",
        # French
        "oui",
        "ouais",
        "ouaip",
        "oui vas y",
        "oui merci",
        "vas y",
        "vasy",
        "allez",
        "allez y",
        "on y va",
        "fais le",
        "faites le",
        "daccord",
        "cest bon",
        "confirme",
        "je confirme",
        "bien sur",
        "carrement",
    }
)

_NEGATIVE_REPLIES = frozenset(
    {
        # English
        "no",
        "n",
        "nope",
        "nah",
        "no thanks",
        "no thank you",
        "not now",
        "never mind",
        "nevermind",
        "cancel",
        "stop",
        "abort",
        "forget it",
        "leave it",
        "negative",
        "later",
        # French
        "non",
        "non merci",
        "annule",
        "annuler",
        "laisse tomber",
        "pas maintenant",
        "non pas maintenant",
        "arrete",
        "arretes",
        "surtout pas",
        "pas la peine",
        "plus tard",
    }
)

# Apostrophe variants dropped outright so "d'accord"/"d’accord" -> "daccord"
# and "let's go" -> "lets go" — one spelling per entry in the tables above.
_APOSTROPHES = "'’‘`´"


def _normalise_reply(text: str) -> str:
    """Fold *text* to the canonical form the reply tables are written in:
    accent-free, lowercase, apostrophe-free, single-spaced. Never raises."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    chars: list[str] = []
    for char in decomposed:
        if unicodedata.combining(char):
            continue
        if char in _APOSTROPHES:
            continue
        lowered = char.lower()
        chars.append(lowered if lowered.isalnum() else " ")
    return " ".join("".join(chars).split())


def _classify_reply(text: str) -> str | None:
    """Return "yes"/"no" if *text* is UNAMBIGUOUSLY a whole-message
    affirmative/negative in English or French, else None.

    None is the fail-closed answer and covers everything interesting: empty
    messages, hedges ("peut-être", "maybe"), and qualified agreement ("yes
    but check the logs first"). Callers must treat None as "not an answer to
    the pending offer" and fall through to normal handling."""
    normalised = _normalise_reply(text)
    if not normalised:
        return None
    if normalised in _AFFIRMATIVE_REPLIES:
        return "yes"
    if normalised in _NEGATIVE_REPLIES:
        return "no"
    return None


def _store_offer(
    conversation_id: str | None, owner_id: str | None, decision: "ConciergeDecision"
) -> None:
    """Record *decision* as the pending offer for *conversation_id*, owned by
    *owner_id*. A missing conversation id or owner stores NOTHING — an offer
    nobody can be matched against must never become an offer anybody can
    trigger."""
    if not conversation_id or not owner_id:
        return
    with _offers_lock:
        _pending_offers[conversation_id] = _PendingOffer(
            conversation_id=conversation_id,
            owner_id=owner_id,
            decision=decision,
            expires_at=time.time() + _OFFER_TTL_SECONDS,
        )


def _resolve_pending_offer(
    reply: str, conversation_id: str | None, user_id: str | None
) -> ConciergeDecision | None:
    """Resolve a yes/no *reply* against the pending offer for
    *conversation_id*, or return None to mean "fall through to normal
    handling" (which never executes anything by itself).

    Returns None — leaving the offer untouched — when the replier is not the
    person who was asked. Returns None after dropping the entry when the
    offer has expired or carries no usable owner/expiry."""
    if not conversation_id or not user_id:
        return None  # fail closed: no conversation or no identity, no execution

    with _offers_lock:
        offer = _pending_offers.get(conversation_id)
        if offer is None:
            return None
        try:
            expired = time.time() > float(offer.expires_at)
        except (TypeError, ValueError):
            expired = True  # unusable expiry means expired, never "never expires"
        if not offer.owner_id or expired:
            _pending_offers.pop(conversation_id, None)
            return None
        if offer.owner_id != user_id:
            # Someone else's "yes". Never consume, never execute — and leave
            # the entry pending so the person who WAS asked can still answer.
            return None
        _pending_offers.pop(conversation_id, None)

    if reply == "no":
        return ConciergeDecision(kind="answer", answer_text=_OFFER_DECLINED_TEXT)
    return offer.decision


def clear_pending_offers(conversation_id: str | None = None) -> None:
    """Ops/test helper: drop the pending offer for one conversation, or every
    conversation when *conversation_id* is None."""
    with _offers_lock:
        if conversation_id is None:
            _pending_offers.clear()
        else:
            _pending_offers.pop(conversation_id, None)


def _summarize_offer(decision: ConciergeDecision) -> str:
    """Plain-language description of what saying "yes" would set in motion —
    read by the operator BEFORE they answer, so an affirmative is informed."""
    if decision.kind == "route":
        target = decision.target or "the default project"
        order = f": {decision.order}" if decision.order else ""
        return f"ask {decision.role_key} to work on {target}{order}"
    if decision.kind == "multi_route":
        parts = [
            f"{d.role_key} on {d.target or 'the default project'}"
            for d in (decision.dispatches or [])
        ]
        if parts:
            return "dispatch " + ", ".join(parts)
        return "do that"
    if decision.kind == "action":
        if decision.action in ("approve", "deny"):
            return f"{decision.action} run {(decision.params or {}).get('run_id')}"
        target = decision.target or "the default project"
        return f"{decision.action} on {target}"
    return "do that"


def _render_offer_line(summary: str) -> str:
    """The invitation is OURS, not the model's — see this section's header."""
    return f'\n\nReply "yes" and I will {summary}. Reply "no" to drop it.'


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


def _role_grounded_in_history(
    role_key: str, roster_by_key: dict[str, dict[str, str]], history_text: str
) -> bool:
    """True if *role_key* (by its role key, display name, or title) was
    actually named somewhere in *history_text* — the conversation memory
    fed to the classifier for THIS chat only (see `_get_history`).

    This is a belt-and-suspenders re-check, independent of the model's own
    judgement: a role can be perfectly valid/configured (pass the
    `known_roles` check) yet still be an ungrounded guess if it was never
    actually part of the conversation — e.g. the model hallucinating a
    plausible-sounding but never-discussed agent. Never trust the model's
    say-so alone for a MULTI-dispatch batch; require an independent textual
    match against the chat's own recent history."""
    if not history_text:
        return False
    entry = roster_by_key.get(role_key)
    needles = {role_key.lower()}
    if entry:
        if entry.get("display"):
            needles.add(entry["display"].lower())
        if entry.get("title"):
            needles.add(entry["title"].lower())
    haystack = history_text.lower()
    return any(needle and needle in haystack for needle in needles)


def _clamp(
    decision: ConciergeDecision,
    *,
    default_role: str,
    default_target: str | None,
    history_text: str = "",
) -> ConciergeDecision:
    """Validate/clamp a parsed decision against what's actually known
    (roster + projects), substitute defaults, and hardcode `destructive`
    (the concierge OWNS this — never trusts the model's self-reported
    value as authoritative for a kind/action already in the table).

    *history_text* (only meaningful for `kind="multi_route"`) is this chat's
    formatted recent-conversation text (see `_format_history`) — used to
    ground each proposed dispatch's role against what was ACTUALLY discussed,
    never merely against what's configured (see `_role_grounded_in_history`).
    """
    if decision.kind == "answer":
        return decision

    from hivepilot.roles import list_roles

    try:
        all_roles = list(list_roles())
    except Exception as exc:  # noqa: BLE001
        logger.warning("concierge.clamp_list_roles_error", error=str(exc))
        all_roles = []
    known_roles = {r.name for r in all_roles}
    known_projects = _known_projects()

    if decision.kind == "multi_route":
        roster_by_key = {
            r.name: {"display": r.display_name or r.name, "title": r.title} for r in all_roles
        }
        valid: list[DispatchOrder] = []
        for dispatch in decision.dispatches or []:
            if dispatch.role_key not in known_roles:
                # Not a configured role at all — dropped, never guessed.
                continue
            if not _role_grounded_in_history(dispatch.role_key, roster_by_key, history_text):
                # Configured, but never actually named in this chat's recent
                # conversation — an ungrounded referent, dropped, not guessed.
                continue
            target = dispatch.target or default_target
            if target is not None and known_projects is not None and target not in known_projects:
                continue
            valid.append(
                DispatchOrder(role_key=dispatch.role_key, target=target, order=dispatch.order or "")
            )
        if not valid:
            return ConciergeDecision(
                kind="answer",
                answer_text=(
                    "I couldn't confirm who you mean — could you name the agents "
                    "and the project explicitly?"
                ),
            )
        return ConciergeDecision(kind="multi_route", dispatches=valid, destructive=True)

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


def _attach_offer(
    decision: ConciergeDecision,
    *,
    default_role: str,
    default_target: str | None,
    history_text: str,
    conversation_id: str | None,
    user_id: str | None,
) -> ConciergeDecision:
    """Turn a classifier-proposed `follow_up` into a live pending offer, and
    append OUR invitation line to the answer the operator will read.

    Returns *decision* unchanged — invitation and all, i.e. nothing — unless
    ALL of these hold: it is an `answer`, it carries a `follow_up`, that
    follow-up survives `_clamp` as an executable route/action/multi_route,
    and we have both a conversation to scope the offer to and an owner to
    bind it to. That conjunction is the enforcement point for "never offer
    what cannot be honoured": there is no path that renders the invitation
    without also storing an offer the router can execute."""
    if decision.kind != "answer" or decision.follow_up is None:
        return _without_follow_up(decision)
    if not conversation_id or not user_id:
        return _without_follow_up(decision)

    offer = _clamp(
        decision.follow_up,
        default_role=default_role,
        default_target=default_target,
        history_text=history_text,
    )
    if offer.kind == "answer":
        # `_clamp` degraded it (unknown role/project, missing run id, an
        # ungrounded multi_route…) — not executable, so not an offer.
        logger.info("concierge.follow_up_not_executable_dropped")
        return _without_follow_up(decision)

    _store_offer(conversation_id, user_id, offer)
    return ConciergeDecision(
        kind="answer",
        answer_text=(decision.answer_text or "") + _render_offer_line(_summarize_offer(offer)),
    )


def _without_follow_up(decision: ConciergeDecision) -> ConciergeDecision:
    """Strip the internal `follow_up` field before returning to a caller — it
    is a classifier-to-`route()` channel, never part of the public result."""
    if decision.follow_up is None:
        return decision
    return replace(decision, follow_up=None)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def route(
    text: str,
    *,
    default_role: str,
    default_target: str | None,
    chat_id: int | None = None,
    conversation_id: str | None = None,
    user_id: str | None = None,
) -> ConciergeDecision:
    """Classify *text* into an ANSWER / ROUTE / ACTION / MULTI_ROUTE decision.

    Fail-closed: any LLM error, timeout, malformed response, or reference to
    an unknown/ungrounded role/project degrades to a friendly `answer` — this
    function NEVER fabricates a route/action/multi_route it cannot validate.
    Synchronous/blocking (one LLM call) — callers on an event loop must run
    it in an executor.

    *chat_id*, when supplied, threads this call into the per-chat rolling
    conversation memory (see the "Conversation memory" section above): the
    chat's recent history is fed into the classifier prompt so a follow-up
    ("give them the orders", "do it") can be resolved, and this turn is
    recorded afterwards. Omitting *chat_id* (the default) disables memory
    entirely for this call — byte-identical to the pre-memory behaviour,
    and every call site that doesn't pass it keeps working unchanged.

    *conversation_id* + *user_id*, when BOTH supplied, enable pending
    follow-up offers (see the "Pending follow-up offers" section above): a
    bare "yes"/"oui" answering an offer this concierge just made resolves to
    the offered decision without an LLM round-trip, and an offer is only ever
    made in the first place when it can be honoured. Omitting either disables
    offers entirely for this call — no offer is stored AND none is rendered,
    so the bot never invites a reply it cannot understand.
    """
    # A yes/no answering a live offer is resolved deterministically, in code,
    # BEFORE any classification — an affirmative must never depend on the
    # model re-deriving what "yes" referred to. Anything that isn't an
    # unambiguous whole-message yes/no, and any yes/no with no honourable
    # offer behind it (expired, someone else's, another conversation's),
    # falls through to normal handling below and executes nothing.
    reply = _classify_reply(text)
    if reply is not None:
        resolved = _resolve_pending_offer(reply, conversation_id, user_id)
        if resolved is not None:
            if chat_id is not None:
                _record_turn(chat_id, text, _history_summary(resolved))
            return resolved

    roster = _build_roster()
    snapshot = _grounding_snapshot()
    history = _get_history(chat_id)
    history_text = _format_history(history)
    prompt = _build_classifier_prompt(text, roster, snapshot, history_text)

    model = settings.chatops_concierge_model or _DEFAULT_CONCIERGE_MODEL
    mode = _resolve_mode()
    options = _build_classifier_options(mode)

    # HARD INVARIANT: the cli classifier must never run tool-capable on
    # untrusted input. This check is deliberately decoupled from
    # `_build_classifier_options` (a separate function, re-reading its
    # output rather than trusting a shared flag) and is a runtime check —
    # not a bare `assert`, which can be stripped by `python -O` — so it
    # holds even if a future edit to that function forgets to wire the
    # no-tools restriction: refuse to spawn the cli session at all rather
    # than ever risk a tool-capable claude process on attacker-controlled
    # concierge input.
    if mode == "cli" and options.get("tools") != _CLASSIFIER_NO_TOOLS:
        logger.error("concierge.cli_no_tools_invariant_violated_refusing")
        return ConciergeDecision(kind="answer", answer_text=_FALLBACK_ANSWER)

    runner_def = RunnerDefinition(
        name="concierge",
        kind=cast(RunnerKind, "claude"),
        model=model,
        options=options,
        timeout_seconds=_CLASSIFIER_TIMEOUT_SECONDS,
    )
    prompt_file = _resolve_prompt_file()
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

    decision = _clamp(
        parsed,
        default_role=default_role,
        default_target=default_target,
        history_text=history_text,
    )
    decision = _attach_offer(
        decision,
        default_role=default_role,
        default_target=default_target,
        history_text=history_text,
        conversation_id=conversation_id,
        user_id=user_id,
    )
    if chat_id is not None:
        _record_turn(chat_id, text, _history_summary(decision))
    return decision
