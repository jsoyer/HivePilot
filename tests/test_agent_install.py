"""Unit tests for `hivepilot.services.agent_install`.

Security-critical module: an operator-consent gate in front of running a
vendor's official installer (`curl ... | sh`). The single most important
property under test is `test_propose_install_non_interactive_refuses_even_with_assume_yes`
— a non-interactive / scheduled caller must NEVER be able to trigger
execution, no matter what flags it passes.
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Optional
from unittest.mock import MagicMock

import pytest

from hivepilot.services import agent_install
from hivepilot.services.agent_install import (
    AGENT_INSTALL_SPECS,
    InstallResult,
    InstallSpec,
    get_install_spec,
    is_on_path,
    missing_agents,
    propose_install,
)


def _fake_which(present: set[str]):
    def _which(name: str) -> Optional[str]:
        return f"/usr/bin/{name}" if name in present else None

    return _which


# ---------------------------------------------------------------------------
# is_on_path / get_install_spec / missing_agents
# ---------------------------------------------------------------------------


def test_is_on_path_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", _fake_which({"claude"}))
    assert is_on_path("claude") is True


def test_is_on_path_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", _fake_which(set()))
    assert is_on_path("claude") is False


def test_get_install_spec_known_kind() -> None:
    spec = get_install_spec("claude")
    assert spec is not None
    assert spec.binary == "claude"


def test_get_install_spec_unknown_kind_returns_none() -> None:
    assert get_install_spec("not-a-real-agent") is None


def test_missing_agents_uses_spec_binary_not_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "cursor" kind installs binary "cursor-agent" — missing_agents must probe
    PATH for the binary, not the kind string."""
    monkeypatch.setattr(shutil, "which", _fake_which({"cursor-agent"}))
    assert "cursor" not in missing_agents(["cursor"])


def test_missing_agents_reports_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", _fake_which(set()))
    assert missing_agents(["claude", "codex"]) == ["claude", "codex"]


def test_missing_agents_preserves_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", _fake_which({"codex"}))
    assert missing_agents(["claude", "codex", "gh"]) == ["claude", "gh"]


def test_missing_agents_unregistered_kind_falls_back_to_kind_as_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", _fake_which({"mystery-tool"}))
    assert missing_agents(["mystery-tool"]) == []


# ---------------------------------------------------------------------------
# propose_install — docs-only (command=None)
# ---------------------------------------------------------------------------


def test_propose_install_docs_only_never_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_run = MagicMock()
    monkeypatch.setattr(subprocess, "run", mock_run)
    spec = InstallSpec(
        name="Example CLI",
        binary="example",
        vendor="Example Inc",
        docs_url="https://example.com/docs/install",
        command=None,
    )
    result = propose_install(spec, assume_yes=True, interactive=True)
    assert result.ran is False
    assert result.exit_code is None
    assert "https://example.com/docs/install" in result.message
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# propose_install — non-interactive guard (THE key security test)
# ---------------------------------------------------------------------------


def test_propose_install_non_interactive_refuses_even_with_assume_yes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_run = MagicMock()
    monkeypatch.setattr(subprocess, "run", mock_run)
    spec = InstallSpec(
        name="Example CLI",
        binary="example",
        vendor="Example Inc",
        docs_url="https://example.com/docs/install",
        command="curl -fsSL https://example.com/install.sh | bash",
    )
    result = propose_install(spec, assume_yes=True, interactive=False)
    assert result.ran is False
    assert result.exit_code is None
    assert "non-interactive" in result.message
    assert spec.command in result.message
    mock_run.assert_not_called()


