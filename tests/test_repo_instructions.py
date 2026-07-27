"""hivepilot.services.repo_instructions — resolve + inline a project's
declared repository instruction/context files (``ProjectConfig.claude_md`` /
``.instruction_files``) into the agent prompt, instead of only naming them.

Root-cause bug: `claude_runner._build_prompt` used to emit
``f"Repository instructions file: {payload.project.claude_md}"`` — a bare
filename the agent was expected to go read itself, with ZERO handling for a
file that doesn't exist (silent no-op). See
`hivepilot/runners/claude_runner.py` history for the pre-fix line.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from hivepilot.config import Settings
from hivepilot.models import ProjectConfig
from hivepilot.services.config_provenance import register_secret_value
from hivepilot.services.repo_instructions import build_repo_instructions_section


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_returns_none_when_no_instructions_declared(tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    assert build_repo_instructions_section(project, _settings()) is None


def test_claude_md_content_is_inlined_not_just_named(tmp_path: Path) -> None:
    """Test 1 — content appears, not just the filename."""
    (tmp_path / "CLAUDE.md").write_text("ALWAYS run tests before committing.", encoding="utf-8")
    project = ProjectConfig(path=tmp_path, claude_md="CLAUDE.md")
    out = build_repo_instructions_section(project, _settings())
    assert out is not None
    assert "ALWAYS run tests before committing." in out


def test_multiple_files_all_injected_in_order_and_delimited(tmp_path: Path) -> None:
    """Test 2 — multiple files, all injected, in declared order, each delimited."""
    (tmp_path / "CLAUDE.md").write_text("FIRST-FILE-CONTENT", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("SECOND-FILE-CONTENT", encoding="utf-8")
    (tmp_path / "RULES.md").write_text("THIRD-FILE-CONTENT", encoding="utf-8")
    project = ProjectConfig(
        path=tmp_path,
        claude_md="CLAUDE.md",
        instruction_files=["AGENTS.md", "RULES.md"],
    )
    out = build_repo_instructions_section(project, _settings())
    assert out is not None
    assert "FIRST-FILE-CONTENT" in out
    assert "SECOND-FILE-CONTENT" in out
    assert "THIRD-FILE-CONTENT" in out
    # Order preserved: claude_md first, then instruction_files in declared order.
    idx_first = out.index("FIRST-FILE-CONTENT")
    idx_second = out.index("SECOND-FILE-CONTENT")
    idx_third = out.index("THIRD-FILE-CONTENT")
    assert idx_first < idx_second < idx_third
    # Each file gets its own clearly delimited block naming the declared file.
    assert out.count("CLAUDE.md") >= 2  # opening + closing delimiter
    assert out.count("AGENTS.md") >= 2
    assert out.count("RULES.md") >= 2


def test_missing_file_warns_loudly_and_marks_absence(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Test 3 — a declared file that does not exist produces a visible
    warning naming the file + searched dir, and the run must NOT proceed as
    if governance content were present (no silent no-op)."""
    project = ProjectConfig(path=tmp_path, claude_md="MISSING-GOVERNANCE.md")
    with caplog.at_level(logging.WARNING):
        out = build_repo_instructions_section(project, _settings())
    assert out is not None
    assert "MISSING-GOVERNANCE.md" in out
    assert "NOT FOUND" in out.upper()
    assert str(tmp_path) in out  # names the searched directory
    log_text = caplog.text
    assert "repo_instructions.missing_file" in log_text
    assert "MISSING-GOVERNANCE.md" in log_text
    assert str(tmp_path) in log_text


