"""HP-79: skill usage + workshop (propose a patch, never auto-apply).

Skills stay files. This module records when a skill ran, can propose a
unified-diff patch (from a failed step, or an explicit API/CLI body), and
applies that patch to a **directory** skill only after an approve-rank
accept. Plugin-inlined skills can be proposed for review but are never
written back (the operator copies them into the config-repo `skills/`
tree first).
"""

from __future__ import annotations

import difflib
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any, Protocol

from hivepilot.services import db, events, state_service
from hivepilot.services.config_provenance import redact_text
from hivepilot.skill_dirs import (
    MAX_SKILL_FILE_BYTES,
    MAX_SKILL_FILES,
    SKILL_MANIFEST,
    skill_scan_dirs,
)
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_PROPOSED = "proposed"
_ACCEPTED = "accepted"
_REJECTED = "rejected"
_DIRECTORY_PREFIX = "directory:"


class SkillWorkshopError(RuntimeError):
    """Operator-facing refusal (unknown skill, stale digest, not writable)."""


class SkillLookup(Protocol):
    """Minimal plugin surface so tests can inject a stub.

    ``get_skill`` may return a ``SkillSpec`` TypedDict or a plain mapping;
    callers only read string keys. Typed as ``Any`` so PluginManager (which
    returns ``SkillSpec | None``) is a structural match under mypy.
    """

    def get_skill(self, name: str) -> Any: ...


