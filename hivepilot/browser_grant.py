"""HP-115 run-scoped browser grant on the existing HP-68 CDP loopback.

Coworker browser-grant pattern, rewritten in Python. This module does
**not** vendor TS/Electron, embed Chromium, launch a browser, or reopen
the HP-67 sandbox-computer spike.

A grant is tied to ``run_id``. ``state_service.complete_run`` (end of
run) revokes it. CDP **actions** refuse without a live grant and never
leave loopback. Dashboard discovery (``host_browser.snapshot``) stays
ungated — listing attached tabs is not an agent action.

Issuing a grant goes through HP-97 PASS (``kind=tool``, token
``BrowserCDP``). Catalog default is ``require_approval`` (HITL).
``match_auto`` may auto-approve only when every HP-97 layer agrees.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

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
from hivepilot.services import db, host_browser, state_service
from hivepilot.services.local_models import is_loopback_url
from hivepilot.tool_catalog import TOOL_APPROVAL_KIND

BROWSER_CDP_TOKEN = "BrowserCDP"
LIVE = "live"
DEAD = "dead"
GRANT_STATUSES: tuple[str, ...] = (LIVE, DEAD)

# Closed CDP action vocabulary. Launch / embed stay refused even with a grant.
CDP_ACTIONS: frozenset[str] = frozenset({"list"})
EMBED_ACTIONS: frozenset[str] = frozenset(
    {
        "launch",
        "embed",
        "chromium",
        "chrome",
        "playwright",
        "puppeteer",
        "pyppeteer",
        "selenium",
    }
)
EMBEDS_CHROMIUM = False

_NOTE_NO_GRANT = "No live run-scoped browser grant. CDP action refused."
_NOTE_DEAD = "Browser grant died with the run. CDP action refused."
_NOTE_EMBED = "HivePilot does not embed Chromium. Launch/embed is refused."
_NOTE_LOOPBACK = "CDP actions stay on the existing loopback endpoint."


class BrowserGrantError(ValueError):
    """Invalid grant request, run, or CDP action."""


@dataclass(frozen=True)
class BrowserGrant:
    """One run-scoped CDP grant. ``status=dead`` after the run ends."""

    id: str
    run_id: int
    status: str
    cdp_url: str
    proposal_id: str = ""
    tenant: str = "default"
    created_at: str = ""
    revoked_at: str = ""

    @property
    def live(self) -> bool:
        return self.status == LIVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "run_id": self.run_id,
            "status": self.status,
            "cdp_url": self.cdp_url,
            "proposal_id": self.proposal_id,
            "tenant": self.tenant,
            "created_at": self.created_at,
            "revoked_at": self.revoked_at,
            "embeds_chromium": EMBEDS_CHROMIUM,
        }


@dataclass(frozen=True)
class GrantIssue:
    """Outcome of ``request``. A PENDING proposal is not a live grant."""

    proposal: PassProposal
    grant: BrowserGrant | None = None

    @property
    def live(self) -> bool:
        return self.grant is not None and self.grant.live

    def to_dict(self) -> dict[str, Any]:
        return {
            "live": self.live,
            "proposal": self.proposal.to_dict(),
            "grant": None if self.grant is None else self.grant.to_dict(),
        }


@dataclass(frozen=True)
class CdpActionResult:
    """Gated CDP action. ``ok=False`` means no CDP side-effect ran."""

    ok: bool
    action: str
    run_id: int
    grant_id: str = ""
    payload: dict[str, Any] | None = None
    error: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "action": self.action,
            "run_id": self.run_id,
            "grant_id": self.grant_id,
            "payload": None if self.payload is None else dict(self.payload),
            "error": self.error,
            "note": self.note,
            "embeds_chromium": EMBEDS_CHROMIUM,
        }


def _ensure_table() -> None:
    state_service.init_db()
    with db.connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS browser_grants (
                id TEXT PRIMARY KEY,
                run_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                cdp_url TEXT NOT NULL,
                proposal_id TEXT NOT NULL DEFAULT '',
                tenant TEXT NOT NULL DEFAULT 'default',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                revoked_at TIMESTAMP
            )
            """
        )


