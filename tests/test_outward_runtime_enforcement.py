"""Runtime enforcement of the three outward tokens `auto_git` never covered:
`notify`, `vault_write` and `external_api` (propose -> ratify -> dispatch PRD,
spec section 6 and open question 4 -- the gap v1 declared loudly).

Every test here fails against the pre-fix tree: `hivepilot.outward` does not
exist there, `Orchestrator.run_pipeline` has no `outward=` keyword, and the
three choke points have no gate at all -- a partition ratified with
`outward_consent=false` still posted to Telegram, still wrote into the
operator's vault, and still routed into a `network`-capable plugin.

The carrier's own unit tests (fail-closed rules, `narrow`, the thread hop)
live in `tests/test_outward.py`.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from hivepilot import outward
from hivepilot.orchestrator import Orchestrator
from hivepilot.services import (
    notification_service,
    partition_service,
)
from hivepilot.services.notification_service import (
    send_approval_keyboard as _real_send_approval_keyboard,
)
from hivepilot.services.obsidian_service import ObsidianService

# `tests/conftest.py`'s autouse `_no_outbound_notifications` replaces
# `notification_service.send_approval_keyboard` with a no-op for every test in
# the suite. Bound at import time above, `_real_send_approval_keyboard` is the
# genuine function -- otherwise this module would be asserting about the
# conftest stub instead of about the gate.

# ---------------------------------------------------------------------------
# Live-config fixture. `acme-api` allowlists ONLY git/forge (so consent there
# still suppresses the three new tokens -- the allowlist is the ceiling a
# ticked checkbox is measured against); `acme-open` allowlists all three.
# ---------------------------------------------------------------------------

PROJECTS_YAML = """
projects:
  acme-api:
    path: /tmp/acme-api
  acme-open:
    path: /tmp/acme-open
"""

PIPELINES_YAML = """
pipelines:
  bugfix:
    description: fix a bug
    stages:
      - name: fix
        task: implement
"""

TASKS_YAML = """
tasks:
  implement:
    description: implement something
    role: developer
"""

POLICIES_YAML = """
policies:
  default:
    require_approval: true
  projects:
    acme-api:
      outward_actions:
        - git_push
        - forge_pr
      budget_daily_usd: 100.0
      max_partition_cost_usd: 50.0
      max_task_wall_clock_seconds: 3600
    acme-open:
      outward_actions:
        - git_push
        - forge_pr
        - notify
        - vault_write
        - external_api
      budget_daily_usd: 100.0
      max_partition_cost_usd: 50.0
      max_task_wall_clock_seconds: 3600
