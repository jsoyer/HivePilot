"""HP-96 workspace text edits, revision counter, isolated JSON memory.

Coworker pattern (``workspace-context.ts``, ``applyWorkspaceTextEdit``),
rewritten in Python. This module does **not** vendor TypeScript or Electron.

Exact text edit:

- ``old_text`` empty → **append** ``new_text``.
- A non-unique ``old_text`` match → **refuse** (no mutation).
- ``expected_revision`` is **required**; mismatch → stale refuse.

Memory isolation: stored values are JSON **data**, never instructions.
Recall returns a data envelope (``role=data``). There is no path that
concatenates memory into ``extra_prompt`` / system text. HP-101 HITL
proposals and HP-97 PASS stay out of scope.

Orthogonal to ``hivepilot.tool_catalog`` and ``hivepilot.skill_catalog`` —
this module does not import or mutate either catalog.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

# Closed vocabularies. Adding a token is additive; renaming is a breaking change.
REFUSAL_CODES: tuple[str, ...] = (
    "missing_revision",
    "stale",
    "ambiguous",
    "not_found",
    "overflow",
    "invalid_json",
    "instruction_role",
)

MEMORY_ROLE = "data"
MEMORY_KIND = "memory"
INSTRUCTION_ROLES: frozenset[str] = frozenset({"system", "instruction", "developer"})

# Bound workspace / memory payloads so a single edit cannot blow the context.
DEFAULT_MAX_BYTES = 256 * 1024


class WorkspaceTextError(ValueError):
    """Programmer error (bad types), not an edit refusal."""


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """One revision of a workspace document."""

    text: str
    revision: int

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "revision": self.revision}


@dataclass(frozen=True)
class WorkspaceEditResult:
    """Outcome of ``apply_workspace_text_edit``. ``ok`` is the only success flag."""

    ok: bool
    snapshot: WorkspaceSnapshot
    code: str = ""
    reason: str = ""

    @property
    def refused(self) -> bool:
        return not self.ok


@dataclass(frozen=True)
class MemoryEnvelope:
    """Isolated recall payload. ``role`` is always ``data``, never an instruction."""

    role: str
    kind: str
    isolated: bool
    key: str
    revision: int
    payload: Any

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "kind": self.kind,
            "isolated": self.isolated,
            "key": self.key,
            "revision": self.revision,
            "payload": self.payload,
        }


@dataclass(frozen=True)
class MemoryPutResult:
    ok: bool
    key: str
    revision: int
    envelope: MemoryEnvelope | None
    code: str = ""
    reason: str = ""

    @property
    def refused(self) -> bool:
        return not self.ok


def _is_int(value: object) -> bool:
    # bool is an int subclass; a revision of True/False is a type error, not 1/0.
    return isinstance(value, int) and not isinstance(value, bool)


def _utf8_size(text: str) -> int:
    return len(text.encode("utf-8"))


def _count_non_overlapping(haystack: str, needle: str) -> int:
    """Exact, non-overlapping occurrences. Empty needle is never a match."""
    if needle == "":
        return 0
    count = 0
    start = 0
    while True:
        idx = haystack.find(needle, start)
        if idx < 0:
            return count
        count += 1
        start = idx + len(needle)


def _refuse(snapshot: WorkspaceSnapshot, code: str, reason: str) -> WorkspaceEditResult:
    return WorkspaceEditResult(ok=False, snapshot=snapshot, code=code, reason=reason)


def apply_workspace_text_edit(
    *,
    text: str,
    revision: int,
    old_text: str,
    new_text: str,
    expected_revision: int | None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> WorkspaceEditResult:
    """Apply one exact text edit under an optimistic revision lock.

    Empty ``old_text`` appends. A unique ``old_text`` is replaced (``new_text``
    empty removes it). Duplicate matches, a missing/stale revision, or a
    result over ``max_bytes`` refuse and leave the snapshot unchanged.
    """
    if not isinstance(text, str) or not isinstance(old_text, str) or not isinstance(new_text, str):
        raise WorkspaceTextError("text, old_text, and new_text must be str")
    if not _is_int(revision):
        raise WorkspaceTextError("revision must be an int")
    if not _is_int(max_bytes) or max_bytes < 1:
        raise WorkspaceTextError("max_bytes must be a positive int")

    current = WorkspaceSnapshot(text=text, revision=revision)

    if expected_revision is None:
        return _refuse(
            current,
            "missing_revision",
            "expected_revision is required",
        )
    if not _is_int(expected_revision):
        raise WorkspaceTextError("expected_revision must be an int")
    if expected_revision != revision:
        return _refuse(
            current,
            "stale",
            f"stale revision: expected {expected_revision}, current {revision}",
        )

    if old_text == "":
        next_text = text + new_text
    else:
        hits = _count_non_overlapping(text, old_text)
        if hits == 0:
            return _refuse(current, "not_found", "old_text does not occur in the document")
        if hits > 1:
            return _refuse(
                current,
                "ambiguous",
                f"old_text matches {hits} times; refuse non-unique edit",
            )
        next_text = text.replace(old_text, new_text, 1)

    if _utf8_size(next_text) > max_bytes:
        return _refuse(
            current,
            "overflow",
            f"edit would exceed max_bytes={max_bytes}",
        )

    return WorkspaceEditResult(
        ok=True,
        snapshot=WorkspaceSnapshot(text=next_text, revision=revision + 1),
    )


def canonicalize_memory_data(value: Any) -> Any:
    """Round-trip through JSON so only data (not callables / NaN) can be stored."""
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise WorkspaceTextError(f"memory value is not JSON data: {exc}") from exc


def memory_json_bytes(value: Any) -> int:
    payload = canonicalize_memory_data(value)
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def wrap_memory_data(key: str, payload: Any, revision: int) -> MemoryEnvelope:
    """Tag a JSON value as isolated data. Inner ``role`` fields stay payload."""
    data = canonicalize_memory_data(payload)
    return MemoryEnvelope(
        role=MEMORY_ROLE,
        kind=MEMORY_KIND,
        isolated=True,
        key=key,
        revision=revision,
        payload=data,
    )


def is_instruction_envelope(value: Mapping[str, Any] | MemoryEnvelope) -> bool:
    """True only for an *outer* instruction role. Payload keys never count."""
    if isinstance(value, MemoryEnvelope):
        role = value.role
    else:
        role = value.get("role")
    return isinstance(role, str) and role in INSTRUCTION_ROLES


def memory_as_instructions(envelope: MemoryEnvelope) -> None:
    """Isolation invariant: memory must not be rendered as instructions."""
    raise WorkspaceTextError("memory is JSON data, not instructions; refuse instruction rendering")


def merge_memory_into_extra_prompt(metadata: Mapping[str, Any], envelope: MemoryEnvelope) -> None:
    """Refuse the extra_prompt contamination path ``memory_kind`` exists to stop."""
    raise WorkspaceTextError("isolated memory must not be merged into extra_prompt or system text")


class IsolatedJsonMemory:
    """In-memory JSON store. Values are data envelopes, never instruction text.

    Writes use the same ``expected_revision`` lock as workspace text. First
    write on a key uses revision ``0``. Overflow is measured on canonical JSON.
    """

    def __init__(self, *, max_bytes: int = DEFAULT_MAX_BYTES) -> None:
        if not _is_int(max_bytes) or max_bytes < 1:
            raise WorkspaceTextError("max_bytes must be a positive int")
        self._max_bytes = max_bytes
        self._records: dict[str, MemoryEnvelope] = {}

    def get(self, key: str) -> MemoryEnvelope | None:
        return self._records.get(key)

    def revision(self, key: str) -> int:
        record = self._records.get(key)
        return 0 if record is None else record.revision

    def keys(self) -> tuple[str, ...]:
        return tuple(sorted(self._records))

    def render(self, key: str) -> MemoryEnvelope | None:
        """Recall. Always ``role=data`` when present."""
        envelope = self._records.get(key)
        if envelope is None:
            return None
        # Re-wrap so a caller cannot mutate stored isolation flags.
        return wrap_memory_data(envelope.key, envelope.payload, envelope.revision)

    def put(
        self,
        key: str,
        value: Any,
        *,
        expected_revision: int | None,
    ) -> MemoryPutResult:
        if not isinstance(key, str) or not key:
            raise WorkspaceTextError("memory key must be a non-empty str")

        current = self._records.get(key)
        current_rev = 0 if current is None else current.revision

        if expected_revision is None:
            return MemoryPutResult(
                ok=False,
                key=key,
                revision=current_rev,
                envelope=current,
                code="missing_revision",
                reason="expected_revision is required",
            )
        if not _is_int(expected_revision):
            raise WorkspaceTextError("expected_revision must be an int")
        if expected_revision != current_rev:
            return MemoryPutResult(
                ok=False,
                key=key,
                revision=current_rev,
                envelope=current,
                code="stale",
                reason=f"stale revision: expected {expected_revision}, current {current_rev}",
            )

        try:
            payload = canonicalize_memory_data(value)
        except WorkspaceTextError as exc:
            return MemoryPutResult(
                ok=False,
                key=key,
                revision=current_rev,
                envelope=current,
                code="invalid_json",
                reason=str(exc),
            )

        if memory_json_bytes(payload) > self._max_bytes:
            return MemoryPutResult(
                ok=False,
                key=key,
                revision=current_rev,
                envelope=current,
                code="overflow",
                reason=f"memory JSON would exceed max_bytes={self._max_bytes}",
            )

        envelope = wrap_memory_data(key, payload, current_rev + 1)
        if is_instruction_envelope(envelope):
            return MemoryPutResult(
                ok=False,
                key=key,
                revision=current_rev,
                envelope=current,
                code="instruction_role",
                reason="memory envelope role must be data, not an instruction",
            )
        self._records[key] = envelope
        return MemoryPutResult(
            ok=True,
            key=key,
            revision=envelope.revision,
            envelope=envelope,
        )
