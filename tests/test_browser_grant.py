"""HP-115: run-scoped CDP grant. End of run = grant dead. No Chromium."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from hivepilot.browser_grant import (
    BROWSER_CDP_TOKEN,
    EMBED_ACTIONS,
    EMBEDS_CHROMIUM,
    LIVE,
    act,
    approve,
    get_live,
    request,
    revoke_for_run,
)
from hivepilot.pass_store import APPROVED, EXPIRED, PENDING
from hivepilot.services import approval_rules_service, host_browser, state_service
from hivepilot.services.sandbox_computers import DECISION as SANDBOX_DECISION
from hivepilot.tool_catalog import load_catalog

_GRANT_SOURCE = Path(__file__).resolve().parents[1] / "hivepilot" / "browser_grant.py"
_HOST_SOURCE = Path(__file__).resolve().parents[1] / "hivepilot" / "services" / "host_browser.py"
_SANDBOX_SOURCE = (
    Path(__file__).resolve().parents[1] / "hivepilot" / "services" / "sandbox_computers.py"
)

_BANNED_IMPORTS = (
    "playwright",
    "pyppeteer",
    "puppeteer",
    "selenium",
    "patchright",
)


def _catalog(tmp_path: Path, *, policy: str = "require_approval"):
    path = tmp_path / "tool_catalog.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "tools": [
                    {
                        "token": BROWSER_CDP_TOKEN,
                        "risk": "high",
                        "defaultPolicy": policy,
                        "volatile": True,
                        "idempotency": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return load_catalog(path, force=True)


def _start_run() -> int:
    return state_service.record_run_start("example-api", "docs")


def _issue_live(run_id: int, tmp_path: Path, *, cdp_url: str | None = None):
    issued = request(run_id, cdp_url=cdp_url, catalog=_catalog(tmp_path))
    return approve(issued.proposal.id, actor="jerome")


class _Probe:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *_a: Any, **_k: Any) -> Any:
        self.calls += 1
        raise AssertionError("CDP must not be contacted")


def test_no_grant_refuses_cdp_action_without_contact(monkeypatch) -> None:
    run_id = _start_run()
    probe = _Probe()
    monkeypatch.setattr(host_browser.urllib.request, "urlopen", probe)
    result = act(run_id, "list")
    assert result.ok is False
    assert result.error.startswith("refused: no live browser grant")
    assert get_live(run_id) is None
    assert probe.calls == 0


def test_pending_hitl_is_not_a_live_grant(tmp_path: Path, monkeypatch) -> None:
    run_id = _start_run()
    issued = request(run_id, catalog=_catalog(tmp_path))
    assert issued.live is False
    assert issued.proposal.status == PENDING
    assert issued.grant is None
    probe = _Probe()
    monkeypatch.setattr(host_browser.urllib.request, "urlopen", probe)
    result = act(run_id, "list")
    assert result.ok is False
    assert probe.calls == 0


def test_approve_then_list_uses_loopback(tmp_path: Path, monkeypatch) -> None:
    run_id = _start_run()
    issued = request(run_id, catalog=_catalog(tmp_path))
    approved = approve(issued.proposal.id, actor="jerome")
    assert approved.live is True
    assert approved.grant is not None
    assert approved.grant.status == LIVE
    assert approved.grant.run_id == run_id
    assert approved.grant.cdp_url.startswith("http://127.0.0.1")

    payload = {
        "attached": True,
        "base_url": approved.grant.cdp_url,
        "tabs": [{"id": "tab-1", "title": "Docs", "url": "https://example.com", "type": "page"}],
        "note": "loopback",
        "error": None,
    }
    monkeypatch.setattr(host_browser, "snapshot", lambda **_k: payload)
    result = act(run_id, "list")
    assert result.ok is True
    assert result.grant_id == approved.grant.id
    assert result.payload == payload


def test_grant_dies_with_complete_run(tmp_path: Path, monkeypatch) -> None:
    run_id = _start_run()
    issued = _issue_live(run_id, tmp_path)
    assert issued.live is True
    assert get_live(run_id) is not None
    state_service.complete_run(run_id, "success")
    assert get_live(run_id) is None
    probe = _Probe()
    monkeypatch.setattr(host_browser.urllib.request, "urlopen", probe)
    result = act(run_id, "list")
    assert result.ok is False
    assert "dead" in result.note.lower() or "no live" in result.error
    assert probe.calls == 0
    try:
        request(run_id, catalog=_catalog(tmp_path))
        raised = False
    except Exception as exc:  # noqa: BLE001
        raised = True
        assert "dead" in str(exc).lower() or "finished" in str(exc).lower()
    assert raised


def test_pending_expires_when_run_ends(tmp_path: Path) -> None:
    run_id = _start_run()
    issued = request(run_id, catalog=_catalog(tmp_path))
    assert issued.proposal.status == PENDING
    state_service.complete_run(run_id, "failed", "boom")
    from hivepilot.pass_store import get

    stored = get(issued.proposal.id)
    assert stored is not None
    assert stored.status == EXPIRED
    assert get_live(run_id) is None


def test_revoke_is_idempotent(tmp_path: Path) -> None:
    run_id = _start_run()
    _issue_live(run_id, tmp_path)
    assert revoke_for_run(run_id) == 1
    assert revoke_for_run(run_id) == 0
    assert get_live(run_id) is None


def test_grant_does_not_cover_another_run(tmp_path: Path, monkeypatch) -> None:
    first = _start_run()
    second = _start_run()
    _issue_live(first, tmp_path)
    probe = _Probe()
    monkeypatch.setattr(host_browser.urllib.request, "urlopen", probe)
    result = act(second, "list")
    assert result.ok is False
    assert probe.calls == 0
    assert get_live(second) is None


def test_remote_cdp_refused_on_request(tmp_path: Path) -> None:
    run_id = _start_run()
    try:
        request(
            run_id,
            cdp_url="http://10.0.0.8:9222",
            catalog=_catalog(tmp_path, policy="allow"),
        )
        raised = False
    except Exception as exc:  # noqa: BLE001
        raised = True
        assert "loopback" in str(exc).lower()
    assert raised
    assert get_live(run_id) is None


def test_granted_action_refuses_remote_override(tmp_path: Path, monkeypatch) -> None:
    run_id = _start_run()
    _issue_live(run_id, tmp_path)
    probe = _Probe()
    monkeypatch.setattr(host_browser.urllib.request, "urlopen", probe)
    result = act(run_id, "list", base_url="http://8.8.8.8:9222")
    assert result.ok is False
    assert "loopback" in result.error.lower()
    assert probe.calls == 0


def test_match_auto_mechanical_allow_activates(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path, policy="allow")
    approval_rules_service.replace_rules(
        [
            {
                "project": "example-api",
                "task": "docs",
                "action": BROWSER_CDP_TOKEN,
                "auto": "approve",
                "change_class": "mechanical",
            }
        ]
    )
    run_id = _start_run()
    issued = request(run_id, change_class="mechanical", catalog=catalog)
    assert issued.proposal.status == APPROVED
    assert issued.live is True
    assert get_live(run_id) is not None


def test_no_chromium_embed_path(tmp_path: Path) -> None:
    assert EMBEDS_CHROMIUM is False
    run_id = _start_run()
    _issue_live(run_id, tmp_path)
    for action in sorted(EMBED_ACTIONS):
        result = act(run_id, action)
        assert result.ok is False
        assert "chromium" in result.error.lower() or "embed" in result.error.lower()
    grant_src = _GRANT_SOURCE.read_text(encoding="utf-8")
    host_src = _HOST_SOURCE.read_text(encoding="utf-8")
    for needle in _BANNED_IMPORTS:
        assert f"import {needle}" not in grant_src
        assert f"from {needle}" not in grant_src
        assert f"import {needle}" not in host_src
        assert f"from {needle}" not in host_src
    assert "def launch" not in grant_src
    assert "Popen" not in grant_src
    assert "subprocess" not in grant_src
    assert SANDBOX_DECISION == "no-go"
    sandbox_src = _SANDBOX_SOURCE.read_text(encoding="utf-8")
    assert 'DECISION: Literal["no-go"] = "no-go"' in sandbox_src


def test_snapshot_discovery_stays_ungated(monkeypatch) -> None:
    monkeypatch.setattr(
        host_browser,
        "snapshot",
        lambda **_k: {
            "attached": False,
            "base_url": "http://127.0.0.1:9222",
            "tabs": [],
            "note": "discovery",
            "error": None,
        },
    )
    snap = host_browser.snapshot()
    assert snap["tabs"] == []
    assert get_live(1) is None


def test_presenter_approve_materializes_grant(tmp_path: Path) -> None:
    run_id = _start_run()
    issued = request(run_id, catalog=_catalog(tmp_path))
    from hivepilot.pass_store import decide

    decided = decide(issued.proposal.id, "approve", actor="pollen")
    assert decided.status == APPROVED
    grant = get_live(run_id)
    assert grant is not None
    assert grant.live is True
    assert grant.proposal_id == issued.proposal.id