"""


@pytest.fixture
def live_config(tmp_path, monkeypatch):
    from hivepilot.config import settings
    from hivepilot.services import policy_service

    (tmp_path / "projects.yaml").write_text(PROJECTS_YAML, encoding="utf-8")
    (tmp_path / "pipelines.yaml").write_text(PIPELINES_YAML, encoding="utf-8")
    (tmp_path / "tasks.yaml").write_text(TASKS_YAML, encoding="utf-8")
    (tmp_path / "policies.yaml").write_text(POLICIES_YAML, encoding="utf-8")
    monkeypatch.setattr(settings, "config_repo", str(tmp_path), raising=False)
    policy_service.reload_policies()
    yield tmp_path
    policy_service.reload_policies()


@pytest.fixture
def sent(monkeypatch) -> list[str]:
    """Capture whatever actually reaches a notifier channel."""
    delivered: list[str] = []
    monkeypatch.setitem(
        notification_service.NOTIFIER_MAP, "slack", lambda message: delivered.append(message)
    )
    return delivered


def _task(task_id: str = "t1", project: str = "acme-api") -> dict[str, Any]:
    return {
        "id": task_id,
        "title": f"Task {task_id}",
        "project": project,
        "pipeline": "bugfix",
        "prompt": f"do the {task_id} work",
        "depends_on": [],
        "budget": {"wall_clock_seconds": 30, "cost_usd": 1.0},
        "done_when": ["a repro test passes"],
        "outward": False,
    }


# ---------------------------------------------------------------------------
# 1. The permission the dispatcher computes
# ---------------------------------------------------------------------------


def test_no_consent_yields_a_permission_that_allows_nothing(live_config) -> None:
    perm = partition_service.task_outward_permission(
        SimpleNamespace(id="t1", project="acme-api"),
        outward_consent=False,
        partition_id="p1",
    )
    assert perm.restricted is True
    for action in outward.OUTWARD_ACTIONS:
        assert perm.allows(action) is False


def test_consent_yields_exactly_the_project_allowlist(live_config) -> None:
    perm = partition_service.task_outward_permission(
        SimpleNamespace(id="t1", project="acme-open"),
        outward_consent=True,
        partition_id="p1",
    )
    assert perm.allows("notify") is True
    assert perm.allows("vault_write") is True
    assert perm.allows("external_api") is True


def test_consent_never_widens_beyond_the_allowlist(live_config) -> None:
    """`acme-api` allowlists git/forge only, so a ticked checkbox still
    suppresses notify/vault_write/external_api. The allowlist is the ceiling
    consent is measured against, not a starting point."""
    perm = partition_service.task_outward_permission(
        SimpleNamespace(id="t1", project="acme-api"),
        outward_consent=True,
        partition_id="p1",
    )
    assert perm.allows("git_push") is True
    assert perm.allows("notify") is False
    assert perm.allows("vault_write") is False
    assert perm.allows("external_api") is False


def test_consent_never_grants_forge_merge(live_config, monkeypatch) -> None:
    """Runtime backstop for the never-auto-merge invariant: even a project
    that (wrongly) allowlists `forge_merge` cannot get it at dispatch."""
    from hivepilot.services import policy_service

    (Path(live_config) / "policies.yaml").write_text(
        POLICIES_YAML.replace(
            "        - external_api", "        - external_api\n        - forge_merge"
        ),
        encoding="utf-8",
    )
    policy_service.reload_policies()
    perm = partition_service.task_outward_permission(
        SimpleNamespace(id="t1", project="acme-open"),
        outward_consent=True,
        partition_id="p1",
    )
    assert perm.allows("forge_merge") is False


def test_unresolvable_policy_suppresses_rather_than_allows(live_config, monkeypatch) -> None:
    """An UNSET/unreadable permission must deny. This is the repo's most
    repeated bug class (absent value read as 'no constraint')."""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("policies.yaml unreadable")

    monkeypatch.setattr(partition_service, "get_autopilot_policy", _boom)
    perm = partition_service.task_outward_permission(
        SimpleNamespace(id="t1", project="acme-open"),
        outward_consent=True,
        partition_id="p1",
    )
    assert perm.allowed == frozenset()
    for action in outward.OUTWARD_ACTIONS:
        assert perm.allows(action) is False


def test_a_project_with_no_allowlist_at_all_gets_nothing(live_config) -> None:
    """Absent `outward_actions` means deny, never 'unconstrained'."""
    perm = partition_service.task_outward_permission(
        SimpleNamespace(id="t1", project="unknown-project"),
        outward_consent=True,
        partition_id="p1",
    )
    assert perm.allowed == frozenset()


# ---------------------------------------------------------------------------
# 2. The three choke points, under a scope
# ---------------------------------------------------------------------------


def test_notification_is_suppressed_without_consent(sent) -> None:
    with outward.scope(outward.OutwardPermission.restricted_to(())):
        notification_service.send_notification("run failed", channels=["slack"])
    assert sent == []


def test_notification_is_delivered_with_consent(sent) -> None:
    with outward.scope(outward.OutwardPermission.restricted_to(["notify"])):
        notification_service.send_notification("run failed", channels=["slack"])
    assert sent == ["run failed"]


def test_event_webhook_is_suppressed_without_consent(monkeypatch) -> None:
    from hivepilot.config import settings

    posted: list[str] = []
    monkeypatch.setattr(settings, "event_webhook_url", "https://example.invalid/hook")
    monkeypatch.setattr(
        notification_service.requests,
        "post",
        lambda *a, **k: posted.append("posted"),
    )
    with outward.scope(outward.OutwardPermission.restricted_to(())):
        notification_service.emit_event("pipeline.end", run_id=1)
    assert posted == []

    with outward.scope(outward.OutwardPermission.restricted_to(["notify"])):
        notification_service.emit_event("pipeline.end", run_id=1)
    assert posted == ["posted"]


def test_agent_stream_is_suppressed_without_consent(monkeypatch) -> None:
    streamed: list[str] = []
    monkeypatch.setattr(
        notification_service,
        "_stream_agent_turn_telegram",
        lambda **kwargs: streamed.append(kwargs["actor"]),
    )
    with outward.scope(outward.OutwardPermission.restricted_to(())):
        notification_service.stream_agent_turn(actor="developer", summary="hi")
    assert streamed == []

    with outward.scope(outward.OutwardPermission.restricted_to(["notify"])):
        notification_service.stream_agent_turn(actor="developer", summary="hi")
    assert streamed == ["developer"]


def test_approval_keyboard_is_suppressed_without_consent(monkeypatch) -> None:
    """An approval request is an outbound message like any other. The run
    still pauses and the approval is still visible in-machine."""
    telegram_bot = pytest.importorskip("hivepilot.services.telegram_bot")

    calls: list[int] = []
    monkeypatch.setattr(
        telegram_bot,
        "notify_approval_required",
        lambda **kwargs: calls.append(kwargs["run_id"]),
    )

    with outward.scope(outward.OutwardPermission.restricted_to(())):
        _real_send_approval_keyboard(7, "acme-api", "implement")
    assert calls == []

    with outward.scope(outward.OutwardPermission.restricted_to(["notify"])):
        _real_send_approval_keyboard(7, "acme-api", "implement")
    assert calls == [7]


def test_vault_write_is_suppressed_without_consent(tmp_path) -> None:
    svc = ObsidianService(tmp_path, dry_run=False)
    with outward.scope(outward.OutwardPermission.restricted_to(())):
        result = svc.write_note(
            subpath="Docs/note.md", title="Note", body="body", frontmatter_fields={}
        )
    assert result["suppressed"] == "outward_consent"
    assert list(tmp_path.rglob("note.md")) == []


def test_vault_write_happens_with_consent(tmp_path) -> None:
    svc = ObsidianService(tmp_path, dry_run=False)
    with outward.scope(outward.OutwardPermission.restricted_to(["vault_write"])):
        result = svc.write_note(
            subpath="Docs/note.md", title="Note", body="body", frontmatter_fields={}
        )
    assert "suppressed" not in result
    assert list(tmp_path.rglob("note.md")) != []


def test_vault_artifact_write_is_suppressed_without_consent(tmp_path) -> None:
    """`_emit` is the single choke point -- write_artifact goes through it too."""
    svc = ObsidianService(tmp_path, dry_run=False)
    with outward.scope(outward.OutwardPermission.restricted_to(())):
        svc.write_artifact(
            role="developer", slug="plan", title="Plan", body="body", frontmatter_fields={}
        )
    assert list(tmp_path.rglob("*.md")) == []


def test_network_capable_plugin_runner_is_blocked_without_consent(monkeypatch) -> None:
    from hivepilot import plugin_capabilities, registry
    from hivepilot.runners.shell_runner import ShellRunner

    monkeypatch.setitem(registry.RUNNER_MAP, "acme-cloud", ShellRunner)
    monkeypatch.setattr(
        plugin_capabilities, "_NETWORK_CAPABLE_RUNNER_KINDS", frozenset({"acme-cloud"})
    )

    with outward.scope(outward.OutwardPermission.restricted_to(())):
        with pytest.raises(outward.OutwardActionDenied) as excinfo:
            registry.resolve_runner_class("acme-cloud")
    assert "external_api" in str(excinfo.value)

    with outward.scope(outward.OutwardPermission.restricted_to(["external_api"])):
        assert registry.resolve_runner_class("acme-cloud") is ShellRunner


def test_a_non_network_runner_is_never_touched_by_the_gate(monkeypatch) -> None:
    from hivepilot import plugin_capabilities, registry
    from hivepilot.runners.shell_runner import ShellRunner

    monkeypatch.setitem(registry.RUNNER_MAP, "plain-runner", ShellRunner)
    monkeypatch.setattr(
        plugin_capabilities, "_NETWORK_CAPABLE_RUNNER_KINDS", frozenset({"acme-cloud"})
    )
    with outward.scope(outward.OutwardPermission.restricted_to(())):
        assert registry.resolve_runner_class("plain-runner") is ShellRunner


def test_network_capable_plugin_hook_is_skipped_without_consent() -> None:
    from hivepilot.plugins import PluginManager

    fired: list[str] = []

    def _network_hook(**kwargs: Any) -> None:
        fired.append("network")

    def _plain_hook(**kwargs: Any) -> None:
        fired.append("plain")

    manager = object.__new__(PluginManager)
    manager.hooks = {"after_step": [_network_hook, _plain_hook]}
    manager._network_hooks = (_network_hook,)

    with outward.scope(outward.OutwardPermission.restricted_to(())):
        manager.run_hook("after_step")
    assert fired == ["plain"]

    fired.clear()
    with outward.scope(outward.OutwardPermission.restricted_to(["external_api"])):
        manager.run_hook("after_step")
    assert fired == ["network", "plain"]


# ---------------------------------------------------------------------------
# 3. Every suppression is logged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "call"),
    [
        ("notify", lambda: notification_service.send_notification("x", channels=["slack"])),
        (
            "vault_write",
            lambda: ObsidianService(Path("/tmp"), dry_run=False)._emit(Path("/tmp/x"), "c"),
        ),
    ],
)
def test_each_suppression_is_logged_and_ledgered(action, call, caplog) -> None:
    mark = outward.ledger_mark()
    with caplog.at_level("WARNING"):
        with outward.scope(outward.OutwardPermission.restricted_to((), label="partition p9")):
            call()
    assert "outward.suppressed" in caplog.text
    events = outward.suppressions_since(mark)
    assert [event.action for event in events] == [action]
    assert events[0].scope_label == "partition p9"


# ---------------------------------------------------------------------------
# 4. The orchestrator wiring: the scope really is installed by run_pipeline,
#    and really does survive the ThreadPoolExecutor hop.
# ---------------------------------------------------------------------------


def _bare_orchestrator() -> Orchestrator:
    """An `Orchestrator` with only the state `run_pipeline`'s own frame
    touches, so the scope wiring can be tested without booting the engine."""
    orch = object.__new__(Orchestrator)
    orch._run_scope_lock = threading.Lock()
    orch._run_scope_depth = 0
    orch._verdict_lock = threading.Lock()
    orch._governing_verdict = None
    return orch


def test_run_pipeline_installs_the_permission_for_the_whole_body(monkeypatch) -> None:
    seen: list[bool] = []
    orch = _bare_orchestrator()

    def _record_body(self: Any, **kwargs: Any) -> list[Any]:
        seen.append(outward.allows("notify"))
        return []

    monkeypatch.setattr(Orchestrator, "_run_pipeline_body", _record_body)
    orch.run_pipeline(
        project_names=["acme-api"],
        pipeline_name="bugfix",
        extra_prompt=None,
        auto_git=False,
        outward=outward.OutwardPermission.restricted_to(()),
    )
    assert seen == [False]


def test_run_pipeline_without_outward_is_unrestricted(monkeypatch) -> None:
    """The byte-identical guarantee: an ordinary `hivepilot run`, a scheduled
    run and an API run all reach here with no `outward=`, so no scope is ever
    opened and every gate short-circuits to 'permitted'."""
    seen: list[bool] = []
    orch = _bare_orchestrator()

    def _record_body(self: Any, **kwargs: Any) -> list[Any]:
        seen.append(outward.allows("notify"))
        return []

    monkeypatch.setattr(Orchestrator, "_run_pipeline_body", _record_body)
    orch.run_pipeline(
        project_names=["acme-api"],
        pipeline_name="bugfix",
        extra_prompt=None,
        auto_git=False,
    )
    assert seen == [True]
    assert outward.current().restricted is False


def test_a_nested_run_pipeline_cannot_escape_the_restriction(monkeypatch) -> None:
    seen: list[bool] = []
    orch = _bare_orchestrator()

    def _record_body(self: Any, **kwargs: Any) -> list[Any]:
        seen.append(outward.allows("notify"))
        return []

    monkeypatch.setattr(Orchestrator, "_run_pipeline_body", _record_body)
    with outward.scope(outward.OutwardPermission.restricted_to(())):
        orch.run_pipeline(
            project_names=["acme-api"],
            pipeline_name="bugfix",
            extra_prompt=None,
            auto_git=False,
            outward=outward.OutwardPermission.unrestricted(),
        )
    assert seen == [False]


def test_the_permission_survives_the_task_thread_pool_hop() -> None:
    """`_run_task_body` submits `_execute_task` to a ThreadPoolExecutor, and
    contextvars do NOT cross that boundary. Reproduces the exact
    capture-then-adopt wrapper the orchestrator installs; without it the
    worker would read the ambient default ('permitted') and every gate below
    would fail open."""
    import concurrent.futures

    with outward.scope(outward.OutwardPermission.restricted_to(())):
        captured = outward.capture()

        def _traced() -> bool:
            with outward.adopt(captured):
                return outward.allows("notify")

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(_traced).result() is False


# ---------------------------------------------------------------------------
# 5. End-to-end through the real dispatcher work function
# ---------------------------------------------------------------------------


class _ScopedOrchestrator:
    """Stands in for the engine: replays what `Orchestrator.run_pipeline`
    does with the `outward=` it is handed (installs it as the ambient scope),
    then performs the three outward side effects a real pipeline would.

    The scope INSTALLATION itself is separately pinned against the real
    `Orchestrator.run_pipeline` above; this fake exists to exercise the
    dispatcher -> permission -> choke-point path end to end.
    """

    def __init__(self, vault: Path) -> None:
        self.vault = vault
        self.kwargs: dict[str, Any] = {}

    def run_pipeline(self, **kwargs: Any) -> list[Any]:
        self.kwargs = kwargs
        with outward.scope(kwargs.get("outward")):
            notification_service.send_notification("stage done", channels=["slack"])
            ObsidianService(self.vault, dry_run=False).write_note(
                subpath="Docs/run.md", title="Run", body="body", frontmatter_fields={}
            )
            from hivepilot import plugin_capabilities, registry

            if "acme-cloud" in plugin_capabilities.network_capable_runner_kinds():
                try:
                    registry.resolve_runner_class("acme-cloud")
                    self.kwargs["plugin_reached"] = True
                except outward.OutwardActionDenied:
                    self.kwargs["plugin_reached"] = False
        return [SimpleNamespace(success=True, detail="ok")]


@pytest.fixture
def network_plugin(monkeypatch):
    from hivepilot import plugin_capabilities, registry
    from hivepilot.runners.shell_runner import ShellRunner

    monkeypatch.setitem(registry.RUNNER_MAP, "acme-cloud", ShellRunner)
    monkeypatch.setattr(
        plugin_capabilities, "_NETWORK_CAPABLE_RUNNER_KINDS", frozenset({"acme-cloud"})
    )


def _dispatch_one(*, project: str, consent: bool, orchestrator: Any, monkeypatch, tmp_path) -> None:
    """Run one partition task's work callable synchronously."""
    monkeypatch.setattr(partition_service, "_run_cost_usd", lambda run_id: 0.0)
    monkeypatch.setattr(partition_service, "mark_task_committed", lambda *a, **k: True)
    monkeypatch.setattr(partition_service, "mark_task_failed", lambda *a, **k: True)
    monkeypatch.setattr(partition_service, "_mark_queue_row", lambda *a, **k: None)
    monkeypatch.setattr(partition_service, "_capture_pr_url", lambda mark, name: None)
    monkeypatch.setattr(partition_service, "_audit_suppressions", lambda *a, **k: None)

    task = json.loads(json.dumps(_task(project=project)))
    work = partition_service._make_task_work(
        "p-e2e",
        task=SimpleNamespace(**task),
        owner="dispatcher",
        run_id=1,
        queue_id=1,
        project_name=project,
        outward_consent=consent,
        orchestrator=orchestrator,
        done=threading.Event(),
    )
    work()


