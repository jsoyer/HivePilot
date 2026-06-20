"""RemoteWorkerRunner — hub-side runner that forwards a step to a remote worker."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from hivepilot.runners.worker_runner import RemoteWorkerRunner

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload


def _payload(tmp_path: Path) -> RunnerPayload:
    return RunnerPayload(
        project_name="noxys-api",
        project=ProjectConfig(path=tmp_path),
        task_name="t",
        step=TaskStep(name="s", runner="claude", prompt_file="p.md"),
        metadata={"extra_prompt": "go", "prior_context": "ctx"},
        secrets={},
    )


def _runner(tmp_path: Path) -> RemoteWorkerRunner:
    rdef = RunnerDefinition(
        name="role:developer",
        kind="claude",
        model="claude-sonnet-4-6",
        host="http://hostC:8900",
    )
    return RemoteWorkerRunner(rdef, settings)


def test_capture_posts_step_to_worker_and_returns_output(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "worker_token", "secret", raising=False)
    resp = MagicMock()
    resp.json.return_value = {"output": "AGENT OUTPUT"}
    resp.raise_for_status.return_value = None
    with patch("hivepilot.runners.worker_runner.requests.post", return_value=resp) as m:
        out = _runner(tmp_path).capture(_payload(tmp_path))

    assert out == "AGENT OUTPUT"
    url = m.call_args.args[0] if m.call_args.args else m.call_args.kwargs["url"]
    assert url == "http://hostC:8900/run-step"
    body = m.call_args.kwargs["json"]
    assert body["kind"] == "claude"
    assert body["model"] == "claude-sonnet-4-6"
    assert body["project_name"] == "noxys-api"
    assert body["metadata"]["prior_context"] == "ctx"
    # bearer token forwarded
    assert m.call_args.kwargs["headers"]["Authorization"] == "Bearer secret"


def test_capture_without_token_sends_no_auth_header(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "worker_token", None, raising=False)
    resp = MagicMock()
    resp.json.return_value = {"output": "x"}
    resp.raise_for_status.return_value = None
    with patch("hivepilot.runners.worker_runner.requests.post", return_value=resp) as m:
        _runner(tmp_path).capture(_payload(tmp_path))
    assert "Authorization" not in m.call_args.kwargs.get("headers", {})