def _row(raw: Any) -> BrowserGrant:
    created_at = raw["created_at"]
    revoked_at = raw["revoked_at"]
    return BrowserGrant(
        id=str(raw["id"]),
        run_id=int(raw["run_id"]),
        status=str(raw["status"]),
        cdp_url=str(raw["cdp_url"]),
        proposal_id=str(raw["proposal_id"] or ""),
        tenant=str(raw["tenant"] or "default"),
        created_at="" if created_at is None else str(created_at),
        revoked_at="" if revoked_at is None else str(revoked_at),
    )


def _normalize_run_id(run_id: int) -> int:
    try:
        cleaned = int(run_id)
    except (TypeError, ValueError) as exc:
        raise BrowserGrantError("run_id must be an integer") from exc
    if cleaned < 1:
        raise BrowserGrantError("run_id must be a positive integer")
    return cleaned


def _normalize_cdp_url(raw: str | None) -> str:
    url = (raw or "").strip() or host_browser.default_cdp_url()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        url = host_browser.DEFAULT_CDP_URL
    if not is_loopback_url(url):
        raise BrowserGrantError("CDP grant is loopback-only; remote CDP is refused")
    return url


def _run_row(run_id: int) -> dict[str, Any]:
    row = state_service.get_run(run_id)
    if row is None:
        raise BrowserGrantError(f"run not found: {run_id}")
    return row


def _run_is_finished(row: Mapping[str, Any]) -> bool:
    finished = row.get("finished_at")
    return finished not in (None, "")


def get(grant_id: str) -> BrowserGrant | None:
    _ensure_table()
    with db.connect() as conn:
        row = conn.execute(
            db.ph("SELECT * FROM browser_grants WHERE id=?"),
            (grant_id,),
        ).fetchone()
    return _row(row) if row else None


def get_live(run_id: int) -> BrowserGrant | None:
    """Return the live grant for ``run_id``, or ``None``.

    An APPROVED PASS card for this run can materialize a grant (presenter
    ``decide`` does not need a custom side-effect). A finished run never
    yields a live grant.
    """
    cleaned = _normalize_run_id(run_id)
    row = state_service.get_run(cleaned)
    if row is None or _run_is_finished(row):
        return None
    _ensure_table()
    with db.connect() as conn:
        stored = conn.execute(
            db.ph(
                "SELECT * FROM browser_grants WHERE run_id=? AND status=? "
                "ORDER BY created_at DESC, id DESC"
            ),
            (cleaned, LIVE),
        ).fetchone()
    if stored is not None:
        return _row(stored)
    return _materialize_approved(cleaned)


def _materialize_approved(run_id: int) -> BrowserGrant | None:
    for proposal in inbox(kind=TOOL_APPROVAL_KIND, status=APPROVED):
        if not _proposal_targets_run(proposal, run_id):
            continue
        existing = _grant_for_proposal(proposal.id)
        if existing is not None:
            return existing if existing.live else None
        return _persist_live(proposal)
    return None


def _proposal_targets_run(proposal: PassProposal, run_id: int) -> bool:
    if (proposal.action or "").strip() != BROWSER_CDP_TOKEN:
        token = str(proposal.payload.get("token") or "")
        if token != BROWSER_CDP_TOKEN:
            return False
    raw = proposal.payload.get("run_id")
    try:
        return int(raw) == run_id
    except (TypeError, ValueError):
        return False


def _grant_for_proposal(proposal_id: str) -> BrowserGrant | None:
    if not proposal_id:
        return None
    _ensure_table()
    with db.connect() as conn:
        row = conn.execute(
            db.ph(
                "SELECT * FROM browser_grants WHERE proposal_id=? ORDER BY created_at DESC, id DESC"
            ),
            (proposal_id,),
        ).fetchone()
    return _row(row) if row else None