def test_partition_run_without_consent_does_none_of_the_three(
    live_config, sent, network_plugin, monkeypatch, tmp_path
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    orch = _ScopedOrchestrator(vault)
    _dispatch_one(
        project="acme-open",
        consent=False,
        orchestrator=orch,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )
    assert sent == []
    assert list(vault.rglob("*.md")) == []
    assert orch.kwargs["plugin_reached"] is False
    # git/forge keeps its pre-existing suppressor, unchanged.
    assert orch.kwargs["auto_git"] is False


def test_partition_run_with_consent_does_all_three(
    live_config, sent, network_plugin, monkeypatch, tmp_path
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    orch = _ScopedOrchestrator(vault)
    _dispatch_one(
        project="acme-open",
        consent=True,
        orchestrator=orch,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )
    assert sent == ["stage done"]
    assert list(vault.rglob("run.md")) != []
    assert orch.kwargs["plugin_reached"] is True
    assert orch.kwargs["auto_git"] is True


def test_dispatcher_hands_run_pipeline_an_explicit_permission(
    live_config, sent, monkeypatch, tmp_path
) -> None:
    """The carrier travels as an explicit keyword, exactly like `auto_git` --
    not as ambient state the dispatcher hopes someone set."""
    vault = tmp_path / "vault"
    vault.mkdir()
    orch = _ScopedOrchestrator(vault)
    _dispatch_one(
        project="acme-api",
        consent=True,
        orchestrator=orch,
        monkeypatch=monkeypatch,
        tmp_path=tmp_path,
    )
    permission = orch.kwargs["outward"]
    assert permission.restricted is True
    assert permission.allowed == frozenset({"git_push", "forge_pr"})
    assert permission.label == "partition p-e2e task t1"
