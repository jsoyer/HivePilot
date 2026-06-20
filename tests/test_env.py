"""
Tests for hivepilot.utils.env — proxy_env() function.
"""

from __future__ import annotations

import pytest

from hivepilot.utils.env import proxy_env


class TestProxyEnv:
    """Tests for proxy_env()."""

    def test_returns_empty_dict_when_no_proxy_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """proxy_env() returns {} when no proxy env vars are set."""
        proxy_keys = (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "all_proxy",
        )
        for key in proxy_keys:
            monkeypatch.delenv(key, raising=False)

        result = proxy_env()
        assert result == {}

    def test_returns_present_proxy_keys_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """proxy_env() returns only the proxy keys actually set in the environment."""
        proxy_keys = (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "all_proxy",
        )
        for key in proxy_keys:
            monkeypatch.delenv(key, raising=False)

        monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.com:8080")
        monkeypatch.setenv("HTTPS_PROXY", "https://proxy.example.com:8080")

        result = proxy_env()

        assert result == {
            "HTTP_PROXY": "http://proxy.example.com:8080",
            "HTTPS_PROXY": "https://proxy.example.com:8080",
        }

    def test_returns_lowercase_proxy_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """proxy_env() includes lowercase variants when set."""
        proxy_keys = (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "all_proxy",
        )
        for key in proxy_keys:
            monkeypatch.delenv(key, raising=False)

        monkeypatch.setenv("http_proxy", "http://lowercase.proxy:3128")
        monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")

        result = proxy_env()

        assert result == {
            "http_proxy": "http://lowercase.proxy:3128",
            "no_proxy": "localhost,127.0.0.1",
        }

    def test_returns_all_proxy_keys_when_all_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """proxy_env() returns all 8 keys when all are set."""
        proxy_keys = (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "all_proxy",
        )
        for key in proxy_keys:
            monkeypatch.setenv(key, f"val_{key}")

        result = proxy_env()

        assert len(result) == 8
        for key in proxy_keys:
            assert key in result

    def test_does_not_include_non_proxy_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """proxy_env() does not include non-proxy environment variables."""
        proxy_keys = (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "NO_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "no_proxy",
            "all_proxy",
        )
        for key in proxy_keys:
            monkeypatch.delenv(key, raising=False)

        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("HOME", "/root")

        result = proxy_env()
        assert "PATH" not in result
        assert "HOME" not in result
