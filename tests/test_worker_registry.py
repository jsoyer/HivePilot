"""W2 — worker health registry: hub pings each worker's /health and records it."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from hivepilot.services import state_service, worker_registry


def test_ping_worker_live() -> None:
    resp = MagicMock(status_code=200)
    with patch("hivepilot.services.worker_registry.requests.get", return_value=resp) as m:
        ok, detail = worker_registry.ping_worker("https://hostC:8900")
    assert ok is True
    assert detail is None
    url = m.call_args.args[0] if m.call_args.args else m.call_args.kwargs["url"]
    assert url == "https://hostC:8900/health"


def test_ping_worker_unreachable() -> None:
    with patch("hivepilot.services.worker_registry.requests.get", side_effect=OSError("boom")):
        ok, detail = worker_registry.ping_worker("https://down:8900")
    assert ok is False
    assert detail is not None and "boom" in detail


def test_ping_worker_non_200() -> None:
    resp = MagicMock(status_code=503)
    with patch("hivepilot.services.worker_registry.requests.get", return_value=resp):
        ok, _ = worker_registry.ping_worker("https://x")
    assert ok is False


def test_refresh_records_worker_health(monkeypatch) -> None:
    monkeypatch.setattr(worker_registry, "_worker_urls", lambda: {"https://hostC:8900"})
    monkeypatch.setattr(worker_registry, "ping_worker", lambda url: (True, None))
    result = worker_registry.refresh()
    assert any(w["url"] == "https://hostC:8900" and w["status"] == "live" for w in result)
    # persisted in state
    assert any(w["status"] == "live" for w in state_service.list_workers())


def test_worker_urls_discovers_http_role_hosts(monkeypatch) -> None:
    # a role with an http(s) host is a worker; ssh aliases / None are not
    monkeypatch.setattr(
        worker_registry,
        "ROLES",
        {"a": object(), "b": object(), "c": object()},
        raising=False,
    )
    hosts = {"a": "https://h1", "b": "machineB", "c": None}
    monkeypatch.setattr(worker_registry, "resolve_host", lambda name: hosts[name])
    assert worker_registry._worker_urls() == {"https://h1"}
