"""Tests for the ``hivepilot secrets cache-clear`` CLI command (Phase 19
follow-up: secret TTL cache flush).

Stubs optional heavy deps before importing ``hivepilot.cli`` (mirrors
tests/test_cli_autopilot.py) so this file runs standalone.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

_STUBS = [
    "langchain",
    "langchain.text_splitter",
    "langchain_community",
    "langchain_community.embeddings",
    "langchain_community.vectorstores",
    "langchain_openai",
    "openai",
    "boto3",
    "docker",
    "telegram",
    "telegram.ext",
    "fastapi",
    "fastapi.responses",
    "fastapi.security",
    "uvicorn",
    "textual",
    "slack_bolt",
    "slack_bolt.adapter",
    "slack_bolt.adapter.fastapi",
    "slack_bolt.adapter.socket_mode",
    "discord",
    "PyNaCl",
    "nacl",
    "nacl.exceptions",
    "nacl.signing",
]

import importlib  # noqa: E402

for _mod in _STUBS:
    if _mod in sys.modules:
        continue
    try:
        importlib.import_module(_mod)
    except Exception:
        sys.modules[_mod] = MagicMock()

from typer.testing import CliRunner  # noqa: E402

from hivepilot.cli import app  # noqa: E402
from hivepilot.services import secrets_service  # noqa: E402


def test_secrets_cache_clear_empties_the_cache() -> None:
    # Seed the process-local cache with a fake entry.
    secrets_service._SECRET_CACHE["k"] = (secrets_service._now() + 100, "v")
    assert secrets_service._SECRET_CACHE

    result = CliRunner().invoke(app, ["secrets", "cache-clear"])

    assert result.exit_code == 0
    assert "cleared" in result.stdout.lower()
    assert secrets_service._SECRET_CACHE == {}


def test_secrets_cache_clear_is_safe_when_already_empty() -> None:
    secrets_service.clear_secret_cache()
    result = CliRunner().invoke(app, ["secrets", "cache-clear"])
    assert result.exit_code == 0
