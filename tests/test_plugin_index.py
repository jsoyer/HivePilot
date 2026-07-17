"""
Tests for Phase 26b Approach A — `hivepilot/services/plugin_index.py`.

Covers `fetch_index` (metadata-only HTTP GET of a JSON index, fail-safe on
network/config/parse errors, skips malformed entries) and `search_index`
(case-insensitive substring match on name+description).

CRITICAL: this module must NEVER download, import, or execute plugin code —
only fetch and parse metadata. See docs/v4/PLUGINS.md "Trust model".
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from hivepilot.services.plugin_index import PluginIndexEntry, fetch_index, search_index


def _mock_response(*, status_code: int = 200, json_data=None, json_error: bool = False):
    resp = MagicMock()
    resp.status_code = status_code
    if json_error:
        resp.json.side_effect = ValueError("bad json")
    else:
        resp.json.return_value = json_data
    return resp


class TestFetchIndex:
    def test_no_url_configured_raises_without_network_call(self, monkeypatch) -> None:
        monkeypatch.setattr("hivepilot.services.plugin_index.settings.plugins_index_url", "")
        with patch("hivepilot.services.plugin_index.requests.get") as mock_get:
            with pytest.raises(RuntimeError, match="no plugin index configured"):
                fetch_index()
            mock_get.assert_not_called()

    def test_valid_json_returns_entries(self) -> None:
        payload = {
            "plugins": [
                {
                    "name": "hugo",
                    "description": "Static site runner",
                    "author": "jsoyer",
                    "homepage": "https://example.com/hugo",
                    "install": {"type": "pip", "target": "hivepilot-plugin-hugo"},
                    "version": "1.0.0",
                    "checksum": "sha256:abc123",
                    "contributes": ["runners"],
                }
            ]
        }
        with patch(
            "hivepilot.services.plugin_index.requests.get",
            return_value=_mock_response(json_data=payload),
        ):
            entries = fetch_index(url="https://index.example.com/plugins.json")

        assert entries == [
            PluginIndexEntry(
                name="hugo",
                description="Static site runner",
                author="jsoyer",
                homepage="https://example.com/hugo",
                install={"type": "pip", "target": "hivepilot-plugin-hugo"},
                version="1.0.0",
                checksum="sha256:abc123",
                contributes=["runners"],
            )
        ]

    def test_bare_list_payload_also_supported(self) -> None:
        payload = [{"name": "foo", "description": "Foo plugin"}]
        with patch(
            "hivepilot.services.plugin_index.requests.get",
            return_value=_mock_response(json_data=payload),
        ):
            entries = fetch_index(url="https://index.example.com/plugins.json")
        assert len(entries) == 1
        assert entries[0].name == "foo"

    def test_network_error_raises_runtime_error_no_raw_body(self) -> None:
        with patch(
            "hivepilot.services.plugin_index.requests.get",
            side_effect=requests.ConnectionError("connection refused to secret-internal-host"),
        ):
            with pytest.raises(RuntimeError) as exc_info:
                fetch_index(url="https://index.example.com/plugins.json")
        message = str(exc_info.value)
        assert "secret-internal-host" not in message

    def test_timeout_raises_runtime_error(self) -> None:
        with patch(
            "hivepilot.services.plugin_index.requests.get",
            side_effect=requests.Timeout("timed out"),
        ):
            with pytest.raises(RuntimeError):
                fetch_index(url="https://index.example.com/plugins.json")

    def test_non_200_raises_runtime_error_no_raw_body(self) -> None:
        resp = _mock_response(status_code=500)
        resp.text = "<html>super secret stack trace</html>"
        with patch("hivepilot.services.plugin_index.requests.get", return_value=resp):
            with pytest.raises(RuntimeError) as exc_info:
                fetch_index(url="https://index.example.com/plugins.json")
        message = str(exc_info.value)
        assert "500" in message
        assert "super secret stack trace" not in message

    def test_invalid_json_raises_runtime_error(self) -> None:
        resp = _mock_response(json_error=True)
        with patch("hivepilot.services.plugin_index.requests.get", return_value=resp):
            with pytest.raises(RuntimeError, match="not valid JSON"):
                fetch_index(url="https://index.example.com/plugins.json")

    def test_malformed_entry_skipped_rest_returned(self) -> None:
        payload = {
            "plugins": [
                {"name": "good-one", "description": "Valid entry"},
                {"description": "Missing name entirely"},
                {"name": "", "description": "Empty name"},
                "not-even-a-dict",
                {"name": "also-good", "description": "Another valid one"},
            ]
        }
        with patch(
            "hivepilot.services.plugin_index.requests.get",
            return_value=_mock_response(json_data=payload),
        ):
            entries = fetch_index(url="https://index.example.com/plugins.json")

        names = [e.name for e in entries]
        assert names == ["good-one", "also-good"]

    def test_non_list_plugins_field_raises(self) -> None:
        with patch(
            "hivepilot.services.plugin_index.requests.get",
            return_value=_mock_response(json_data={"plugins": "not-a-list"}),
        ):
            with pytest.raises(RuntimeError):
                fetch_index(url="https://index.example.com/plugins.json")

    def test_uses_settings_url_when_none_passed(self, monkeypatch) -> None:
        monkeypatch.setattr(
            "hivepilot.services.plugin_index.settings.plugins_index_url",
            "https://configured.example.com/index.json",
        )
        with patch(
            "hivepilot.services.plugin_index.requests.get",
            return_value=_mock_response(json_data={"plugins": []}),
        ) as mock_get:
            fetch_index()
        mock_get.assert_called_once()
        called_url = mock_get.call_args[0][0]
        assert called_url == "https://configured.example.com/index.json"


class TestSearchIndex:
    def _entries(self) -> list[PluginIndexEntry]:
        return [
            PluginIndexEntry(name="hugo", description="Static site runner plugin"),
            PluginIndexEntry(name="obsidian", description="Notifier for Obsidian vaults"),
            PluginIndexEntry(name="rtk", description="Rust Token Killer runner"),
        ]

    def test_empty_query_returns_all(self) -> None:
        entries = self._entries()
        assert search_index(entries, "") == entries

    def test_matches_name_case_insensitive(self) -> None:
        entries = self._entries()
        result = search_index(entries, "HUGO")
        assert [e.name for e in result] == ["hugo"]

    def test_matches_description_case_insensitive(self) -> None:
        entries = self._entries()
        result = search_index(entries, "VAULTS")
        assert [e.name for e in result] == ["obsidian"]

    def test_substring_matches_multiple(self) -> None:
        entries = self._entries()
        result = search_index(entries, "runner")
        assert {e.name for e in result} == {"hugo", "rtk"}

    def test_no_match_returns_empty(self) -> None:
        entries = self._entries()
        assert search_index(entries, "nonexistent-plugin-xyz") == []
