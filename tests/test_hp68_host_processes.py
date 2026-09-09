"""HP-68 slice 2 — this-host allowlisted processes + loopback browser tabs.

Honesty: no full ``ps``, no cmdline, no invented tabs, no remote CDP.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError

import pytest
import yaml
from fastapi.testclient import TestClient

from hivepilot.services import host_browser, host_processes
from hivepilot.services.token_service import add_token


def _write_proc(root: Path, pid: int, comm: str, rss_kb: int | None = None) -> None:
    entry = root / str(pid)
    entry.mkdir()
    (entry / "comm").write_text(f"{comm}\n", encoding="utf-8")
    if rss_kb is not None:
        (entry / "status").write_text(f"Name:\t{comm}\nVmRSS:\t{rss_kb} kB\n", encoding="utf-8")


def test_snapshot_keep_allowlist_drop_others(tmp_path: Path) -> None:
    _write_proc(tmp_path, 11, "hivepilot", rss_kb=2048)
    _write_proc(tmp_path, 12, "ocx", rss_kb=512)
    _write_proc(tmp_path, 13, "google-chrome", rss_kb=4096)
    _write_proc(tmp_path, 14, "cursor-agent", rss_kb=1024)
    _write_proc(tmp_path, 99, "bash", rss_kb=128)
    _write_proc(tmp_path, 100, "python3", rss_kb=256)
    (tmp_path / "cpuinfo").write_text("not a pid", encoding="utf-8")

    snap = host_processes.snapshot(proc_root=tmp_path, hostname="box")
    names = {row["name"] for row in snap["processes"]}
    assert names == {"hivepilot", "ocx", "google-chrome", "cursor-agent"}
    assert snap["host"] == "box"
    assert "cmdline" not in snap
    assert all("cmdline" not in row for row in snap["processes"])
    assert snap["processes"][0]["name"] == "google-chrome"
    assert snap["processes"][0]["rss_bytes"] == 4096 * 1024


def test_snapshot_never_reads_cmdline(tmp_path: Path) -> None:
    _write_proc(tmp_path, 42, "ollama", rss_kb=100)
    (tmp_path / "42" / "cmdline").write_text("ollama\x00serve\x00--token=secret", encoding="utf-8")
    seen: list[str] = []

    def _read(path: Path) -> str | None:
        seen.append(path.name)
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return None

    snap = host_processes.snapshot(proc_root=tmp_path, hostname="box", read_text=_read)
    assert "cmdline" not in seen
    assert snap["processes"] == [{"pid": 42, "name": "ollama", "rss_bytes": 100 * 1024}]


def test_snapshot_caps_at_24(tmp_path: Path) -> None:
    for i in range(30):
        _write_proc(tmp_path, 1000 + i, "hivepilot", rss_kb=10 + i)
    snap = host_processes.snapshot(proc_root=tmp_path, hostname="box")
    assert len(snap["processes"]) == 24
    assert snap["processes"][0]["pid"] == 1029


def test_snapshot_missing_proc_is_empty_not_invented(tmp_path: Path) -> None:
    missing = tmp_path / "no-proc"
    snap = host_processes.snapshot(proc_root=missing, hostname="box")
    assert snap["processes"] == []
    assert "invented" in snap["note"]


def test_browser_refuses_non_loopback() -> None:
    snap = host_browser.snapshot(base_url="http://10.0.0.8:9222")
    assert snap["attached"] is False
    assert snap["tabs"] == []
    assert "refused" in (snap["error"] or "")
    assert "loopback" in snap["note"]


class _CtxResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_CtxResp":
        return self

    def __exit__(self, *exc) -> None:  # noqa: ANN002
        return None


def test_browser_lists_pages_drops_websocket(monkeypatch) -> None:
    payload = json.dumps(
        [
            {
                "id": "tab-1",
                "title": "Docs",
                "url": "https://example.com/docs",
                "type": "page",
                "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/tab-1",
            },
            {
                "id": "sw-1",
                "title": "worker",
                "url": "chrome://serviceworker",
                "type": "service_worker",
            },
        ]
    ).encode()
    monkeypatch.setattr(host_browser.urllib.request, "urlopen", lambda *_a, **_k: _CtxResp(payload))
    snap = host_browser.snapshot(base_url="http://127.0.0.1:9222")
    assert snap["attached"] is True
    assert snap["error"] is None
    assert snap["tabs"] == [
        {"id": "tab-1", "title": "Docs", "url": "https://example.com/docs", "type": "page"}
    ]
    assert "webSocketDebuggerUrl" not in snap["tabs"][0]


def test_browser_unreachable_is_empty_not_invented(monkeypatch) -> None:
    def _boom(*_a, **_k):  # noqa: ANN002
        raise OSError("connection refused")

    monkeypatch.setattr(host_browser.urllib.request, "urlopen", _boom)
    snap = host_browser.snapshot(base_url="http://127.0.0.1:9222")
    assert snap["attached"] is False
    assert snap["tabs"] == []
    assert "connection refused" in (snap["error"] or "")
    assert "invent" not in snap["note"].lower()


def test_browser_http_error_is_empty(monkeypatch) -> None:
    def _http(*_a, **_k):  # noqa: ANN002
        raise HTTPError("http://127.0.0.1:9222/json/list", 404, "nope", hdrs=None, fp=None)

    monkeypatch.setattr(host_browser.urllib.request, "urlopen", _http)
    snap = host_browser.snapshot(base_url="http://127.0.0.1:9222")
    assert snap["attached"] is False
    assert snap["tabs"] == []
    assert snap["error"] == "HTTP 404"


def test_browser_env_overrides_default(monkeypatch) -> None:
    monkeypatch.setenv("HIVEPILOT_BROWSER_CDP_URL", "http://127.0.0.1:9333")
    assert host_browser.default_cdp_url() == "http://127.0.0.1:9333"


@pytest.fixture()
def tmp_tokens_file(tmp_path, monkeypatch):
    tokens_file = tmp_path / "tokens.yaml"
    tokens_file.write_text(yaml.safe_dump({"tokens": []}), encoding="utf-8")
    from hivepilot.config import settings

    monkeypatch.setattr(settings, "tokens_file", tokens_file)
    return tokens_file


@pytest.fixture()
def api_client():
    from hivepilot.services.api_service import app

    return TestClient(app, raise_server_exceptions=True)


def _auth(raw_token: str) -> dict:
    return {"Authorization": f"Bearer {raw_token}"}


def test_host_processes_endpoint_read_role(tmp_tokens_file, api_client, monkeypatch) -> None:
    raw, _ = add_token("read")
    monkeypatch.setattr(
        host_processes,
        "snapshot",
        lambda: {
            "host": "box",
            "processes": [{"pid": 7, "name": "hivepilot", "rss_bytes": 1024}],
            "note": "this host",
        },
    )
    resp = api_client.get("/v1/host/processes", headers=_auth(raw))
    assert resp.status_code == 200
    data = resp.json()
    assert data["host"] == "box"
    assert data["processes"][0]["name"] == "hivepilot"
    assert "cmdline" not in data["processes"][0]


def test_host_browser_endpoint_read_role(tmp_tokens_file, api_client, monkeypatch) -> None:
    raw, _ = add_token("read")
    monkeypatch.setattr(
        host_browser,
        "snapshot",
        lambda: {
            "attached": False,
            "base_url": "http://127.0.0.1:9222",
            "tabs": [],
            "note": "no browser",
            "error": "connection refused",
        },
    )
    resp = api_client.get("/v1/host/browser", headers=_auth(raw))
    assert resp.status_code == 200
    data = resp.json()
    assert data["attached"] is False
    assert data["tabs"] == []
    assert "webSocketDebuggerUrl" not in data


def test_host_processes_endpoint_rejects_anonymous(api_client) -> None:
    resp = api_client.get("/v1/host/processes")
    assert resp.status_code in {401, 403}


def test_host_browser_endpoint_rejects_anonymous(api_client) -> None:
    resp = api_client.get("/v1/host/browser")
    assert resp.status_code in {401, 403}
