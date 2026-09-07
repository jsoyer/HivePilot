"""HP-82 — OpenCodex is its own proxy, not the Codex runner."""

from __future__ import annotations

import urllib.error

from hivepilot.services import local_models, opencodex_probe


def test_kind_is_never_codex(monkeypatch):
    monkeypatch.setattr(opencodex_probe.shutil, "which", lambda _name: None)
    monkeypatch.setattr(opencodex_probe, "_http_reachable", lambda *_a, **_k: (False, "down"))
    snap = opencodex_probe.snapshot()
    assert snap["kind"] == "opencodex"
    assert snap["binary"] == "ocx"
    assert snap["kind"] != "codex"
    assert snap["binary"] != "codex"


def test_refuses_non_loopback(monkeypatch):
    monkeypatch.setattr(opencodex_probe.shutil, "which", lambda _name: "/usr/bin/ocx")
    snap = opencodex_probe.snapshot(base_url="http://10.0.0.8:10100")
    assert snap["reachable"] is False
    assert "loopback" in (snap["error"] or "")


def test_http_401_counts_as_up(monkeypatch):
    monkeypatch.setattr(opencodex_probe.shutil, "which", lambda _name: "/usr/bin/ocx")

    def _raise(_url, timeout=1.5):  # noqa: ANN001
        raise urllib.error.HTTPError(_url, 401, "auth", hdrs=None, fp=None)

    monkeypatch.setattr(opencodex_probe.urllib.request, "urlopen", _raise)
    snap = opencodex_probe.snapshot(base_url="http://127.0.0.1:10100")
    assert snap["reachable"] is True
    assert snap["binary_present"] is True


def test_machine_snapshot_keeps_codex_cli_and_opencodex_proxy_apart(monkeypatch):
    monkeypatch.setattr(
        "hivepilot.services.agent_auth.auth_state",
        lambda kind: "present" if kind == "codex" else "absent",
    )
    monkeypatch.setattr(local_models, "discover", lambda: [])
    monkeypatch.setattr(
        "hivepilot.services.opencodex_probe.snapshot",
        lambda: {
            "kind": "opencodex",
            "binary": "ocx",
            "binary_present": True,
            "base_url": "http://127.0.0.1:10100",
            "reachable": True,
            "models": ["proxy-model"],
            "error": None,
        },
    )
    snap = local_models.machine_snapshot()
    assert "proxies" in snap
    assert [p["kind"] for p in snap["proxies"]] == ["opencodex"]
    assert any(row["kind"] == "codex" for row in snap["cli"])
    assert all(row["kind"] != "opencodex" for row in snap["cli"])
    assert all(row["kind"] != "opencodex" for row in snap["local"])
    assert all(p["kind"] != "codex" for p in snap["proxies"])
