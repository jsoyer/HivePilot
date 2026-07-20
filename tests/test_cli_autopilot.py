"""Tests for `hivepilot autopilot enqueue|queue|list|promote|veto|pause|resume|
stop|status` (guarded objective queue CLI).

`hivepilot.services.autopilot_queue` is mocked throughout -- no real state DB
is ever touched. Covers:

1. `enqueue`: happy path calls `autopilot_queue.enqueue(project, pipeline,
   reason, tenant=...)` with the literal plain-string args in the right
   order, and the CLI's stdout never contains anything beyond the row id +
   the plain strings the user passed in (no dict/object repr, no
   `RunResult`/`detail`-shaped payload).
2. `queue`/`list`: empty queue prints the empty message; non-empty renders a
   row per `QueueItem`.
3. `promote`/`veto`: call the matching service function with the given id.
4. `pause`/`resume`/`stop`: call the matching service function with the
   given tenant.
5. `status`: calls `is_paused`, `is_stopped`, and `list_queue`, all with the
   given tenant, and prints a per-state count summary.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Stub out optional heavy dependencies before importing hivepilot.cli
# (mirrors tests/test_cli_drift.py so this file can run standalone).
# ---------------------------------------------------------------------------

_STUBS = [
    "langchain",
    "langchain.text_splitter",
    "langchain_community",
    "langchain_community.embeddings",
    "langchain_community.vectorstores",
    "langchain_openai",
    "openai",
    "boto3",
    "docker",
    "telegram",
    "telegram.ext",
    "fastapi",
    "fastapi.responses",
    "fastapi.security",
    "uvicorn",
    "textual",
    "slack_bolt",
    "slack_bolt.adapter",
    "slack_bolt.adapter.fastapi",
    "slack_bolt.adapter.socket_mode",
    "discord",
    "PyNaCl",
    "nacl",
    "nacl.exceptions",
    "nacl.signing",
]

import importlib  # noqa: E402

for _mod in _STUBS:
    if _mod in sys.modules:
        continue
    try:
        importlib.import_module(_mod)
    except Exception:
        sys.modules[_mod] = MagicMock()

from typer.testing import CliRunner  # noqa: E402

from hivepilot.cli import app  # noqa: E402
from hivepilot.services.autopilot_queue import QueueItem  # noqa: E402

_LEAKED_LOOKING_DETAIL = "RunResult(detail='sk-live-should-never-leak-0123456789')"  # noqa: S105


def _item(
    item_id: int = 1,
    *,
    tenant: str = "default",
    project: str = "acme-api",
    pipeline: str = "groomer",
    reason: str | None = "found stale docs",
    state: str = "proposed",
    cost_usd: float | None = None,
) -> QueueItem:
    return QueueItem(
        id=item_id,
        tenant=tenant,
        project=project,
        pipeline=pipeline,
        reason=reason,
        state=state,
        cost_usd=cost_usd,
        created_ts="2026-07-20T00:00:00",
        updated_ts="2026-07-20T00:00:00",
    )


class TestEnqueue:
    def test_happy_path_calls_service_with_plain_strings_in_right_order(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_enqueue = MagicMock(return_value=7)
        monkeypatch.setattr("hivepilot.services.autopilot_queue.enqueue", mock_enqueue)
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "autopilot",
                "enqueue",
                "groomer",
                "acme-api",
                "--reason",
                "found stale docs",
                "--tenant",
                "default",
            ],
        )
        assert result.exit_code == 0, result.output
        mock_enqueue.assert_called_once_with(
            "acme-api", "groomer", "found stale docs", tenant="default"
        )
        assert "7" in result.output
        assert "groomer" in result.output
        assert "acme-api" in result.output
        assert "RunResult" not in result.output
        assert "detail" not in result.output

    def test_defaults_reason_empty_and_tenant_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_enqueue = MagicMock(return_value=1)
        monkeypatch.setattr("hivepilot.services.autopilot_queue.enqueue", mock_enqueue)
        runner = CliRunner()
        result = runner.invoke(app, ["autopilot", "enqueue", "groomer", "acme-api"])
        assert result.exit_code == 0, result.output
        mock_enqueue.assert_called_once_with("acme-api", "groomer", "", tenant="default")

    def test_output_never_leaks_a_run_result_shaped_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_enqueue = MagicMock(return_value=3)
        monkeypatch.setattr("hivepilot.services.autopilot_queue.enqueue", mock_enqueue)
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["autopilot", "enqueue", "groomer", "acme-api", "--reason", _LEAKED_LOOKING_DETAIL],
        )
        assert result.exit_code == 0, result.output
        # The `reason` value is forwarded to the service as a plain string
        # (asserted below) but the CLI's own confirmation echo only ever
        # contains the row id + pipeline + project -- it never echoes
        # `reason` at all, so a RunResult/detail-shaped string smuggled in
        # via `--reason` never reaches stdout either.
        mock_enqueue.assert_called_once_with(
            "acme-api", "groomer", _LEAKED_LOOKING_DETAIL, tenant="default"
        )
        assert _LEAKED_LOOKING_DETAIL not in result.output
        assert "RunResult" not in result.output
        assert "detail" not in result.output
        assert result.output.strip() == "Enqueued objective #3: groomer -> acme-api (proposed)"


class TestQueueList:
    def test_queue_empty_prints_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "hivepilot.services.autopilot_queue.list_queue", MagicMock(return_value=[])
        )
        runner = CliRunner()
        result = runner.invoke(app, ["autopilot", "queue"])
        assert result.exit_code == 0, result.output
        assert "Autopilot queue is empty." in result.output

    def test_list_alias_empty_prints_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "hivepilot.services.autopilot_queue.list_queue", MagicMock(return_value=[])
        )
        runner = CliRunner()
        result = runner.invoke(app, ["autopilot", "list"])
        assert result.exit_code == 0, result.output
        assert "Autopilot queue is empty." in result.output

    def test_queue_non_empty_renders_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        items = [_item(1, cost_usd=1.5), _item(2, state="queued", cost_usd=None)]
        mock_list = MagicMock(return_value=items)
        monkeypatch.setattr("hivepilot.services.autopilot_queue.list_queue", mock_list)
        runner = CliRunner()
        result = runner.invoke(app, ["autopilot", "queue", "--tenant", "default"])
        assert result.exit_code == 0, result.output
        mock_list.assert_called_once_with(tenant="default", state=None)
        assert "acme-api" in result.output
        assert "groomer" in result.output
        assert "1.50" in result.output
        assert "-" in result.output

    def test_queue_passes_state_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_list = MagicMock(return_value=[])
        monkeypatch.setattr("hivepilot.services.autopilot_queue.list_queue", mock_list)
        runner = CliRunner()
        runner.invoke(app, ["autopilot", "queue", "--state", "queued"])
        mock_list.assert_called_once_with(tenant="default", state="queued")


class TestPromoteVeto:
    def test_promote_calls_service_with_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_promote = MagicMock()
        monkeypatch.setattr("hivepilot.services.autopilot_queue.promote", mock_promote)
        runner = CliRunner()
        result = runner.invoke(app, ["autopilot", "promote", "5"])
        assert result.exit_code == 0, result.output
        mock_promote.assert_called_once_with(5)

    def test_veto_calls_service_with_id(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_veto = MagicMock()
        monkeypatch.setattr("hivepilot.services.autopilot_queue.veto", mock_veto)
        runner = CliRunner()
        result = runner.invoke(app, ["autopilot", "veto", "9"])
        assert result.exit_code == 0, result.output
        mock_veto.assert_called_once_with(9)


class TestControls:
    def test_pause_calls_service_with_tenant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_pause = MagicMock()
        monkeypatch.setattr("hivepilot.services.autopilot_queue.pause", mock_pause)
        runner = CliRunner()
        result = runner.invoke(app, ["autopilot", "pause", "--tenant", "acme"])
        assert result.exit_code == 0, result.output
        mock_pause.assert_called_once_with(tenant="acme")

    def test_resume_calls_service_with_tenant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_resume = MagicMock()
        monkeypatch.setattr("hivepilot.services.autopilot_queue.resume", mock_resume)
        runner = CliRunner()
        result = runner.invoke(app, ["autopilot", "resume", "--tenant", "acme"])
        assert result.exit_code == 0, result.output
        mock_resume.assert_called_once_with(tenant="acme")

    def test_stop_calls_service_with_tenant(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mock_stop = MagicMock()
        monkeypatch.setattr("hivepilot.services.autopilot_queue.stop", mock_stop)
        runner = CliRunner()
        result = runner.invoke(app, ["autopilot", "stop", "--tenant", "acme"])
        assert result.exit_code == 0, result.output
        mock_stop.assert_called_once_with(tenant="acme")


class TestStatus:
    def test_status_calls_all_three_reads_with_tenant_and_prints_summary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_paused = MagicMock(return_value=True)
        mock_stopped = MagicMock(return_value=False)
        mock_list = MagicMock(
            return_value=[
                _item(1, state="proposed"),
                _item(2, state="proposed"),
                _item(3, state="queued"),
            ]
        )
        monkeypatch.setattr("hivepilot.services.autopilot_queue.is_paused", mock_paused)
        monkeypatch.setattr("hivepilot.services.autopilot_queue.is_stopped", mock_stopped)
        monkeypatch.setattr("hivepilot.services.autopilot_queue.list_queue", mock_list)
        runner = CliRunner()
        result = runner.invoke(app, ["autopilot", "status", "--tenant", "acme"])
        assert result.exit_code == 0, result.output
        mock_paused.assert_called_once_with(tenant="acme")
        mock_stopped.assert_called_once_with(tenant="acme")
        mock_list.assert_called_once_with(tenant="acme")
        assert "acme" in result.output
        assert "paused" in result.output.lower()
        assert "stopped" in result.output.lower()
        assert "proposed" in result.output
        assert "2" in result.output

    def test_status_empty_queue_prints_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "hivepilot.services.autopilot_queue.is_paused", MagicMock(return_value=False)
        )
        monkeypatch.setattr(
            "hivepilot.services.autopilot_queue.is_stopped", MagicMock(return_value=False)
        )
        monkeypatch.setattr(
            "hivepilot.services.autopilot_queue.list_queue", MagicMock(return_value=[])
        )
        runner = CliRunner()
        result = runner.invoke(app, ["autopilot", "status"])
        assert result.exit_code == 0, result.output
        assert "empty" in result.output.lower()
