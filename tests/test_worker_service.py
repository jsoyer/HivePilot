"""HivePilot worker HTTP service — POST /run-step executes a step locally (W1)."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from hivepilot.config import settings
from hivepilot.services import worker_service


def test_run_step_requires_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, "worker_token", "secret", raising=False)
    client = TestClient(worker_service.create_app())
    r = client.post("/run-step", json={})
    assert r.status_code == 401


def test_run_step_fails_closed_when_token_unset(monkeypatch) -> None:
    # No token configured → refuse to serve (never fall open to unauthenticated RCE).
    monkeypatch.setattr(settings, "worker_token", None, raising=False)
    client = TestClient(worker_service.create_app())
    r = client.post("/run-step", json={}, headers={"Authorization": "Bearer anything"})
    assert r.status_code == 503


def test_run_step_rejects_wrong_token(monkeypatch) -> None:
    monkeypatch.setattr(settings, "worker_token", "secret", raising=False)
    client = TestClient(worker_service.create_app())
    r = client.post("/run-step", json={}, headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_run_step_executes_and_returns_output(monkeypatch) -> None:
    monkeypatch.setattr(settings, "worker_token", "secret", raising=False)
    monkeypatch.setattr(worker_service, "execute_step", lambda body: "REMOTE OUT")
    client = TestClient(worker_service.create_app())
    r = client.post(
        "/run-step",
        headers={"Authorization": "Bearer secret"},
        json={
            "kind": "claude",
            "project_path": "/tmp",
            "project_name": "p",
            "task_name": "t",
            "step_name": "s",
        },
    )
    assert r.status_code == 200
    assert r.json()["output"] == "REMOTE OUT"


def test_execute_step_builds_definition_and_captures(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}

    class FakeRegistry:
        def __init__(self, defs) -> None:
            pass

        def capture_definition(self, rdef, payload):
            captured["kind"] = rdef.kind
            captured["model"] = rdef.model
            captured["prior"] = payload.metadata.get("prior_context")
            return "OUT"

    monkeypatch.setattr(worker_service, "RunnerRegistry", FakeRegistry)
    out = worker_service.execute_step(
        {
            "kind": "claude",
            "model": "m",
            "project_path": str(tmp_path),
            "project_name": "p",
            "task_name": "t",
            "step_name": "s",
            "prompt_file": "p.md",
            "metadata": {"prior_context": "ctx"},
        }
    )
    assert out == "OUT"
    assert captured == {"kind": "claude", "model": "m", "prior": "ctx"}