def _persist_live(proposal: PassProposal) -> BrowserGrant:
    existing = _grant_for_proposal(proposal.id)
    if existing is not None:
        if existing.live:
            return existing
        raise BrowserGrantError("grant for this approval is already dead")
    run_id = _normalize_run_id(int(proposal.payload.get("run_id")))
    row = _run_row(run_id)
    if _run_is_finished(row):
        raise BrowserGrantError("run is finished; grant is dead")
    cdp_url = _normalize_cdp_url(str(proposal.payload.get("cdp_url") or ""))
    grant_id = uuid.uuid4().hex
    tenant = (proposal.tenant or row.get("tenant") or "default").strip() or "default"
    _ensure_table()
    with db.connect() as conn:
        conn.execute(
            db.ph(
                """
                INSERT INTO browser_grants (
                    id, run_id, status, cdp_url, proposal_id, tenant
                ) VALUES (?, ?, ?, ?, ?, ?)
                """
            ),
            (grant_id, run_id, LIVE, cdp_url, proposal.id, tenant),
        )
    stored = get(grant_id)
    if stored is None:
        raise BrowserGrantError(f"failed to persist grant {grant_id}")
    return stored


def _activate_if_approved(proposal: PassProposal) -> None:
    if proposal.status != APPROVED:
        return
    try:
        _persist_live(proposal)
    except BrowserGrantError:
        return


def request(
    run_id: int,
    *,
    project: str = "",
    task: str = "",
    tenant: str = "default",
    change_class: str = "",
    cdp_url: str | None = None,
    catalog: Any = None,
    policy_overrides: Mapping[str, str] | None = None,
) -> GrantIssue:
    """Ask PASS for a run-scoped CDP grant.

    PENDING / REJECTED / EXPIRED ⇒ no live grant. APPROVED materializes one.
    A finished run cannot receive a new grant.
    """
    cleaned = _normalize_run_id(run_id)
    row = _run_row(cleaned)
    if _run_is_finished(row):
        raise BrowserGrantError("run is finished; grant is dead")
    live = get_live(cleaned)
    if live is not None:
        proposal = get_proposal(live.proposal_id) if live.proposal_id else None
        if proposal is not None:
            return GrantIssue(proposal=proposal, grant=live)
    pending = _pending_for_run(cleaned)
    if pending is not None:
        return GrantIssue(proposal=pending, grant=None)
    url = _normalize_cdp_url(cdp_url)
    project_name = (project or str(row.get("project") or "")).strip()
    task_name = (task or str(row.get("task") or "")).strip()
    tenant_name = (tenant or str(row.get("tenant") or "default")).strip() or "default"
    proposal = submit(
        kind=TOOL_APPROVAL_KIND,
        project=project_name,
        task=task_name,
        action=BROWSER_CDP_TOKEN,
        change_class=change_class,
        payload={
            "token": BROWSER_CDP_TOKEN,
            "run_id": cleaned,
            "cdp_url": url,
        },
        tenant=tenant_name,
        metadata={
            "token": BROWSER_CDP_TOKEN,
            "action": BROWSER_CDP_TOKEN,
            "change_class": change_class,
            "run_id": cleaned,
        },
        catalog=catalog,
        policy_overrides=policy_overrides,
        side_effect=_activate_if_approved,
    )
    grant = get_live(cleaned) if proposal.status == APPROVED else None
    return GrantIssue(proposal=proposal, grant=grant)


def approve(proposal_id: str, *, actor: str = "", reason: str = "") -> GrantIssue:
    """HITL approve. Decision persists before the grant becomes live."""
    cleaned = (proposal_id or "").strip()
    if not cleaned:
        raise BrowserGrantError("proposal_id is required")
    proposal = decide(
        cleaned,
        "approve",
        actor=actor,
        reason=reason,
        side_effect=_activate_if_approved,
    )
    grant = None
    raw = proposal.payload.get("run_id")
    if raw is not None:
        grant = get_live(int(raw))
    return GrantIssue(proposal=proposal, grant=grant)