def test_claude_md_alone_byte_identical_except_content_inlined(tmp_path: Path) -> None:
    """Test 4 — `claude_md` alone (new field absent) produces the SAME
    section content as today's filename-only line, except the file's real
    content now appears instead of merely its name. This documents the
    INTENDED behavior change: before this fix, the prompt only ever said
    'Repository instructions file: CLAUDE.md' (agent had to fetch it
    itself); after, the literal content is inlined so the section is no
    longer just the bare filename string.
    """
    (tmp_path / "CLAUDE.md").write_text("Repo policy: squash-merge only.", encoding="utf-8")
    project = ProjectConfig(path=tmp_path, claude_md="CLAUDE.md")
    out = build_repo_instructions_section(project, _settings())
    assert out is not None
    # Old behavior would have been exactly this string and NOTHING else useful:
    old_style_line = "Repository instructions file: CLAUDE.md"
    # The new section still names the file (for traceability) ...
    assert "CLAUDE.md" in out
    # ...but the bare old-style "tell them to go read it" line is gone, and
    # the actual content is present instead.
    assert old_style_line not in out
    assert "Repo policy: squash-merge only." in out


def test_oversized_file_is_truncated_with_explicit_notice(tmp_path: Path) -> None:
    """Test 5 — an oversized file is truncated with an explicit in-band
    notice (never a silent truncation)."""
    big_content = "A" * 5000
    (tmp_path / "CLAUDE.md").write_text(big_content, encoding="utf-8")
    project = ProjectConfig(path=tmp_path, claude_md="CLAUDE.md")
    out = build_repo_instructions_section(
        project, _settings(instruction_file_max_bytes=100, instruction_files_max_total_bytes=1000)
    )
    assert out is not None
    assert "A" * 100 in out
    assert "A" * 101 not in out
    assert "TRUNCATED" in out.upper()


def test_secret_shaped_value_is_redacted(tmp_path: Path) -> None:
    """Test 6 — a secret pasted into an instructions file is redacted in the
    injected text via the existing `config_provenance` redaction choke
    point (the same sink every other runner output is masked through)."""
    secret = "sk-TOTALLY-SECRET-INSTRUCTIONS-TOKEN"  # noqa: S105 - test fixture
    register_secret_value(secret)
    (tmp_path / "CLAUDE.md").write_text(
        f"Use this API token when calling the service: {secret}", encoding="utf-8"
    )
    project = ProjectConfig(path=tmp_path, claude_md="CLAUDE.md")
    out = build_repo_instructions_section(project, _settings())
    assert out is not None
    assert secret not in out
    assert "REDACTED" in out


def test_absolute_path_outside_project_is_supported(tmp_path: Path) -> None:
    """Resolution path — a monorepo/umbrella pattern where the instructions
    file lives OUTSIDE the project's checkout (e.g. the parent directory) is
    supported via an explicit relative ('../X') or absolute path; there is
    no implicit parent-directory walk/search."""
    # `project_root` is the product repo checkout; the instructions file
    # lives in ITS PARENT (`tmp_path`) -- the exact umbrella/monorepo shape
    # described in the PRD.
    outside_file = tmp_path / "OUTSIDE-GOVERNANCE.md"
    outside_file.write_text("OUTSIDE-CONTENT", encoding="utf-8")
    project_root = tmp_path / "repo"
    project_root.mkdir()
    project = ProjectConfig(path=project_root, claude_md=f"../{outside_file.name}")
    out = build_repo_instructions_section(project, _settings())
    assert out is not None
    assert "OUTSIDE-CONTENT" in out


def test_relative_path_resolves_against_project_path(tmp_path: Path) -> None:
    sub = tmp_path / "docs"
    sub.mkdir()
    (sub / "AGENTS.md").write_text("NESTED-CONTENT", encoding="utf-8")
    project = ProjectConfig(path=tmp_path, instruction_files=["docs/AGENTS.md"])
    out = build_repo_instructions_section(project, _settings())
    assert out is not None
    assert "NESTED-CONTENT" in out


def test_duplicate_declared_file_not_injected_twice(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("DUP-CONTENT", encoding="utf-8")
    project = ProjectConfig(path=tmp_path, claude_md="CLAUDE.md", instruction_files=["CLAUDE.md"])
    out = build_repo_instructions_section(project, _settings())
    assert out is not None
    assert out.count("DUP-CONTENT") == 1
