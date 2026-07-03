"""Tests for hivepilot.services.knowledge_service.

knowledge_service no longer imports langchain at module level: build_context
falls back to a plain file read when the optional embedding stack is absent
(the default in this test env). These tests verify that fallback.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.services import knowledge_service as ks


@pytest.fixture(autouse=True)
def _force_plain_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """conftest stubs langchain as MagicMocks, so the optional embedding path
    would falsely 'succeed' and return junk. Force the plain-read fallback —
    the default production path when the embedding stack isn't installed."""
    monkeypatch.setattr(ks, "_embedding_context", lambda *a, **k: None)


def test_module_imports_without_langchain() -> None:
    # Importing the module must not require langchain/torch.
    assert callable(ks.build_context)
    assert callable(ks.append_feedback)


def test_build_context_plain_read(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("hello readme", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "arch.md").write_text("arch notes", encoding="utf-8")
    ctx = ks.build_context(tmp_path, [Path("README.md"), Path("docs/arch.md")])
    assert "hello readme" in ctx
    assert "arch notes" in ctx
    assert "# README.md" in ctx


def test_build_context_skips_missing_files(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("present", encoding="utf-8")
    ctx = ks.build_context(tmp_path, [Path("README.md"), Path("nope.md")])
    assert "present" in ctx
    assert "nope.md" not in ctx


def test_build_context_truncates_large_files(tmp_path: Path) -> None:
    (tmp_path / "big.md").write_text("x" * 10000, encoding="utf-8")
    ctx = ks.build_context(tmp_path, [Path("big.md")])
    assert "…(truncated)" in ctx
    assert len(ctx) < 10000


def test_append_feedback_then_included_in_context(tmp_path: Path, monkeypatch) -> None:
    fb = tmp_path / "fb"
    fb.mkdir()
    monkeypatch.setattr(ks, "FEEDBACK_DIR", fb)
    (tmp_path / "README.md").write_text("doc", encoding="utf-8")
    ks.append_feedback(tmp_path, "task-x", "did a thing")
    ctx = ks.build_context(tmp_path, [Path("README.md")])
    assert "Recent AI feedback" in ctx
    assert "did a thing" in ctx
