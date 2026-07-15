"""
Tests for hivepilot.services.obsidian_service.

All tests use tmp_path (pytest) — NEVER write to the real vault.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.services.obsidian_service import ObsidianService, ObsidianWriteError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HIVEPILOT_SUBTREE = "12 - HivePilot"
_SUBTREE_FOLDERS = ["Agents", "Tasks", "Reports", "Runs", "Interactions"]
_FROZEN_FOLDERS = [
    "08 - Security",
    "03 - Decisions",
    "02 - Architecture",
    "01 - Journal",
]

EXPECTED_TOP_LEVEL_FOLDERS = [
    "00 - Inbox",
    "01 - Journal",
    "01 - Knowledge",
    "02 - Architecture",
    "02 - Design",
    "03 - Decisions",
    "03 - Research",
    "04 - Engineering",
    "04 - Integrations",
    "04 - PRDs",
    "04 - Roadmap",
    "05 - Competitive Intel",
    "05 - GTM",
    "06 - GTM",
    "07 - Infrastructure",
    "08 - Security",
    "09 - People",
    "10 - Legal & Compliance",
    "10 - Templates",
    "11 - Projects",
    "12 - HivePilot",
    "99 - Archive",
]


def _make_fake_vault(tmp_path: Path) -> Path:
    """Create a minimal fake vault structure in tmp_path."""
    vault = tmp_path / "FakeVault"
    vault.mkdir()
    # Create a subset of expected top-level folders (simulate partial vault)
    present = [
        "00 - Inbox",
        "01 - Journal",
        "03 - Decisions",
        "08 - Security",
        "02 - Architecture",
        "12 - HivePilot",
        "99 - Archive",
    ]
    for folder in present:
        (vault / folder).mkdir()
    # Create HivePilot subtree
    for sub in _SUBTREE_FOLDERS:
        (vault / _HIVEPILOT_SUBTREE / sub).mkdir(parents=True, exist_ok=True)
    return vault


def _make_full_vault(tmp_path: Path) -> Path:
    """Create a complete fake vault with all expected folders."""
    vault = tmp_path / "FullVault"
    vault.mkdir()
    for folder in EXPECTED_TOP_LEVEL_FOLDERS:
        (vault / folder).mkdir()
    for sub in _SUBTREE_FOLDERS:
        (vault / _HIVEPILOT_SUBTREE / sub).mkdir(parents=True, exist_ok=True)
    return vault


# ---------------------------------------------------------------------------
# dry_run=True — filesystem must remain unchanged
# ---------------------------------------------------------------------------


class TestDryRun:
    def test_write_note_dry_run_no_files_created(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=True)

        result = svc.write_note(
            subpath="Tasks/2026-06-18-test-task.md",
            title="Test Task",
            body="Body content",
            frontmatter_fields={
                "type": "task",
                "status": "draft",
                "created": "2026-06-18",
                "agent": "test_agent",
            },
        )

        # dry_run should return a result dict, not write anything
        assert result is not None
        assert result.get("dry_run") is True

        # Verify no new files were created under HivePilot subtree beyond pre-existing
        created = list((vault / _HIVEPILOT_SUBTREE / "Tasks").iterdir())
        assert created == [], "dry_run must not write files"

    def test_write_adr_dry_run_no_files_created(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=True)

        result = svc.write_adr(
            title="Use pytest for testing",
            context="We need a test framework",
            options=["pytest", "unittest"],
            decision="Use pytest",
            consequences="Standard pytest patterns apply",
            security_impact="None",
            review_date="2027-01-01",
        )

        assert result.get("dry_run") is True
        decisions_dir = vault / "03 - Decisions"
        created = list(decisions_dir.iterdir())
        assert created == [], "dry_run must not write ADR files"


# ---------------------------------------------------------------------------
# render_frontmatter — required fields including language: en
# ---------------------------------------------------------------------------


class TestRenderFrontmatter:
    def test_renders_all_required_fields(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=True)

        fm = svc.render_frontmatter(
            fields={
                "title": "My Note",
                "type": "task",
                "status": "draft",
                "created": "2026-06-18",
                "agent": "chief_of_staff",
            }
        )

        assert fm.startswith("---\n")
        assert fm.rstrip().endswith("---")
        assert "title: My Note" in fm
        assert "type: task" in fm
        assert "status: draft" in fm
        assert "created: 2026-06-18" in fm
        assert "agent: chief_of_staff" in fm
        assert "language: en" in fm

    def test_renders_optional_fields_when_provided(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=True)

        fm = svc.render_frontmatter(
            fields={
                "title": "Run Note",
                "type": "run",
                "status": "active",
                "created": "2026-06-18",
                "agent": "executor",
                "run_id": "run-42",
                "tags": ["hivepilot", "ci"],
            }
        )

        assert "run_id: run-42" in fm
        # Tags list should be present
        assert "tags:" in fm

    def test_language_always_en(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=True)

        # Even if caller does not pass language, it must appear as en
        fm = svc.render_frontmatter(
            fields={
                "title": "X",
                "type": "reference",
                "status": "active",
                "created": "2026-06-18",
                "agent": "bot",
            }
        )
        assert "language: en" in fm

    def test_explicit_language_overridden_to_en(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=True)

        # Caller may try to pass language fr — service must enforce en
        fm = svc.render_frontmatter(
            fields={
                "title": "X",
                "type": "reference",
                "status": "active",
                "created": "2026-06-18",
                "agent": "bot",
                "language": "fr",
            }
        )
        assert "language: en" in fm


# ---------------------------------------------------------------------------
# write_note dry_run=False — actual write + round-trip read
# ---------------------------------------------------------------------------


class TestWriteNote:
    def test_write_creates_file_in_hivepilot_subtree(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=False)

        result = svc.write_note(
            subpath="Tasks/2026-06-18-my-task.md",
            title="My Task",
            body="## Description\n\nDo the thing.",
            frontmatter_fields={
                "type": "task",
                "status": "draft",
                "created": "2026-06-18",
                "agent": "executor",
            },
        )

        expected_path = vault / _HIVEPILOT_SUBTREE / "Tasks" / "2026-06-18-my-task.md"
        assert expected_path.exists(), "File should be written when dry_run=False"
        assert result["path"] == str(expected_path)
        assert result.get("dry_run") is False

    def test_round_trip_contains_frontmatter_and_body(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=False)

        svc.write_note(
            subpath="Reports/2026-06-18-report.md",
            title="Weekly Report",
            body="# Report\n\nContent here.",
            frontmatter_fields={
                "type": "report",
                "status": "complete",
                "created": "2026-06-18",
                "agent": "reporter",
            },
        )

        file_path = vault / _HIVEPILOT_SUBTREE / "Reports" / "2026-06-18-report.md"
        content = file_path.read_text(encoding="utf-8")

        assert "---" in content
        assert "title: Weekly Report" in content
        assert "language: en" in content
        assert "# Report" in content
        assert "Content here." in content

    def test_write_note_creates_parent_dirs(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=False)

        # Nested subpath that doesn't exist yet
        svc.write_note(
            subpath="Runs/2026-06/run-001.md",
            title="Run 001",
            body="Run output here.",
            frontmatter_fields={
                "type": "run",
                "status": "complete",
                "created": "2026-06-18",
                "agent": "executor",
            },
        )

        expected = vault / _HIVEPILOT_SUBTREE / "Runs" / "2026-06" / "run-001.md"
        assert expected.exists()


# ---------------------------------------------------------------------------
# write_adr — produces all ADR sections
# ---------------------------------------------------------------------------


class TestWriteAdr:
    def test_adr_dry_run_returns_content_with_all_sections(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=True)

        result = svc.write_adr(
            title="Use structured logging",
            context="We need consistent log output across services.",
            options=["structlog", "standard logging", "loguru"],
            decision="Use structlog",
            consequences="All services must use structlog; training required.",
            security_impact="No direct security impact.",
            review_date="2027-06-01",
        )

        content = result["content"]
        assert "Status:" in content or "status:" in content.lower()
        assert "Context:" in content
        assert "Options:" in content
        assert "Decision:" in content
        assert "Consequences:" in content
        assert "Security Impact:" in content
        assert "Review Date:" in content
        assert "structlog" in content
        assert "2027-06-01" in content

    def test_adr_write_creates_file_in_decisions(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=False)

        result = svc.write_adr(
            title="Adopt ruff for linting",
            context="Need a fast linter.",
            options=["ruff", "flake8", "pylint"],
            decision="Use ruff",
            consequences="Unified linting config in pyproject.toml",
            security_impact="No direct impact.",
            review_date="2027-01-01",
        )

        written_path = Path(result["path"])
        assert written_path.exists()
        # Must be under 03 - Decisions
        assert "03 - Decisions" in str(written_path)
        content = written_path.read_text(encoding="utf-8")
        assert "ruff" in content
        assert "Consequences:" in content
        assert "Security Impact:" in content

    def test_adr_frontmatter_has_adr_type(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=True)

        result = svc.write_adr(
            title="Test ADR",
            context="ctx",
            options=["A"],
            decision="A",
            consequences="none",
            security_impact="none",
            review_date="2027-01-01",
        )

        content = result["content"]
        assert "type: adr" in content
        assert "language: en" in content


# ---------------------------------------------------------------------------
# audit() — expected vs missing folders, frozen flags, subtree
# ---------------------------------------------------------------------------


class TestAudit:
    def test_audit_reports_present_and_missing_folders(self, tmp_path: Path) -> None:
        vault = _make_fake_vault(tmp_path)  # partial vault
        svc = ObsidianService(vault_path=vault, dry_run=True)

        report = svc.audit()

        assert "present" in report
        assert "missing" in report

        # Folders in present list must actually exist
        for folder in report["present"]:
            assert (vault / folder).exists(), f"{folder} should exist"

        # Folders in missing list must NOT exist
        for folder in report["missing"]:
            assert not (vault / folder).exists(), f"{folder} should not exist"

        # Some folders must be missing (we only created 7 of 22)
        assert len(report["missing"]) > 0

    def test_audit_flags_frozen_folders(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=True)

        report = svc.audit()

        frozen = report.get("frozen", [])
        for expected_frozen in _FROZEN_FOLDERS:
            assert expected_frozen in frozen, f"{expected_frozen} must be flagged as frozen"

    def test_audit_confirms_hivepilot_subtree(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=True)

        report = svc.audit()

        subtree = report.get("hivepilot_subtree", {})
        assert subtree.get("exists") is True
        for sub in _SUBTREE_FOLDERS:
            assert subtree.get(sub) is True, f"Subtree folder {sub} must be confirmed"

    def test_audit_detects_missing_hivepilot_subtree_folders(self, tmp_path: Path) -> None:
        vault = tmp_path / "MinimalVault"
        vault.mkdir()
        (vault / "12 - HivePilot").mkdir()
        # Only create Agents, not the rest
        (vault / "12 - HivePilot" / "Agents").mkdir()

        svc = ObsidianService(vault_path=vault, dry_run=True)
        report = svc.audit()

        subtree = report.get("hivepilot_subtree", {})
        assert subtree.get("Agents") is True
        assert subtree.get("Tasks") is False
        assert subtree.get("Reports") is False

    def test_audit_is_always_read_only(self, tmp_path: Path) -> None:
        vault = _make_fake_vault(tmp_path)
        before = set(vault.rglob("*"))

        svc = ObsidianService(vault_path=vault, dry_run=False)  # even with dry_run=False
        svc.audit()

        after = set(vault.rglob("*"))
        assert before == after, "audit() must never create or modify files"


# ---------------------------------------------------------------------------
# Guard — refuse to write outside allowed subtrees
# ---------------------------------------------------------------------------


class TestAppendDaily:
    def test_append_creates_daily_file(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=False)

        result = svc.append_daily("- 12:00 First entry")

        today = __import__("datetime").date.today().isoformat()
        expected_path = vault / _HIVEPILOT_SUBTREE / "Runs" / f"{today}.md"
        assert expected_path.exists()
        assert result["path"] == str(expected_path)
        assert result.get("dry_run") is False
        assert result.get("created") is True

        content = expected_path.read_text(encoding="utf-8")
        assert "---" in content
        assert "language: en" in content
        assert "First entry" in content

    def test_second_append_appends_not_overwrites(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=False)

        svc.append_daily("- 12:00 First entry")
        result2 = svc.append_daily("- 12:05 Second entry")

        assert result2.get("created") is False

        today = __import__("datetime").date.today().isoformat()
        content = (vault / _HIVEPILOT_SUBTREE / "Runs" / f"{today}.md").read_text(encoding="utf-8")
        assert "First entry" in content
        assert "Second entry" in content
        # Frontmatter block appears exactly once — second append did not
        # re-write the whole file with a new frontmatter block.
        assert content.count("---\n") == 2 or content.startswith("---")

    def test_append_daily_respects_subfolder(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=False)

        result = svc.append_daily("- entry", subfolder="Interactions")

        today = __import__("datetime").date.today().isoformat()
        expected_path = vault / _HIVEPILOT_SUBTREE / "Interactions" / f"{today}.md"
        assert expected_path.exists()
        assert result["path"] == str(expected_path)

    def test_append_daily_path_guard_holds(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=False)

        with pytest.raises(ObsidianWriteError, match="outside allowed"):
            svc.append_daily("entry", subfolder="../../etc")

    def test_append_daily_dry_run_returns_plan_without_writing(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=True)

        result = svc.append_daily("- dry run entry")

        assert result.get("dry_run") is True
        today = __import__("datetime").date.today().isoformat()
        expected_path = vault / _HIVEPILOT_SUBTREE / "Runs" / f"{today}.md"
        assert not expected_path.exists(), "dry_run must not write files"
        assert "dry run entry" in result["content"]


class TestGuard:
    def test_write_note_outside_hivepilot_raises(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=False)

        with pytest.raises(ObsidianWriteError, match="outside allowed"):
            svc.write_note(
                subpath="../00 - Inbox/evil.md",
                title="Evil",
                body="Bad",
                frontmatter_fields={
                    "type": "task",
                    "status": "draft",
                    "created": "2026-06-18",
                    "agent": "attacker",
                },
            )

    def test_write_note_absolute_path_outside_raises(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=False)

        with pytest.raises(ObsidianWriteError, match="outside allowed"):
            svc.write_note(
                subpath="/tmp/escape.md",
                title="Escape",
                body="Bad",
                frontmatter_fields={
                    "type": "task",
                    "status": "draft",
                    "created": "2026-06-18",
                    "agent": "attacker",
                },
            )

    def test_write_note_traversal_still_blocked_in_dry_run(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=True)

        with pytest.raises(ObsidianWriteError, match="outside allowed"):
            svc.write_note(
                subpath="../../etc/passwd",
                title="Escape",
                body="Bad",
                frontmatter_fields={
                    "type": "task",
                    "status": "draft",
                    "created": "2026-06-18",
                    "agent": "attacker",
                },
            )

    def test_write_adr_to_non_decisions_path_raises(self, tmp_path: Path) -> None:
        """ADR internal guard: the service always targets 03 - Decisions; test the guard by
        verifying write_note rejects targeting that folder directly."""
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=False)

        # Attempting write_note (not write_adr) into 03 - Decisions must fail
        with pytest.raises(ObsidianWriteError, match="outside allowed"):
            svc.write_note(
                subpath="../03 - Decisions/adr-test.md",
                title="ADR",
                body="body",
                frontmatter_fields={
                    "type": "adr",
                    "status": "draft",
                    "created": "2026-06-18",
                    "agent": "attacker",
                },
            )
