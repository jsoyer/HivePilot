"""HP-116 ``schedules.create`` HITL — PASS, class ≠ mechanical.

Coworker ``schedules.create`` HITL pattern, rewritten in Python. This
module does **not** vendor TS, run the scheduler daemon, or auto-approve
standing automations.

Creating a schedule is always a HP-97 PASS card (``kind=tool``, token
``schedules.create``). The change class is forced to ``product_fork``:
a caller or HP-61 rule that claims ``mechanical`` cannot auto-approve.
Catalog default is ``require_approval``; high risk plus a non-mechanical
class also blocks a ``tool_policies`` widen to ``allow``.

Any workspace ``path`` / ``cwd`` / ``workspace`` in the payload is
confined by :mod:`hivepilot.workspace_paths` before PENDING is written.
The YAML write runs only after APPROVED is persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from hivepilot.config import settings
from hivepilot.pass_store import (
    APPROVED,
    PENDING,
    PassProposal,
    decide,
    inbox,
    submit,
)
from hivepilot.pass_store import (
    get as get_proposal,
)
from hivepilot.services.approval_rules_service import PRODUCT_FORK
from hivepilot.services.schedule_service import ScheduleEntry
from hivepilot.tool_catalog import TOOL_APPROVAL_KIND
from hivepilot.workspace_paths import WorkspacePathError, confine

SCHEDULES_CREATE_TOKEN = "schedules.create"
SCHEDULES_CREATE_CLASS = PRODUCT_FORK
PATH_FIELDS: tuple[str, ...] = ("path", "cwd", "workspace")

_NAME_MAX = 80


class ScheduleCreateError(ValueError):
    """Invalid schedule create request or apply."""


@dataclass(frozen=True)
class ScheduleCreateIssue:
    """Outcome of ``request``. PENDING is not a written schedule."""

    proposal: PassProposal
    written: bool = False
    path: Path | None = None

    @property
    def pending(self) -> bool:
        return self.proposal.status == PENDING

    def to_dict(self) -> dict[str, Any]:
        return {
            "written": self.written,
            "pending": self.pending,
            "proposal": self.proposal.to_dict(),
            "path": None if self.path is None else str(self.path),
        }


def _normalize_name(raw: object) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ScheduleCreateError("schedule name must be a non-empty str")
    name = raw.strip()
    if len(name) > _NAME_MAX:
        raise ScheduleCreateError(f"schedule name longer than {_NAME_MAX} characters")
    if name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise ScheduleCreateError("schedule name must not be a path")
    if not all(part.isalnum() or part in {"-", "_"} for part in name):
        raise ScheduleCreateError("schedule name must be [A-Za-z0-9_-]+")
    return name


def _normalize_projects(raw: object) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ScheduleCreateError("projects must be a list of names")
    projects: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise ScheduleCreateError("each project must be a non-empty str")
        projects.append(item.strip())
    return projects


def _normalize_interval(raw: object) -> int:
    if raw is None:
        return 1440
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        raise ScheduleCreateError("interval_minutes must be a positive int")
    return raw


def _confine_payload_paths(
    payload: Mapping[str, Any],
    workspace_root: Path | None,
) -> dict[str, Any]:
    out = dict(payload)
    for key in PATH_FIELDS:
        if key not in out or out[key] in (None, ""):
            continue
        if workspace_root is None:
            raise ScheduleCreateError(f"{key} requires workspace_root")
        result = confine(out[key], root=workspace_root)
        if not result.ok:
            raise WorkspacePathError(result.reason or result.code)
        out[key] = result.rel
    return out


def _schedules_file(raw: str | Path | None, workspace_root: Path | None) -> Path:
    if raw in (None, ""):
        return settings.resolve_config_path(settings.schedules_file)
    path = Path(raw).expanduser()
    if workspace_root is not None:
        root = Path(workspace_root).resolve()
        if not path.is_absolute():
            result = confine(path.as_posix(), root=root)
            if not result.ok or result.path is None:
                raise WorkspacePathError(result.reason or result.code)
            return result.path
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
        except (OSError, ValueError) as exc:
            raise WorkspacePathError("schedules file escapes the workspace") from exc
        return resolved
    if not path.is_absolute():
        return settings.resolve_config_path(path)
    return path.resolve()


def _entry_mapping(payload: Mapping[str, Any]) -> dict[str, Any]:
    task = payload.get("task")
    source = payload.get("source")
    entry: dict[str, Any] = {
        "projects": list(payload.get("projects") or []),
        "interval_minutes": int(payload.get("interval_minutes") or 1440),
        "enabled": bool(payload.get("enabled", True)),
    }
    if task:
        entry["task"] = task
    if source:
        entry["source"] = source
    if payload.get("remember"):
        entry["remember"] = True
    # Validate with the same dataclass the daemon loads.
    ScheduleEntry(name=str(payload.get("name") or ""), **entry)
    return entry


def _load_schedules_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schedules": {}}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        raise ScheduleCreateError("schedules.yaml must be a mapping")
    section = loaded.get("schedules")
    if section is None:
        loaded["schedules"] = {}
    elif not isinstance(section, dict):
        raise ScheduleCreateError("schedules.yaml key 'schedules' must be a mapping")
    return loaded


def _write_schedule(proposal: PassProposal) -> Path:
    payload = proposal.payload
    name = str(payload.get("name") or "")
    path = Path(str(payload.get("schedules_file") or ""))
    if not name or not path:
        raise ScheduleCreateError("approved proposal is missing name or schedules_file")
    data = _load_schedules_yaml(path)
    existing = data["schedules"].get(name)
    entry = _entry_mapping(payload)
    if existing is not None and existing != entry:
        raise ScheduleCreateError(f"schedule {name!r} already exists")
    data["schedules"][name] = entry
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def _apply_if_approved(proposal: PassProposal) -> None:
    if proposal.status != APPROVED:
        return
    try:
        _write_schedule(proposal)
    except ScheduleCreateError:
        return


def _pending_for_name(name: str, schedules_file: Path) -> PassProposal | None:
    target = str(schedules_file)
    for proposal in inbox(kind=TOOL_APPROVAL_KIND, status=PENDING):
        if (proposal.action or "").strip() != SCHEDULES_CREATE_TOKEN:
            continue
        if str(proposal.payload.get("name") or "") != name:
            continue
        if str(proposal.payload.get("schedules_file") or "") == target:
            return proposal
    return None


def request(
    *,
    name: str,
    task: str | None = None,
    projects: Sequence[str] | None = None,
    interval_minutes: int = 1440,
    enabled: bool = True,
    source: str | None = None,
    remember: bool = False,
    path: str | None = None,
    cwd: str | None = None,
    workspace: str | None = None,
    workspace_root: Path | str | None = None,
    schedules_file: str | Path | None = None,
    project: str = "",
    change_class: str = "",
    tenant: str = "default",
    catalog: Any = None,
    policy_overrides: Mapping[str, str] | None = None,
) -> ScheduleCreateIssue:
    """Ask PASS to create a schedule. PENDING / reject ⇒ YAML unchanged.

    ``change_class`` from the caller is ignored. The stored class is
    always :data:`SCHEDULES_CREATE_CLASS` (not mechanical).
    """
    change_class = SCHEDULES_CREATE_CLASS
    cleaned_name = _normalize_name(name)
    cleaned_projects = _normalize_projects(projects)
    cleaned_interval = _normalize_interval(interval_minutes)
    cleaned_task = (task or "").strip() or None
    cleaned_source = (source or "").strip() or None
    # Fail closed on the same invariant the daemon loader enforces.
    ScheduleEntry(
        name=cleaned_name,
        projects=cleaned_projects,
        task=cleaned_task,
        interval_minutes=cleaned_interval,
        enabled=bool(enabled),
        source=cleaned_source,
        remember=bool(remember),
    )
    root = Path(workspace_root).resolve() if workspace_root is not None else None
    file_path = _schedules_file(schedules_file, root)
    pending = _pending_for_name(cleaned_name, file_path)
    if pending is not None:
        return ScheduleCreateIssue(proposal=pending, written=False, path=file_path)

    payload: dict[str, Any] = {
        "token": SCHEDULES_CREATE_TOKEN,
        "name": cleaned_name,
        "projects": cleaned_projects,
        "interval_minutes": cleaned_interval,
        "enabled": bool(enabled),
        "remember": bool(remember),
        "schedules_file": str(file_path),
    }
    if cleaned_task:
        payload["task"] = cleaned_task
    if cleaned_source:
        payload["source"] = cleaned_source
    if path is not None:
        payload["path"] = path
    if cwd is not None:
        payload["cwd"] = cwd
    if workspace is not None:
        payload["workspace"] = workspace
    payload = _confine_payload_paths(payload, root)

    existing = _load_schedules_yaml(file_path)["schedules"].get(cleaned_name)
    if existing is not None:
        raise ScheduleCreateError(f"schedule {cleaned_name!r} already exists")

    proposal = submit(
        kind=TOOL_APPROVAL_KIND,
        project=project,
        task=cleaned_task or cleaned_source or "",
        action=SCHEDULES_CREATE_TOKEN,
        change_class=change_class,
        payload=payload,
        tenant=tenant or "default",
        metadata={
            "token": SCHEDULES_CREATE_TOKEN,
            "action": SCHEDULES_CREATE_TOKEN,
            "change_class": SCHEDULES_CREATE_CLASS,
        },
        catalog=catalog,
        policy_overrides=policy_overrides,
        side_effect=_apply_if_approved,
    )
    written = (
        proposal.status == APPROVED and cleaned_name in _load_schedules_yaml(file_path)["schedules"]
    )
    return ScheduleCreateIssue(proposal=proposal, written=written, path=file_path)


def approve(proposal_id: str, *, actor: str = "", reason: str = "") -> ScheduleCreateIssue:
    """HITL approve. Decision persists before the YAML write."""
    cleaned = (proposal_id or "").strip()
    if not cleaned:
        raise ScheduleCreateError("proposal_id is required")
    proposal = decide(
        cleaned,
        "approve",
        actor=actor,
        reason=reason,
        side_effect=_apply_if_approved,
    )
    path = Path(str(proposal.payload.get("schedules_file") or ""))
    name = str(proposal.payload.get("name") or "")
    written = False
    if path and name:
        written = name in _load_schedules_yaml(path).get("schedules", {})
    return ScheduleCreateIssue(
        proposal=proposal,
        written=written,
        path=path if path else None,
    )


def get(proposal_id: str) -> PassProposal | None:
    return get_proposal(proposal_id)