def test_propose_install_non_interactive_default_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When `interactive` is not passed explicitly, it must be derived from
    stdin/stdout isatty() — simulating a headless run (both False) must
    refuse, even with assume_yes=True."""
    mock_run = MagicMock()
    monkeypatch.setattr(subprocess, "run", mock_run)
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    spec = InstallSpec(
        name="Example CLI",
        binary="example",
        vendor="Example Inc",
        docs_url="https://example.com/docs/install",
        command="curl -fsSL https://example.com/install.sh | bash",
    )
    result = propose_install(spec, assume_yes=True)
    assert result.ran is False
    mock_run.assert_not_called()


@pytest.mark.parametrize(
    ("stdin_tty", "stdout_tty"),
    [
        (True, False),  # a TTY stdin but a piped/redirected stdout
        (False, True),  # a piped/redirected stdin but a TTY stdout
    ],
)
def test_propose_install_mixed_tty_is_non_interactive_and_refuses(
    monkeypatch: pytest.MonkeyPatch, stdin_tty: bool, stdout_tty: bool
) -> None:
    """`interactive` is `stdin.isatty() AND stdout.isatty()` — ANY single
    non-TTY stream (redirected/piped) means non-interactive, so the installer
    must refuse WITHOUT executing even with assume_yes=True. Locks the `and`
    logic so a future collapse to a single-stream check is caught by CI."""
    mock_run = MagicMock()
    monkeypatch.setattr(subprocess, "run", mock_run)
    monkeypatch.setattr("sys.stdin.isatty", lambda: stdin_tty)
    monkeypatch.setattr("sys.stdout.isatty", lambda: stdout_tty)
    spec = InstallSpec(
        name="Example CLI",
        binary="example",
        vendor="Example Inc",
        docs_url="https://example.com/docs/install",
        command="curl -fsSL https://example.com/install.sh | bash",
    )
    result = propose_install(spec, assume_yes=True)
    assert result.ran is False
    assert result.exit_code is None
    assert "non-interactive" in result.message
    mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# propose_install — interactive
# ---------------------------------------------------------------------------


def test_propose_install_interactive_decline_does_not_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_run = MagicMock()
    monkeypatch.setattr(subprocess, "run", mock_run)
    monkeypatch.setattr("builtins.input", lambda *_args, **_kwargs: "n")
    spec = InstallSpec(
        name="Example CLI",
        binary="example",
        vendor="Example Inc",
        docs_url="https://example.com/docs/install",
        command="curl -fsSL https://example.com/install.sh | bash",
    )
    result = propose_install(spec, interactive=True)
    assert result.ran is False
    assert result.exit_code is None
    mock_run.assert_not_called()


def test_propose_install_interactive_default_declines(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-No: an empty answer must NOT be treated as consent."""
    mock_run = MagicMock()
    monkeypatch.setattr(subprocess, "run", mock_run)
    monkeypatch.setattr("builtins.input", lambda *_args, **_kwargs: "")
    spec = InstallSpec(
        name="Example CLI",
        binary="example",
        vendor="Example Inc",
        docs_url="https://example.com/docs/install",
        command="curl -fsSL https://example.com/install.sh | bash",
    )
    result = propose_install(spec, interactive=True)
    assert result.ran is False
    mock_run.assert_not_called()


def test_propose_install_interactive_yes_runs_exact_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(subprocess, "run", mock_run)
    monkeypatch.setattr("builtins.input", lambda *_args, **_kwargs: "y")
    spec = InstallSpec(
        name="Example CLI",
        binary="example",
        vendor="Example Inc",
        docs_url="https://example.com/docs/install",
        command="curl -fsSL https://example.com/install.sh | bash",
    )
    result = propose_install(spec, interactive=True)
    assert result.ran is True
    assert result.exit_code == 0
    mock_run.assert_called_once()
    args, kwargs = mock_run.call_args
    assert args[0] == ["bash", "-lc", spec.command]
    assert kwargs.get("check") is False


def test_propose_install_interactive_assume_yes_skips_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(subprocess, "run", mock_run)

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("input() must not be called when assume_yes=True")

    monkeypatch.setattr("builtins.input", _fail_if_called)
    spec = InstallSpec(
        name="Example CLI",
        binary="example",
        vendor="Example Inc",
        docs_url="https://example.com/docs/install",
        command="curl -fsSL https://example.com/install.sh | bash",
    )
    result = propose_install(spec, assume_yes=True, interactive=True)
    assert result.ran is True
    assert result.exit_code == 0
    mock_run.assert_called_once()
    args, _kwargs = mock_run.call_args
    assert args[0] == ["bash", "-lc", spec.command]


