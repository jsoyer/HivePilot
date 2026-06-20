"""RemoteWorkerRunner — hub-side runner that forwards a step to a remote worker."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.runners.base import RunnerPayload
from hivepilot.runners.worker_runner import RemoteWorkerRunner


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
        host="https://hostC:8900",
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
    assert url == "https://hostC:8900/run-step"
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


def test_capture_refuses_plaintext_http_to_remote(tmp_path: Path) -> None:
    import pytest

    rdef = RunnerDefinition(name="r", kind="claude", host="http://hostC:8900")
    with pytest.raises(ValueError, match="plaintext http"):
        RemoteWorkerRunner(rdef, settings).capture(_payload(tmp_path))


def test_capture_allows_loopback_http(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "worker_token", None, raising=False)
    rdef = RunnerDefinition(name="r", kind="claude", host="http://127.0.0.1:8900")
    resp = MagicMock()
    resp.json.return_value = {"output": "ok"}
    resp.raise_for_status.return_value = None
    with patch("hivepilot.runners.worker_runner.requests.post", return_value=resp) as m:
        out = RemoteWorkerRunner(rdef, settings).capture(_payload(tmp_path))
    assert out == "ok"
    url = m.call_args.args[0] if m.call_args.args else m.call_args.kwargs["url"]
    assert url.startswith("http://127.0.0.1:8900")