def revoke_for_run(run_id: int) -> int:
    """Kill every live grant for ``run_id`` and expire pending PASS cards.

    Idempotent. Safe to call from ``complete_run`` (end of run = grant dead).
    """
    try:
        cleaned = int(run_id)
    except (TypeError, ValueError):
        return 0
    if cleaned < 1:
        return 0
    _ensure_table()
    with db.connect() as conn:
        cur = conn.execute(
            db.ph(
                """
                UPDATE browser_grants
                SET status=?, revoked_at=CURRENT_TIMESTAMP
                WHERE run_id=? AND status=?
                """
            ),
            (DEAD, cleaned, LIVE),
        )
        revoked = int(cur.rowcount or 0)
    _expire_pending_for_run(cleaned)
    return revoked


def _pending_for_run(run_id: int) -> PassProposal | None:
    for proposal in inbox(kind=TOOL_APPROVAL_KIND, status=PENDING):
        if _proposal_targets_run(proposal, run_id):
            return proposal
    return None


def _expire_pending_for_run(run_id: int) -> None:
    for proposal in inbox(kind=TOOL_APPROVAL_KIND, status=PENDING):
        if not _proposal_targets_run(proposal, run_id):
            continue
        if proposal.status != PENDING:
            continue
        decide(
            proposal.id,
            "expire",
            actor="run",
            reason="run ended; browser grant is dead",
        )


def act(
    run_id: int,
    action: str,
    *,
    base_url: str | None = None,
) -> CdpActionResult:
    """Run one CDP action. No live grant ⇒ refuse (no CDP I/O)."""
    cleaned = _normalize_run_id(run_id)
    token = (action or "").strip().lower()
    if token in EMBED_ACTIONS:
        return CdpActionResult(
            ok=False,
            action=token,
            run_id=cleaned,
            error="refused: no Chromium embed path",
            note=_NOTE_EMBED,
        )
    if token not in CDP_ACTIONS:
        return CdpActionResult(
            ok=False,
            action=token or action,
            run_id=cleaned,
            error=f"unknown CDP action: {action!r}",
            note=_NOTE_LOOPBACK,
        )
    grant = get_live(cleaned)
    if grant is None:
        row = state_service.get_run(cleaned)
        dead = row is not None and _run_is_finished(row)
        return CdpActionResult(
            ok=False,
            action=token,
            run_id=cleaned,
            error="refused: no live browser grant",
            note=_NOTE_DEAD if dead else _NOTE_NO_GRANT,
        )
    url = grant.cdp_url
    if base_url:
        try:
            url = _normalize_cdp_url(base_url)
        except BrowserGrantError as exc:
            return CdpActionResult(
                ok=False,
                action=token,
                run_id=cleaned,
                grant_id=grant.id,
                error=str(exc),
                note=_NOTE_LOOPBACK,
            )
        if url != grant.cdp_url:
            return CdpActionResult(
                ok=False,
                action=token,
                run_id=cleaned,
                grant_id=grant.id,
                error="refused: action URL is not the granted loopback CDP",
                note=_NOTE_LOOPBACK,
            )
    if token == "list":
        snapshot = host_browser.snapshot(base_url=url)
        return CdpActionResult(
            ok=True,
            action=token,
            run_id=cleaned,
            grant_id=grant.id,
            payload=snapshot,
            note=_NOTE_LOOPBACK,
        )
    return CdpActionResult(
        ok=False,
        action=token,
        run_id=cleaned,
        grant_id=grant.id,
        error=f"unknown CDP action: {action!r}",
        note=_NOTE_LOOPBACK,
    )