def test_propose_install_reports_nonzero_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_run = MagicMock(return_value=MagicMock(returncode=1))
    monkeypatch.setattr(subprocess, "run", mock_run)
    spec = InstallSpec(
        name="Example CLI",
        binary="example",
        vendor="Example Inc",
        docs_url="https://example.com/docs/install",
        command="curl -fsSL https://example.com/install.sh | bash",
    )
    result = propose_install(spec, assume_yes=True, interactive=True)
    assert result.ran is True
    assert result.exit_code == 1


def test_install_result_is_dataclass_with_expected_fields() -> None:
    result = InstallResult(ran=False, exit_code=None, message="x")
    assert result.ran is False
    assert result.exit_code is None
    assert result.message == "x"


# ---------------------------------------------------------------------------
# Registry sanity: every spec is well-formed and sourced from a real vendor
# domain. Not a network call — a light guard against typos/fabrication.
# ---------------------------------------------------------------------------

_EXPECTED_KINDS = {
    "claude",
    "codex",
    "cursor",
    "gemini",
    "opencode",
    "ollama",
    "kimi-cli",
    "qwen-code",
    "vibe",
    "antigravity",
    "gh",
}

# For each kind with a pinned `command`, the domain we expect to see in it —
# a light guard that the command actually targets the vendor's own official
# host cited in `docs_url`, not something fabricated.
_EXPECTED_COMMAND_DOMAIN = {
    "claude": "claude.ai",
    "codex": "chatgpt.com",
    "cursor": "cursor.com",
    "opencode": "opencode.ai",
    "ollama": "ollama.com",
    "vibe": "mistral.ai",
    "antigravity": "antigravity.google",
}


def test_registry_has_exactly_the_expected_kinds() -> None:
    assert set(AGENT_INSTALL_SPECS.keys()) == _EXPECTED_KINDS


@pytest.mark.parametrize("kind", sorted(_EXPECTED_KINDS))
def test_every_spec_has_non_empty_binary_and_docs_url(kind: str) -> None:
    spec = AGENT_INSTALL_SPECS[kind]
    assert spec.binary
    assert spec.docs_url.startswith("https://")
    assert spec.name
    assert spec.vendor


@pytest.mark.parametrize("kind", sorted(_EXPECTED_COMMAND_DOMAIN))
def test_pinned_commands_target_official_vendor_domain(kind: str) -> None:
    spec = AGENT_INSTALL_SPECS[kind]
    assert spec.command is not None
    assert spec.command.startswith("curl ")
    assert "| bash" in spec.command or "| sh" in spec.command
    assert _EXPECTED_COMMAND_DOMAIN[kind] in spec.command


@pytest.mark.parametrize("kind", ["gemini", "kimi-cli", "qwen-code", "gh"])
def test_package_manager_only_agents_are_docs_only(kind: str) -> None:
    """gemini/kimi-cli/qwen-code/gh are package-manager-based (npm/uv/brew) —
    per the security posture, those must never be auto-runnable."""
    spec = AGENT_INSTALL_SPECS[kind]
    assert spec.command is None


def test_no_secret_or_sensitive_data_in_any_spec() -> None:
    banned_substrings = ("api_key", "apikey", "token=", "password", "secret")
    for spec in AGENT_INSTALL_SPECS.values():
        haystack = " ".join(
            [spec.name, spec.binary, spec.vendor, spec.docs_url, spec.command or ""]
        ).lower()
        for banned in banned_substrings:
            assert banned not in haystack


def test_module_docstring_warns_maintainer_must_vet() -> None:
    assert "MAINTAINER MUST VET" in (agent_install.__doc__ or "")
