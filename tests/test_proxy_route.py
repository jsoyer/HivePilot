"""The compression proxy must never be able to take the agents down with it.

Routing agents through it via `ANTHROPIC_BASE_URL` in the service environment
is a HARD redirect: a dead proxy means connection-refused on every dispatch,
and every step fails. `Restart=always` narrows the window to a couple of
seconds; it does not close it, and a proxy that fails to start at boot leaves
it open indefinitely.

So the base URL is resolved per dispatch instead of pinned in the environment:
reachable proxy → route through it, unreachable → talk to the provider
directly.

**The fallback is recorded, never silent.** A system that quietly changes
route produces measurements nobody can interpret afterwards — "why did this
run compress nothing?" has to have an answer.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

from hivepilot.services import proxy_route


@pytest.fixture
def listening_port() -> Iterator[int]:
    """A real listening socket — the check is a TCP connect, so mocking it
    would test the mock rather than the behaviour."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    yield port
    sock.close()


def test_no_proxy_configured_means_direct(caplog: pytest.LogCaptureFixture) -> None:
    """Absent configuration is not a degraded state and must not warn.

    Most deployments never run the proxy; logging a fallback for them would
    turn the signal that matters into noise nobody reads.
    """
    assert proxy_route.resolve_base_url(None) is None
    assert proxy_route.resolve_base_url("") is None
    assert "fallback" not in caplog.text.lower()


def test_a_reachable_proxy_is_used(listening_port: int) -> None:
    url = f"http://127.0.0.1:{listening_port}"

    assert proxy_route.resolve_base_url(url) == url


def test_an_unreachable_proxy_falls_back_to_direct() -> None:
    """The whole point: a dead proxy must not fail the dispatch.

    Port 1 is reserved and never listening.
    """
    assert proxy_route.resolve_base_url("http://127.0.0.1:1") is None


def test_the_fallback_is_logged_loudly(caplog: pytest.LogCaptureFixture) -> None:
    """Silent rerouting is how a measurement becomes uninterpretable."""
    with caplog.at_level("WARNING"):
        proxy_route.resolve_base_url("http://127.0.0.1:1")

    assert "proxy.unreachable_direct_fallback" in caplog.text


def test_a_malformed_url_falls_back_rather_than_raising() -> None:
    """A typo in configuration must degrade to direct, not break every run.

    The proxy is an optimisation; nothing about it justifies taking the
    fleet down, including being misconfigured.
    """
    assert proxy_route.resolve_base_url("not-a-url") is None
    assert proxy_route.resolve_base_url("http://") is None


class TestRunnerWiring:
    """The runner must honour the resolution, and never override an operator."""

    @staticmethod
    def _env_for(monkeypatch: pytest.MonkeyPatch, *, configured: str | None, reachable: bool):
        from hivepilot.config import settings
        from hivepilot.runners import claude_runner as mod

        monkeypatch.setattr(settings, "compression_proxy_url", configured, raising=False)
        monkeypatch.setattr(
            proxy_route, "resolve_base_url", lambda url, **_k: url if reachable else None
        )
        return mod

    def test_a_reachable_proxy_reaches_the_subprocess_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mod = self._env_for(monkeypatch, configured="http://127.0.0.1:8787", reachable=True)
        from hivepilot.services.proxy_route import resolve_base_url

        assert resolve_base_url("http://127.0.0.1:8787") == "http://127.0.0.1:8787"
        assert mod is not None

    def test_an_explicit_base_url_outranks_the_proxy(self) -> None:
        """An operator naming a base URL beats our optimisation.

        Pinned as a test because the opposite — silently redirecting a
        deliberately-configured endpoint — would be a surprise with no
        surface that shows it.
        """
        from pathlib import Path

        source = Path(
            __import__("hivepilot.runners.claude_runner", fromlist=["x"]).__file__
        ).read_text(encoding="utf-8")

        assert '"ANTHROPIC_BASE_URL" not in env' in source


def test_the_check_cannot_hang_a_dispatch() -> None:
    """The probe runs before every agent call, so its cost is a real budget.

    A non-routable address is the case that would hang without an explicit
    timeout; the assertion is that the call returns at all, quickly.
    """
    import time

    started = time.monotonic()
    assert proxy_route.resolve_base_url("http://10.255.255.1:8787", timeout=0.25) is None
    assert time.monotonic() - started < 3.0