def files_digest(files: dict[str, str]) -> str:
    payload = json.dumps(files, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def unified_files_diff(before: dict[str, str], after: dict[str, str]) -> str:
    """Unified diff of two skill file maps. Empty string means identical."""
    chunks: list[str] = []
    for rel in sorted(set(before) | set(after)):
        old = before.get(rel, "").splitlines()
        new = after.get(rel, "").splitlines()
        if old == new:
            continue
        chunks.extend(
            difflib.unified_diff(
                old,
                new,
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                lineterm="",
            )
        )
    return "\n".join(chunks)


def _safe_relpath(rel: str) -> str:
    cleaned = (rel or "").strip().replace("\\", "/")
    if not cleaned or cleaned.startswith("/") or cleaned.startswith("./../"):
        raise SkillWorkshopError(f"unsafe skill path {rel!r}")
    parts = Path(cleaned).parts
    if any(part in {"", ".", ".."} or part.startswith(".") for part in parts):
        raise SkillWorkshopError(f"unsafe skill path {rel!r}")
    return Path(*parts).as_posix()


def merge_patch(current: dict[str, str], patch_files: dict[str, str]) -> dict[str, str]:
    if len(current) + len(patch_files) > MAX_SKILL_FILES:
        raise SkillWorkshopError(f"more than {MAX_SKILL_FILES} files")
    merged = dict(current)
    for raw_rel, content in patch_files.items():
        rel = _safe_relpath(raw_rel)
        if not isinstance(content, str):
            raise SkillWorkshopError(f"file {rel} must be text")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_SKILL_FILE_BYTES:
            raise SkillWorkshopError(
                f"file {rel} is {len(encoded)} bytes, over the {MAX_SKILL_FILE_BYTES}-byte limit"
            )
        merged[rel] = content
    return merged


def record_usage(
    skill_names: list[str],
    *,
    run_id: int | None = None,
    step: str | None = None,
    runner_kind: str | None = None,
    tenant: str = "default",
    outcome: str = "applied",
) -> None:
    """Append-only; fail-safe so a telemetry write never breaks a step."""
    try:
        state_service.init_db()
        with db.connect() as conn:
            for name in skill_names:
                if not name:
                    continue
                conn.execute(
                    db.ph(
                        """
                        INSERT INTO skill_usage_events
                            (id, skill_name, tenant, run_id, step, runner_kind, outcome)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """
                    ),
                    (str(uuid.uuid4()), name, tenant, run_id, step, runner_kind, outcome),
                )
                events.emit(
                    "skill.applied",
                    "skill",
                    name,
                    tenant=tenant,
                    payload={"run_id": run_id, "step": step, "outcome": outcome},
                )
    except Exception:  # noqa: BLE001 — usage must never abort a run
        logger.warning("skill_usage.record_failed", exc_info=True)


def list_usage(
    *, skill_name: str | None = None, tenant: str | None = None, limit: int = 50
) -> list[dict]:
    state_service.init_db()
    clauses = ["1=1"]
    args: list[Any] = []
    if skill_name:
        clauses.append("skill_name = ?")
        args.append(skill_name)
    if tenant is not None:
        clauses.append("tenant = ?")
        args.append(tenant)
    args.append(max(1, min(int(limit), 500)))
    with db.connect() as conn:
        rows = conn.execute(
            db.ph(
                f"SELECT * FROM skill_usage_events WHERE {' AND '.join(clauses)} "
                "ORDER BY created_ts DESC LIMIT ?"
            ),
            args,
        ).fetchall()
    return [dict(r) for r in rows]


def _pending_for(skill_name: str, tenant: str) -> dict | None:
    state_service.init_db()
    with db.connect() as conn:
        row = conn.execute(
            db.ph(
                "SELECT * FROM skill_patch_proposals WHERE skill_name = ? AND tenant = ? "
                "AND status = ? ORDER BY created_ts DESC LIMIT 1"
            ),
            (skill_name, tenant, _PROPOSED),
        ).fetchone()
    return dict(row) if row is not None else None


def propose_patch(
    skill_name: str,
    patch_files: dict[str, str],
    *,
    lookup: SkillLookup,
    rationale: str,
    tenant: str = "default",
    run_id: int | None = None,
    step: str | None = None,
) -> dict:
    """Queue a patch. Never writes skill files."""
    spec = lookup.get_skill(skill_name)
    if spec is None:
        raise SkillWorkshopError(f"unknown skill {skill_name!r}")
    current = dict(spec.get("files") or {})
    if not current:
        raise SkillWorkshopError(f"skill {skill_name!r} has no files")
    merged = merge_patch(current, patch_files)
    diff_text = unified_files_diff(current, merged)
    if not diff_text:
        raise SkillWorkshopError("patch is identical to the current skill")
    pending = _pending_for(skill_name, tenant)
    if pending is not None:
        return pending
    row = {
        "id": str(uuid.uuid4()),
        "skill_name": skill_name,
        "tenant": tenant,
        "status": _PROPOSED,
        "provider": spec.get("provider"),
        "run_id": run_id,
        "step": step,
        "rationale": redact_text(rationale)[:2000],
        "base_digest": files_digest(current),
        "patch_json": json.dumps({"files": {_safe_relpath(k): v for k, v in patch_files.items()}}),
        "diff_text": redact_text(diff_text),
        "decided_ts": None,
        "decided_by": None,
    }
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
                INSERT INTO skill_patch_proposals
                    (id, skill_name, tenant, status, provider, run_id, step,
                     rationale, base_digest, patch_json, diff_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """
            ),
            (
                row["id"],
                row["skill_name"],
                row["tenant"],
                row["status"],
                row["provider"],
                row["run_id"],
                row["step"],
                row["rationale"],
                row["base_digest"],
                row["patch_json"],
                row["diff_text"],
            ),
        )
    events.emit(
        "skill.proposal",
        "skill",
        skill_name,
        tenant=tenant,
        payload={"proposal_id": row["id"], "step": step},
    )
    return get_proposal(row["id"], tenant=None) or row


def propose_from_failure(
    skill_names: list[str],
    *,
    lookup: SkillLookup,
    detail: str,
    run_id: int | None,
    step: str | None,
    tenant: str = "default",
) -> list[dict]:
    """Auto-improve v0: one proposed SKILL.md note per skill, on step failure."""
    created: list[dict] = []
    note = redact_text((detail or "").strip())[:400]
    if not note:
        return created
    for name in skill_names:
        spec = lookup.get_skill(name)
        if spec is None:
            continue
        files = dict(spec.get("files") or {})
        manifest = files.get(SKILL_MANIFEST)
        if manifest is None:
            continue
        appendix = (
            f"\n\n## Observed failure (workshop draft)\n\n"
            f"- step: `{step or 'unknown'}`\n"
            f"- note: {note}\n"
        )
        if appendix.strip() in manifest:
            continue
        try:
            created.append(
                propose_patch(
                    name,
                    {SKILL_MANIFEST: manifest.rstrip() + appendix},
                    lookup=lookup,
                    rationale=f"step '{step}' failed while skill '{name}' was applied",
                    tenant=tenant,
                    run_id=run_id,
                    step=step,
                )
            )
        except SkillWorkshopError:
            continue
        except Exception:  # noqa: BLE001 — failure notes must not hide the real error
            logger.warning("skill_proposal.from_failure_failed", skill=name, exc_info=True)
    return created


def list_proposals(
    *, status: str | None = None, tenant: str | None = None, limit: int = 50
) -> list[dict]:
    state_service.init_db()
    clauses = ["1=1"]
    args: list[Any] = []
    if status:
        clauses.append("status = ?")
        args.append(status)
    if tenant is not None:
        clauses.append("tenant = ?")
        args.append(tenant)
    args.append(max(1, min(int(limit), 500)))
    with db.connect() as conn:
        rows = conn.execute(
            db.ph(
                f"SELECT * FROM skill_patch_proposals WHERE {' AND '.join(clauses)} "
                "ORDER BY created_ts DESC LIMIT ?"
            ),
            args,
        ).fetchall()
    return [dict(r) for r in rows]


def get_proposal(proposal_id: str, *, tenant: str | None) -> dict | None:
    state_service.init_db()
    clauses = ["id = ?"]
    args: list[Any] = [proposal_id]
    if tenant is not None:
        clauses.append("tenant = ?")
        args.append(tenant)
    with db.connect() as conn:
        row = conn.execute(
            db.ph(f"SELECT * FROM skill_patch_proposals WHERE {' AND '.join(clauses)}"),
            args,
        ).fetchone()
    return dict(row) if row is not None else None


def _writable_root(provider: str | None) -> Path:
    if not provider or not provider.startswith(_DIRECTORY_PREFIX):
        raise SkillWorkshopError("only directory skills can be applied from the workshop")
    root = Path(provider[len(_DIRECTORY_PREFIX) :]).resolve()
    allowed = [p.resolve() for p in skill_scan_dirs()]
    if not any(root == scan / root.name or root.is_relative_to(scan) for scan in allowed):
        raise SkillWorkshopError("skill directory is outside the configured skills scan roots")
    return root


def _write_patched_files(root: Path, patch_files: dict[str, str]) -> None:
    root = root.resolve()
    for rel, content in patch_files.items():
        dest = (root / _safe_relpath(rel)).resolve()
        if not dest.is_relative_to(root):
            raise SkillWorkshopError(f"write escapes skill directory: {rel}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


def decide_proposal(
    proposal_id: str,
    *,
    accept: bool,
    actor: str,
    lookup: SkillLookup,
    tenant: str | None,
) -> dict:
    row = get_proposal(proposal_id, tenant=tenant)
    if row is None:
        raise SkillWorkshopError("proposal not found")
    if row["status"] != _PROPOSED:
        return row
    if accept:
        spec = lookup.get_skill(row["skill_name"])
        if spec is None:
            raise SkillWorkshopError(f"unknown skill {row['skill_name']!r}")
        current = dict(spec.get("files") or {})
        if files_digest(current) != row["base_digest"]:
            raise SkillWorkshopError("skill changed since the proposal; reject and re-propose")
        patch = json.loads(row["patch_json"]).get("files") or {}
        merge_patch(current, patch)  # size / path validation
        root = _writable_root(str(spec.get("provider") or row.get("provider") or ""))
        _write_patched_files(root, patch)
        new_status = _ACCEPTED
    else:
        new_status = _REJECTED
    with db.connect() as conn:
        updated = conn.execute(
            db.ph(
                """
                UPDATE skill_patch_proposals
                   SET status = ?, decided_by = ?, decided_ts = CURRENT_TIMESTAMP
                 WHERE id = ? AND status = ?
                """
            ),
            (new_status, actor, proposal_id, _PROPOSED),
        )
        if getattr(updated, "rowcount", 1) != 1:
            fresh = get_proposal(proposal_id, tenant=tenant)
            if fresh is None:
                raise SkillWorkshopError("proposal not found")
            return fresh
    events.emit(
        "skill.proposal.decided",
        "skill",
        row["skill_name"],
        tenant=row.get("tenant") or "default",
        payload={"proposal_id": proposal_id, "status": new_status, "actor": actor},
    )
    decided = get_proposal(proposal_id, tenant=tenant)
    if decided is None:
        raise SkillWorkshopError("proposal not found")
    return decided
