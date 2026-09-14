"""HP-118 local task-trace export — redacted ZIP, no upload.

OpenSpace task-traces export pattern, rewritten in Python. This module
does **not** vendor OpenSpace, talk to OpenSpace cloud, persist pickle
embeddings, or implement a reporter that leaves this machine.

Contracts:

- A project run projects to metadata / tools / skills / redaction.
- ZIP is assembled on demand and written only to a local path.
- Critical findings block export (no archive written).
- Destination must be a filesystem path. There is no upload API.
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from hivepilot.evidence import EvidenceRef, build_packet, list_refs, normalize_tenant
from hivepilot.services import state_service
from hivepilot.services.config_provenance import redact_value
from hivepilot.skill_events import list_skill_events

SCHEMA_VERSION = "1"
EXPORT_KIND = "local"
UPLOAD = False
CRITICAL = "critical"
REDACTION_STATUS = "redacted"
ARCHIVE_MEMBERS: tuple[str, ...] = (
    "manifest.json",
    "metadata.json",
    "tools.json",
    "skills.json",
    "redaction.json",
)
_REMOTE_PREFIXES: tuple[str, ...] = (
    "http://",
    "https://",
    "s3://",
    "gs://",
    "ftp://",
)


class TraceExportError(ValueError):
    """Invalid run, tenant, or local destination."""


class CriticalFindingsBlock(TraceExportError):
    """Critical findings refuse the export. No ZIP is written."""

    def __init__(self, findings: tuple[dict[str, Any], ...]) -> None:
        self.findings = findings
        count = len(findings)
        super().__init__(
            f"critical findings block export ({count}): {_summarize_critical(findings)}"
        )


@dataclass(frozen=True)
class RunProjection:
    """Redacted local view of one project run."""

    run_id: int
    tenant: str
    metadata: dict[str, Any]
    tools: dict[str, Any]
    skills: dict[str, Any]
    redaction: dict[str, Any]
    critical_findings: tuple[dict[str, Any], ...] = ()

    def blocked(self) -> bool:
        return bool(self.critical_findings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "tenant": self.tenant,
            "metadata": dict(self.metadata),
            "tools": dict(self.tools),
            "skills": dict(self.skills),
            "redaction": dict(self.redaction),
            "critical_findings": [dict(item) for item in self.critical_findings],
        }

    def archive_members(self) -> dict[str, Any]:
        return {
            "manifest.json": {
                "schema_version": SCHEMA_VERSION,
                "kind": EXPORT_KIND,
                "upload": UPLOAD,
                "run_id": self.run_id,
                "tenant": self.tenant,
                "redaction_status": REDACTION_STATUS,
                "members": list(ARCHIVE_MEMBERS[1:]),
            },
            "metadata.json": self.metadata,
            "tools.json": self.tools,
            "skills.json": self.skills,
            "redaction.json": self.redaction,
        }


def resolve_local_zip_path(dest: str | Path) -> Path:
    """Accept only a local filesystem path. Remote URLs are refused."""
    raw = str(dest).strip()
    if not raw:
        raise TraceExportError("output path is required")
    lowered = raw.lower()
    if lowered.startswith(_REMOTE_PREFIXES):
        raise TraceExportError("destination must be a local path (no upload)")
    path = Path(raw).expanduser()
    if path.suffix.lower() != ".zip":
        path = path.with_name(f"{path.name}.zip")
    return path


def project_run(run_id: int, *, tenant: str = "default") -> RunProjection:
    """Assemble the redacted metadata / tools / skills / redaction view."""
    scoped = normalize_tenant(tenant)
    run = _load_run(run_id, tenant=scoped)
    steps = [_public_step(row) for row in state_service.get_steps_for_run(run_id)]
    interactions = [
        _public_interaction(row)
        for row in state_service.list_recent_interactions(limit=200, run_id=run_id)
    ]
    skill_rows = [
        event.to_dict() for event in list_skill_events(run_id=run_id, tenant=scoped, limit=1000)
    ]
    evidence_refs = _refs_for_run(run_id, tenant=scoped)
    tool_refs = [ref.to_dict() for ref in evidence_refs if ref.ref_type == "tool_event"]
    packet = build_packet(
        [ref.ref_id for ref in evidence_refs],
        tenant=scoped,
        packet_id=f"run-{run_id}-traces",
    )
    critical = _critical_from_verdicts(run_id)
    projection = RunProjection(
        run_id=run_id,
        tenant=scoped,
        metadata={
            "run": _public_run(run),
            "interactions": interactions,
            "failed_steps": [_public_step(row) for row in state_service.list_failed_steps(run_id)],
        },
        tools={"steps": steps, "evidence": tool_refs},
        skills={"events": skill_rows},
        redaction={
            "status": REDACTION_STATUS,
            "packet": packet.to_dict(),
            "ref_ids": [ref.ref_id for ref in evidence_refs],
        },
        critical_findings=critical,
    )
    return _redact_projection(projection)


def export_zip(
    run_id: int,
    dest: str | Path,
    *,
    tenant: str = "default",
) -> Path:
    """Write the redacted projection to a local ZIP. Critical findings refuse."""
    path = resolve_local_zip_path(dest)
    projection = project_run(run_id, tenant=tenant)
    if projection.blocked():
        raise CriticalFindingsBlock(projection.critical_findings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, payload in projection.archive_members().items():
                archive.writestr(
                    name,
                    json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
                )
        tmp.replace(path)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
    return path


def _load_run(run_id: int, *, tenant: str) -> dict[str, Any]:
    run = state_service.get_run(run_id)
    if run is None or str(run.get("tenant") or "default") != tenant:
        raise TraceExportError(f"run {run_id} not found for tenant {tenant!r}")
    return run


def _refs_for_run(run_id: int, *, tenant: str) -> list[EvidenceRef]:
    matched: list[EvidenceRef] = []
    target = str(run_id)
    for ref in list_refs(tenant=tenant):
        raw = ref.metadata.get("run_id")
        if raw is not None and str(raw).strip() == target:
            matched.append(ref)
    return matched


def _critical_from_verdicts(run_id: int) -> tuple[dict[str, Any], ...]:
    found: list[dict[str, Any]] = []
    for verdict in state_service.list_recent_verdicts(limit=200, run_id=run_id):
        for finding in _findings_from_verdict(verdict):
            severity = str(finding.get("severity") or "").strip().lower()
            if severity != CRITICAL:
                continue
            found.append(
                {
                    "severity": CRITICAL,
                    "message": str(finding.get("message") or finding.get("why") or ""),
                    "path": finding.get("path"),
                    "line": finding.get("line"),
                    "source": verdict.get("kind"),
                    "verdict_id": verdict.get("id"),
                }
            )
    return tuple(found)


def _findings_from_verdict(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = row.get("findings_json")
    if not raw:
        return []
    payload: Any = raw
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return []
    items: list[Any]
    if isinstance(payload, dict):
        nested = payload.get("findings")
        items = list(nested) if isinstance(nested, list) else []
        if str(payload.get("severity") or "").strip().lower() == CRITICAL:
            items.append(payload)
    elif isinstance(payload, list):
        items = payload
    else:
        return []
    return [item for item in items if isinstance(item, dict)]


def _public_run(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "project": row.get("project"),
        "task": row.get("task"),
        "status": row.get("status"),
        "detail": row.get("detail"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "tenant": row.get("tenant"),
    }


def _public_step(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "step": row.get("step"),
        "status": row.get("status"),
        "detail": row.get("detail"),
        "provider": row.get("provider"),
        "model": row.get("model"),
        "role": row.get("role"),
        "timestamp": row.get("timestamp"),
    }


def _public_interaction(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "actor": row.get("actor"),
        "action": row.get("action"),
        "target": row.get("target"),
        "summary": row.get("summary"),
        "timestamp": row.get("timestamp"),
    }


def _redact_projection(projection: RunProjection) -> RunProjection:
    body = _jsonable(projection.to_dict())
    return RunProjection(
        run_id=int(body["run_id"]),
        tenant=str(body["tenant"]),
        metadata=body["metadata"],
        tools=body["tools"],
        skills=body["skills"],
        redaction=body["redaction"],
        critical_findings=tuple(body["critical_findings"]),
    )


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(redact_value(value), default=str, ensure_ascii=False))


def _summarize_critical(findings: tuple[dict[str, Any], ...]) -> str:
    messages = []
    for item in findings[:3]:
        message = str(item.get("message") or "").strip() or "critical finding"
        path = item.get("path")
        messages.append(f"{path}: {message}" if path else message)
    extra = len(findings) - len(messages)
    if extra > 0:
        messages.append(f"+{extra} more")
    return "; ".join(messages)
