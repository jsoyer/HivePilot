"""HP-111 atomic accept + crash recovery for skill-evolution drafts.

OpenSpace evolution-engine commit pattern, rewritten in Python. This
module does **not** vendor OpenSpace, talk to OpenSpace cloud, persist
pickle embeddings, write ``.skill_id`` sidecars, or auto-apply.

Contracts:

- Writes only after PASS ``APPROVED`` (HITL). PENDING / REJECTED refuse.
- ``validate()`` / ``validate_proposal()`` run before any write. Reject
  blocks. ``needs_human_review`` stays HITL (already APPROVED is enough).
- Digest / etag mismatch is stale and does not write.
- Double accept is idempotent (same digest, no second mutation).
- Multi-file write uses a sibling journal + staging tree so an interrupted
  accept can recover without leaving a half-applied skill.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from hivepilot.pass_store import (
    APPROVED,
    PENDING,
    REJECTED,
    SKILL_EVOLUTION_KIND,
    PassProposal,
    PassStoreError,
    get as get_proposal,
    update_payload,
)
from hivepilot.services import events
from hivepilot.skill_catalog import (
    SKILL_ID_SIDECAR,
    SkillCatalog,
    SkillCatalogError,
    snapshot_hash,
)
from hivepilot.skill_evolution_validator import (
    REJECT,
    SkillEvolutionValidatorError,
    validate_proposal,
)

JOURNAL_DIR_NAME = ".hp111"
JOURNAL_SUFFIX = ".json"
STAGING_SUFFIX = ".new"
BACKUP_SUFFIX = ".bak"

PREPARING = "preparing"
STAGED = "staged"
BACKUP = "backup"
SWAPPED = "swapped"
COMMITTED = "committed"

HITL_REQUIRED = "hitl_required"
STALE_DIGEST = "stale_digest"
VALIDATION_REJECTED = "validation_rejected"
ALREADY_APPLIED = "already_applied"
SKILL_ROOT_REQUIRED = "skill_root_required"
PROPOSAL_NOT_FOUND = "proposal_not_found"
WRONG_KIND = "wrong_kind"
REJECTED_PROPOSAL = "rejected_proposal"
UNSAFE_PATH = "unsafe_path"
UNSAFE_NAME = "unsafe_name"

_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SkillEvolutionAcceptError(ValueError):
    """Invalid accept arguments (not a HITL / digest / validation refusal)."""


@dataclass(frozen=True)
class ApplyResult:
    """Outcome of an accept attempt. ``mutated`` is True only on a first write."""

    ok: bool
    mutated: bool
    code: str
    reason: str
    proposal_id: str = ""
    skill_root: str = ""
    content_hash: str = ""
    recovered: bool = False
    idempotent: bool = False
    validation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "mutated": self.mutated,
            "code": self.code,
            "reason": self.reason,
            "proposal_id": self.proposal_id,
            "skill_root": self.skill_root,
            "content_hash": self.content_hash,
            "recovered": self.recovered,
            "idempotent": self.idempotent,
            "validation": dict(self.validation),
        }


def accept_approved(
    proposal_id: str = "",
    *,
    proposal: PassProposal | None = None,
    skill_root: Path | None = None,
    catalog: SkillCatalog | None = None,
    expected_digest: str = "",
    actor: str = "",
) -> ApplyResult:
    """Atomically write an APPROVED draft. Never auto-approves."""
    row = _load_proposal(proposal_id, proposal)
    if isinstance(row, ApplyResult):
        return row
    gated = _gate_hitl_and_digest(row, expected_digest=expected_digest)
    if gated is not None:
        return gated

    dest = _resolve_dest(row, skill_root)
    if dest is None:
        return ApplyResult(
            ok=False,
            mutated=False,
            code=SKILL_ROOT_REQUIRED,
            reason="skill_root is required to write directory skills",
            proposal_id=row.id,
            content_hash=str(row.payload.get("content_hash") or ""),
            validation=_validation_dict(row.id),
        )

    files = _proposed_files(row)
    digest = str(row.payload.get("content_hash") or "")
    if snapshot_hash(files) != digest:
        return ApplyResult(
            ok=False,
            mutated=False,
            code=STALE_DIGEST,
            reason="payload files no longer match the stored content_hash",
            proposal_id=row.id,
            skill_root=str(dest),
            content_hash=digest,
            validation=_validation_dict(row.id),
        )

    on_disk = _read_skill_files(dest) if dest.is_dir() else {}
    if on_disk and snapshot_hash(on_disk) == digest:
        stored = _mark_applied(row, dest, digest, actor=actor, recovered=False)
        _cleanup(dest)
        if catalog is not None:
            _record_catalog(catalog, stored, dest)
        return ApplyResult(
            ok=True,
            mutated=False,
            code=ALREADY_APPLIED,
            reason="skill files already match the approved digest",
            proposal_id=row.id,
            skill_root=str(dest),
            content_hash=digest,
            idempotent=True,
            validation=_validation_dict(row.id),
        )

    stale = _stale_on_disk(row, on_disk)
    if stale is not None:
        return stale

    recovered = _recover_if_needed(dest, row.id, digest)
    if dest.is_dir():
        on_disk = _read_skill_files(dest)
        if on_disk and snapshot_hash(on_disk) == digest:
            stored = _mark_applied(row, dest, digest, actor=actor, recovered=recovered)
            _cleanup(dest)
            if catalog is not None:
                _record_catalog(catalog, stored, dest)
            return ApplyResult(
                ok=True,
                mutated=False,
                code=ALREADY_APPLIED,
                reason="recovered accept already matches the approved digest",
                proposal_id=row.id,
                skill_root=str(dest),
                content_hash=digest,
                recovered=recovered,
                idempotent=True,
                validation=_validation_dict(row.id),
            )

    _atomic_commit(dest, files, proposal_id=row.id, content_hash=digest)
    stored = _mark_applied(row, dest, digest, actor=actor, recovered=recovered)
    if catalog is not None:
        _record_catalog(catalog, stored, dest)
    events.emit(
        "skill.evolution_accepted",
        SKILL_EVOLUTION_KIND,
        row.id,
        tenant=row.tenant,
        payload={
            "skill_root": str(dest),
            "content_hash": digest,
            "recovered": recovered,
            "actor": (actor or "").strip(),
        },
    )
    return ApplyResult(
        ok=True,
        mutated=True,
        code="accepted",
        reason="atomic accept committed",
        proposal_id=row.id,
        skill_root=str(dest),
        content_hash=digest,
        recovered=recovered,
        validation=_validation_dict(row.id),
    )


def recover_accept(skill_root: Path) -> bool:
    """Finish or roll back an interrupted accept. True if dest now matches journal."""
    dest = skill_root.resolve()
    journal = _journal_path(dest)
    if not journal.is_file():
        return dest.is_dir()
    payload = _read_journal(journal)
    if payload is None:
        return False
    return _resume_journal(dest, payload)


def _load_proposal(
    proposal_id: str,
    proposal: PassProposal | None,
) -> PassProposal | ApplyResult:
    if proposal is not None:
        row = proposal
    else:
        row_id = (proposal_id or "").strip()
        if not row_id:
            raise SkillEvolutionAcceptError("proposal_id is required")
        found = get_proposal(row_id)
        if found is None:
            return ApplyResult(
                ok=False,
                mutated=False,
                code=PROPOSAL_NOT_FOUND,
                reason="proposal not found",
                proposal_id=row_id,
            )
        row = found
    if row.kind != SKILL_EVOLUTION_KIND:
        return ApplyResult(
            ok=False,
            mutated=False,
            code=WRONG_KIND,
            reason=f"proposal {row.id} is kind={row.kind}, not skill_evolution",
            proposal_id=row.id,
        )
    return row


def _gate_hitl_and_digest(row: PassProposal, *, expected_digest: str) -> ApplyResult | None:
    digest = str(row.payload.get("content_hash") or "")
    if row.status == REJECTED:
        return ApplyResult(
            ok=False,
            mutated=False,
            code=REJECTED_PROPOSAL,
            reason="rejected proposals cannot be accepted",
            proposal_id=row.id,
            content_hash=digest,
        )
    if row.status != APPROVED:
        return ApplyResult(
            ok=False,
            mutated=False,
            code=HITL_REQUIRED,
            reason="human approve required before atomic accept",
            proposal_id=row.id,
            content_hash=digest,
            validation=_validation_dict(row.id),
        )
    if row.payload.get("applied") and str(row.payload.get("applied_digest") or "") == digest:
        return ApplyResult(
            ok=True,
            mutated=False,
            code=ALREADY_APPLIED,
            reason="proposal already accepted",
            proposal_id=row.id,
            skill_root=str(row.payload.get("applied_root") or ""),
            content_hash=digest,
            idempotent=True,
            validation=_validation_dict(row.id),
        )
    verdict = validate_proposal(row.id)
    if verdict.result == REJECT:
        return ApplyResult(
            ok=False,
            mutated=False,
            code=VALIDATION_REJECTED,
            reason=verdict.reason or "validation_rejected",
            proposal_id=row.id,
            content_hash=digest,
            validation=verdict.to_dict(),
        )
    etag = (expected_digest or "").strip()
    if etag and etag != digest:
        return ApplyResult(
            ok=False,
            mutated=False,
            code=STALE_DIGEST,
            reason="expected_digest does not match the proposal content_hash",
            proposal_id=row.id,
            content_hash=digest,
            validation=verdict.to_dict(),
        )
    return None


def _stale_on_disk(row: PassProposal, on_disk: Mapping[str, str]) -> ApplyResult | None:
    if not on_disk:
        return None
    digest = str(row.payload.get("content_hash") or "")
    base_digest = str(row.payload.get("base_digest") or "")
    current = snapshot_hash(on_disk)
    if current == digest:
        return None
    if base_digest and current == base_digest:
        return None
    if not base_digest:
        return None
    return ApplyResult(
        ok=False,
        mutated=False,
        code=STALE_DIGEST,
        reason="on-disk skill digest no longer matches the draft baseline",
        proposal_id=row.id,
        content_hash=digest,
        validation=_validation_dict(row.id),
    )


def _resolve_dest(row: PassProposal, skill_root: Path | None) -> Path | None:
    if skill_root is not None:
        dest = Path(skill_root)
        _assert_skill_name(dest.name)
        return dest
    stored = str(row.payload.get("skill_root") or "").strip()
    if stored:
        dest = Path(stored)
        _assert_skill_name(dest.name)
        return dest
    return None


def _proposed_files(row: PassProposal) -> dict[str, str]:
    raw = row.payload.get("files")
    if not isinstance(raw, dict):
        raise SkillEvolutionAcceptError("proposal files must be a mapping")
    files: dict[str, str] = {}
    for rel, content in raw.items():
        if not isinstance(rel, str) or not isinstance(content, str):
            raise SkillEvolutionAcceptError("skill files must be str → str")
        cleaned = _safe_relpath(rel)
        files[cleaned] = content
    if SKILL_ID_SIDECAR in files:
        raise SkillEvolutionAcceptError("writing .skill_id sidecars is forbidden")
    return files


def _safe_relpath(rel: str) -> str:
    cleaned = (rel or "").strip().replace("\\", "/")
    if not cleaned or cleaned.startswith("/") or cleaned.startswith("~"):
        raise SkillEvolutionAcceptError(f"{UNSAFE_PATH}: {rel!r}")
    parts = Path(cleaned).parts
    if any(part in {"", ".", ".."} or part.startswith(".") for part in parts):
        raise SkillEvolutionAcceptError(f"{UNSAFE_PATH}: {rel!r}")
    return Path(*parts).as_posix()


def _assert_skill_name(name: str) -> None:
    if not _SKILL_NAME_RE.match(name or ""):
        raise SkillEvolutionAcceptError(f"{UNSAFE_NAME}: {name!r}")


def _read_skill_files(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    if not root.is_dir():
        return files
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            continue
        if any(part.startswith(".") for part in Path(rel).parts):
            continue
        if rel.endswith((".hp111tmp", SKILL_ID_SIDECAR)):
            continue
        try:
            files[rel] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return files


def _atomic_commit(
    dest: Path,
    files: Mapping[str, str],
    *,
    proposal_id: str,
    content_hash: str,
) -> None:
    dest = dest.resolve()
    journal, staging, backup = _paths(dest)
    _wipe(staging)
    _write_journal(
        journal,
        {
            "state": PREPARING,
            "proposal_id": proposal_id,
            "content_hash": content_hash,
            "dest": str(dest),
            "files": sorted(files),
        },
    )
    _write_tree(staging, files)
    _write_journal(
        journal,
        {
            "state": STAGED,
            "proposal_id": proposal_id,
            "content_hash": content_hash,
            "dest": str(dest),
            "files": sorted(files),
        },
    )
    _swap(dest, staging, backup, journal, proposal_id, content_hash, files)
    _cleanup(dest)


def _recover_if_needed(dest: Path, proposal_id: str, content_hash: str) -> bool:
    journal = _journal_path(dest)
    if not journal.is_file():
        return False
    payload = _read_journal(journal)
    if payload is None:
        return False
    if payload.get("proposal_id") != proposal_id:
        return False
    if payload.get("content_hash") != content_hash:
        return False
    return _resume_journal(dest, payload)


def _resume_journal(dest: Path, payload: Mapping[str, Any]) -> bool:
    state = str(payload.get("state") or "")
    journal, staging, backup = _paths(dest)
    proposal_id = str(payload.get("proposal_id") or "")
    content_hash = str(payload.get("content_hash") or "")
    files = payload.get("files") if isinstance(payload.get("files"), list) else []
    if state == PREPARING:
        _wipe(staging)
        journal.unlink(missing_ok=True)
        return False
    if state == STAGED:
        _swap(dest, staging, backup, journal, proposal_id, content_hash, files)
        _cleanup(dest)
        return True
    if state == BACKUP:
        if staging.is_dir():
            dest.parent.mkdir(parents=True, exist_ok=True)
            staging.replace(dest)
        _write_journal(
            journal,
            {
                "state": SWAPPED,
                "proposal_id": proposal_id,
                "content_hash": content_hash,
                "dest": str(dest),
                "files": list(files),
            },
        )
        _cleanup(dest)
        return dest.is_dir()
    if state in {SWAPPED, COMMITTED}:
        _cleanup(dest)
        return dest.is_dir()
    return False


def _swap(
    dest: Path,
    staging: Path,
    backup: Path,
    journal: Path,
    proposal_id: str,
    content_hash: str,
    files: Mapping[str, str] | list[str],
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _wipe(backup)
    if dest.exists():
        dest.replace(backup)
        _write_journal(
            journal,
            {
                "state": BACKUP,
                "proposal_id": proposal_id,
                "content_hash": content_hash,
                "dest": str(dest),
                "files": list(files) if isinstance(files, list) else sorted(files),
            },
        )
    staging.replace(dest)
    _write_journal(
        journal,
        {
            "state": SWAPPED,
            "proposal_id": proposal_id,
            "content_hash": content_hash,
            "dest": str(dest),
            "files": list(files) if isinstance(files, list) else sorted(files),
        },
    )


def _cleanup(dest: Path) -> None:
    journal, staging, backup = _paths(dest)
    _wipe(staging)
    _wipe(backup)
    if journal.is_file():
        payload = _read_journal(journal) or {}
        payload["state"] = COMMITTED
        _write_journal(journal, payload)
        journal.unlink(missing_ok=True)
    parent = _journal_dir(dest.parent)
    if parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()


def _write_tree(root: Path, files: Mapping[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        dest = (root / rel).resolve()
        if not dest.is_relative_to(root.resolve()):
            raise SkillEvolutionAcceptError(f"{UNSAFE_PATH}: {rel}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


def _paths(dest: Path) -> tuple[Path, Path, Path]:
    parent = dest.parent
    name = dest.name
    journal_dir = _journal_dir(parent)
    return (
        journal_dir / f"{name}{JOURNAL_SUFFIX}",
        journal_dir / f"{name}{STAGING_SUFFIX}",
        journal_dir / f"{name}{BACKUP_SUFFIX}",
    )


def _journal_path(dest: Path) -> Path:
    return _paths(dest)[0]


def _journal_dir(skills_parent: Path) -> Path:
    return skills_parent / JOURNAL_DIR_NAME


def _write_journal(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(dict(payload), sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _read_journal(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _wipe(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _mark_applied(
    row: PassProposal,
    dest: Path,
    digest: str,
    *,
    actor: str,
    recovered: bool,
) -> PassProposal:
    payload = dict(row.payload)
    payload["applied"] = True
    payload["draft_only"] = False
    payload["applied_digest"] = digest
    payload["applied_root"] = str(dest)
    payload["applied_recovered"] = recovered
    if actor:
        payload["accepted_by"] = actor.strip()
    try:
        return update_payload(row.id, payload)
    except PassStoreError as exc:
        raise SkillEvolutionAcceptError(str(exc)) from exc


def _record_catalog(catalog: SkillCatalog, row: PassProposal, dest: Path) -> None:
    payload = row.payload
    name = str(payload.get("name") or dest.name)
    origin = str(payload.get("origin") or "imported")
    parents = tuple(str(item) for item in (payload.get("parent_logical_ids") or []) if str(item))
    if catalog.get_by_name(name) is not None:
        origin = "fixed"
        parents = ()
    elif origin == "fixed":
        origin = "imported"
        parents = ()
    try:
        catalog.record(
            name=name,
            files=_proposed_files(row),
            origin=origin,
            provider=f"directory:{dest}",
            parent_logical_ids=parents,
        )
    except SkillCatalogError:
        catalog.record(
            name=name,
            files=_proposed_files(row),
            origin="imported",
            provider=f"directory:{dest}",
        )


def _validation_dict(proposal_id: str) -> dict[str, Any]:
    try:
        return validate_proposal(proposal_id).to_dict()
    except SkillEvolutionValidatorError:
        return {}
