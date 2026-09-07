"""HP-83 — OpenCodex is an OpenAI-compat backend, not a Codex runner."""

from __future__ import annotations

import json
from io import BytesIO
from urllib.error import HTTPError

from hivepilot.services import opencodex_probe


def test_openai_compat_base_appends_v1():
    assert opencodex_probe.openai_compat_base("http://127.0.0.1:10100") == (
        "http://127.0.0.1:10100/v1"
    )
    assert opencodex_probe.openai_compat_base("http://127.0.0.1:10100/v1") == (
        "http://127.0.0.1:10100/v1"
    )


def test_snapshot_lists_models_when_reachable(monkeypatch):
    monkeypatch.setattr(opencodex_probe.shutil, "which", lambda _name: "/usr/bin/ocx")
    monkeypatch.setattr(opencodex_probe, "_http_reachable", lambda *_a, **_k: (True, None))

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):  # noqa: ANN002
            return False

        def read(self):
            return json.dumps({"data": [{"id": "gpt-proxy"}, {"id": "local-x"}]}).encode()

    monkeypatch.setattr(opencodex_probe.urllib.request, "urlopen", lambda *_a, **_k: _Resp())
    snap = opencodex_probe.snapshot(base_url="http://127.0.0.1:10100")
    assert snap["kind"] == "opencodex"
    assert snap["models"] == ["gpt-proxy", "local-x"]


def test_list_models_stays_loopback(monkeypatch):
    monkeypatch.setattr(opencodex_probe.shutil, "which", lambda _name: "/usr/bin/ocx")
    called = []

    def _open(url, timeout=1.5):  # noqa: ANN001
        called.append(url)
        raise AssertionError("must not fetch non-loopback")

    monkeypatch.setattr(opencodex_probe.urllib.request, "urlopen", _open)
    snap = opencodex_probe.snapshot(base_url="http://10.0.0.8:10100")
    assert snap["reachable"] is False
    assert snap["models"] == []
    assert called == []


def test_list_models_401_is_empty_not_crash(monkeypatch):
    monkeypatch.setattr(opencodex_probe, "is_loopback_url", lambda _url: True)
    def _raise(url, timeout=1.5):  # noqa: ANN001
        raise HTTPError(url, 401, "auth", hdrs={}, fp=BytesIO())

    monkeypatch.setattr(opencodex_probe.urllib.request, "urlopen", _raise)
    assert opencodex_probe._list_models("http://127.0.0.1:10100", timeout=1.5) == []
