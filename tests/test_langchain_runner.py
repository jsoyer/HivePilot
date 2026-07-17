"""LangChainRunner capability contract (Sprint 1: mode cli|api)."""

from __future__ import annotations

from hivepilot.runners.langchain_runner import LangChainRunner


def test_langchain_runner_is_cli_only() -> None:
    assert LangChainRunner.supported_modes == frozenset({"cli"})
