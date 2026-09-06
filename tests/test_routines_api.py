"""HP-56 FastAPI routines: read list, run writes, webhook fire."""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from hivepilot.services.token_service import add_token


@pytest.fixture()
def tmp_tokens_file(tmp_path, monkeypatch):
    import yaml

    from hivepilot.config import settings

    tokens_file = tmp_path / "tokens.yaml"
    tokens_file.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    monkeypatch.setattr(settings, "tokens_file", tokens_file)
    return tokens_file


@pytest.fixture()
def api_client():
    from hivepilot.services.api_service import app

    return TestClient(app, raise_server_exceptions=True)


def _auth(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}


def _payload(**overrides):
    body = {
        "role": "developer",
        "projects": ["example-api"],
        "crons": ["0 9 * * 1"],
        "timezone": "UTC",
        "replace_key": "weekly-dev",
    }
    body.update(overrides)
    return body


def test_list_routines_requires_auth(api_client):
    assert api_client.get("/v1/routines").status_code == 401


def test_create_list_patch_delete_and_replace_key(api_client, tmp_tokens_file):
    read_raw, _ = add_token("read")
    run_raw, _ = add_token("run")

    empty = api_client.get("/v1/routines", headers=_auth(read_raw))
    assert empty.status_code == 200
    assert empty.json() == {"routines": []}

    created = api_client.post("/v1/routines", headers=_auth(run_raw), json=_payload())
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["role"] == "developer"
    assert body["replace_key"] == "weekly-dev"
    assert body["next_run_at"]
    rid = body["id"]

    again = api_client.post(
        "/v1/routines", headers=_auth(run_raw), json=_payload(crons=["0 10 * * 1"])
    )
    assert again.status_code == 200
    assert again.json()["id"] == rid
    assert again.json()["crons"] == ["0 10 * * 1"]

    listed = api_client.get("/v1/routines", headers=_auth(read_raw))
    assert len(listed.json()["routines"]) == 1

    patched = api_client.patch(
        f"/v1/routines/{rid}",
        headers=_auth(run_raw),
        json={"enabled": False},
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False

    got = api_client.get("/v1/routines/weekly-dev", headers=_auth(read_raw))
    assert got.status_code == 200
    assert got.json()["id"] == rid

    deleted = api_client.delete(f"/v1/routines/{rid}", headers=_auth(run_raw))
    assert deleted.status_code == 200
    assert api_client.get(f"/v1/routines/{rid}", headers=_auth(read_raw)).status_code == 404


def test_create_rejects_bad_cron_and_unknown_role(api_client, tmp_tokens_file):
    run_raw, _ = add_token("run")
    bad_cron = api_client.post(
        "/v1/routines",
        headers=_auth(run_raw),
        json=_payload(crons=["nope"], replace_key=None),
    )
    assert bad_cron.status_code == 400
    unknown = api_client.post(
        "/v1/routines",
        headers=_auth(run_raw),
        json=_payload(role="no-such-role", replace_key="x"),
    )
    assert unknown.status_code == 400


def test_read_cannot_write(api_client, tmp_tokens_file):
    read_raw, _ = add_token("read")
    resp = api_client.post("/v1/routines", headers=_auth(read_raw), json=_payload())
    assert resp.status_code == 403


def test_webhook_fires_async(api_client, tmp_tokens_file, monkeypatch):
    run_raw, _ = add_token("run")
    created = api_client.post(
        "/v1/routines", headers=_auth(run_raw), json=_payload(replace_key="hook")
    )
    rid = created.json()["id"]
    calls: list[str] = []

    def _run(routine, orch, **kwargs):
        calls.append(routine.id)
        return True

    monkeypatch.setattr("hivepilot.services.routine_service.run_routine", _run)

    class _Immediate:
        def __init__(self, target, daemon=False):  # noqa: FBT002
            self._target = target

        def start(self) -> None:
            self._target()

    monkeypatch.setattr(threading, "Thread", _Immediate)
    fired = api_client.post(f"/v1/webhook/routines/{rid}", headers=_auth(run_raw))
    assert fired.status_code == 200
    assert fired.json()["status"] == "triggered"
    assert calls == [rid]
