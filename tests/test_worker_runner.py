"""RemoteWorkerRunner — hub-side runner that forwards a step to a remote worker."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

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
    resp = MagicMock(status_code=200)
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
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"output": "x"}
    resp.raise_for_status.return_value = None
    with patch("hivepilot.runners.worker_runner.requests.post", return_value=resp) as m:
        _runner(tmp_path).capture(_payload(tmp_path))
    assert "Authorization" not in m.call_args.kwargs.get("headers", {})


def _ok_response(output: str = "done") -> MagicMock:
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"output": output}
    resp.raise_for_status.return_value = None
    return resp


def test_retries_on_connection_error_then_succeeds(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "worker_retries", 2, raising=False)
    monkeypatch.setattr("hivepilot.runners.worker_runner.time.sleep", lambda s: None)
    seq = [requests.ConnectionError("x"), _ok_response("done")]

    def side_effect(*a, **k):
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    with patch("hivepilot.runners.worker_runner.requests.post", side_effect=side_effect):
        assert _runner(tmp_path).capture(_payload(tmp_path)) == "done"


def test_retries_on_5xx_then_succeeds(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "worker_retries", 1, raising=False)
    monkeypatch.setattr("hivepilot.runners.worker_runner.time.sleep", lambda s: None)
    with patch(
        "hivepilot.runners.worker_runner.requests.post",
        side_effect=[MagicMock(status_code=503), _ok_response("ok")],
    ):
        assert _runner(tmp_path).capture(_payload(tmp_path)) == "ok"


def test_4xx_is_not_retried(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "worker_retries", 3, raising=False)
    bad = MagicMock(status_code=401)
    bad.raise_for_status.side_effect = requests.HTTPError("401")
    with patch("hivepilot.runners.worker_runner.requests.post", return_value=bad) as m:
        with pytest.raises(requests.HTTPError):
            _runner(tmp_path).capture(_payload(tmp_path))
    assert m.call_count == 1  # auth failure is not transient → no retry


def test_exhausted_retries_raise_runtimeerror(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "worker_retries", 1, raising=False)
    monkeypatch.setattr("hivepilot.runners.worker_runner.time.sleep", lambda s: None)
    with patch(
        "hivepilot.runners.worker_runner.requests.post",
        side_effect=requests.ConnectionError("down"),
    ):
        with pytest.raises(RuntimeError, match="failed after"):
            _runner(tmp_path).capture(_payload(tmp_path))


def test_semaphore_is_per_host() -> None:
    from hivepilot.runners.worker_runner import _semaphore_for

    a = _semaphore_for("https://h1", 4)
    a2 = _semaphore_for("https://h1", 4)
    b = _semaphore_for("https://h2", 4)
    assert a is a2
    assert a is not b


def test_capture_refuses_plaintext_http_to_remote(tmp_path: Path) -> None:
    rdef = RunnerDefinition(name="r", kind="claude", host="http://hostC:8900")
    with pytest.raises(ValueError, match="plaintext http"):
        RemoteWorkerRunner(rdef, settings).capture(_payload(tmp_path))


def test_capture_allows_loopback_http(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "worker_token", None, raising=False)
    rdef = RunnerDefinition(name="r", kind="claude", host="http://127.0.0.1:8900")
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"output": "ok"}
    resp.raise_for_status.return_value = None
    with patch("hivepilot.runners.worker_runner.requests.post", return_value=resp) as m:
        out = RemoteWorkerRunner(rdef, settings).capture(_payload(tmp_path))
    assert out == "ok"
    url = m.call_args.args[0] if m.call_args.args else m.call_args.kwargs["url"]
    assert url.startswith("http://127.0.0.1:8900")
