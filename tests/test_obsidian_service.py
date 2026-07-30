"""
Tests for hivepilot.services.obsidian_service.

All tests use tmp_path (pytest) — NEVER write to the real vault.

Vault folder names are CONFIG-OWNED (`folders:` in vault.yaml); the autouse
`_pin_vault_layout` fixture in conftest.py installs a generic test taxonomy and
the constants below mirror it. No test in this module asserts a folder name the
engine could have hardcoded.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import conftest
import pytest

from hivepilot.services import config_provenance, vault_layout
from hivepilot.services.obsidian_service import ObsidianService, ObsidianWriteError
from hivepilot.services.vault_layout import (
    SLOT_ARTIFACTS,
    SLOT_DECISIONS,
    SLOT_HIVEPILOT,
    SLOT_SECURITY,
    VAULT_FOLDER_SLOTS,
    VaultLayout,
)


@pytest.fixture(autouse=True)
def _clean_secret_registry() -> Iterator[None]:
    config_provenance.clear_secret_values()
    yield
    config_provenance.clear_secret_values()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Folder names are CONFIG-OWNED (`folders:` in vault.yaml). These mirror the
# generic taxonomy the autouse `_pin_vault_layout` fixture in conftest.py
# installs — deliberately NOT any organisation's numbered filing scheme and NOT
# the engine's own default, so a name hardcoded back into the engine would fail
# against these values instead of quietly agreeing with them.
_HIVEPILOT_SUBTREE = conftest.TEST_VAULT_HIVEPILOT_FOLDER
_ARTIFACTS_FOLDER = conftest.TEST_VAULT_ARTIFACTS_FOLDER
_DECISIONS_FOLDER = conftest.TEST_VAULT_DECISIONS_FOLDER
_SECURITY_FOLDER = conftest.TEST_VAULT_SECURITY_FOLDER

# Engine-owned subfolders inside the engine's own subtree — not config.
_SUBTREE_FOLDERS = ["Agents", "Tasks", "Reports", "Runs", "Interactions"]
_FROZEN_FOLDERS = list(conftest.TEST_VAULT_FROZEN_FOLDERS)

# NOTE: the artifacts folder is deliberately NOT in this list even though the
# engine writes into it — several write_artifact tests build a vault from this
# list and then assert the folder is absent to prove dry_run created nothing.
# Keep it out. This is also the point of the two lists being independent: the
# expected LAYOUT need not mention a write target, and `audit()` reports write
# targets separately so it can still never be blind to one.
EXPECTED_TOP_LEVEL_FOLDERS = list(conftest.TEST_VAULT_EXPECTED_FOLDERS)


def _make_fake_vault(tmp_path: Path) -> Path:
    """Create a minimal fake vault structure in tmp_path."""
    vault = tmp_path / "FakeVault"
    vault.mkdir()
    # Create a subset of expected top-level folders (simulate partial vault)
    present = [
        "Inbox",
        "Journal",
        _DECISIONS_FOLDER,
        _SECURITY_FOLDER,
        "Architecture",
        _HIVEPILOT_SUBTREE,
        "Archive",
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
        decisions_dir = vault / _DECISIONS_FOLDER
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

    def test_write_note_redacts_registered_secret_in_body(self, tmp_path: Path) -> None:
        """A resolved ${secret:NAME} value echoed into a note body — e.g. the
        commits_vault documentation changelog note built directly from
        stage_output — must never reach the file written to the vault."""
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=False)
        marker = "OBSIDIAN-MARKER-do-not-leak"
        config_provenance.register_secret_value(marker)

        result = svc.write_note(
            subpath="Docs/changelog-run-1.md",
            title="Documentation update",
            body=f"echoed {marker}",
            frontmatter_fields={"type": "documentation"},
        )

        file_path = vault / _HIVEPILOT_SUBTREE / "Docs" / "changelog-run-1.md"
        written = file_path.read_text(encoding="utf-8")
        assert marker not in written
        assert config_provenance.REDACTED in written
        assert marker not in result["content"]
        assert config_provenance.REDACTED in result["content"]

    def test_write_note_dry_run_preview_also_redacted(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=True)
        marker = "OBSIDIAN-DRYRUN-MARKER-do-not-leak"
        config_provenance.register_secret_value(marker)

        result = svc.write_note(
            subpath="Docs/preview.md",
            title="Preview",
            body=f"echoed {marker}",
            frontmatter_fields={"type": "documentation"},
        )

        assert marker not in result["content"]
        assert config_provenance.REDACTED in result["content"]

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
        # Must be under the configured decisions folder
        assert _DECISIONS_FOLDER in str(written_path)
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

        # Some folders must be missing (we only created 7 of the declared list)
        assert len(report["missing"]) > 0

    def test_audit_reflects_the_configured_expected_folders(self, tmp_path: Path) -> None:
        """The expected layout comes from config, not from engine code."""
        vault = tmp_path / "CustomVault"
        (vault / "OnlyThisOne").mkdir(parents=True)
        layout = VaultLayout(
            folders={SLOT_HIVEPILOT: _HIVEPILOT_SUBTREE},
            expected_folders=("OnlyThisOne", "NotThere"),
            frozen_folders=(),
        )
        report = ObsidianService(vault_path=vault, dry_run=True, layout=layout).audit()

        assert report["present"] == ["OnlyThisOne"]
        assert report["missing"] == ["NotThere"]
        assert report["expected_examined"] == 2

    def test_audit_examining_nothing_is_never_reported_as_clean(self, tmp_path: Path) -> None:
        """THE trap: an operator who declared no expected layout must not get an
        audit that silently checks zero folders and looks like a pass.

        `expected_examined` is the load-bearing field — `present`/`missing` are
        both empty here, which on their own read as "nothing wrong".
        """
        vault = tmp_path / "EmptyVault"
        vault.mkdir()
        layout = VaultLayout(folders={}, expected_folders=(), frozen_folders=())
        report = ObsidianService(vault_path=vault, dry_run=True, layout=layout).audit()

        assert report["present"] == []
        assert report["missing"] == []
        assert report["expected_examined"] == 0

    def test_audit_reports_engine_folders_independently_of_the_expected_list(
        self, tmp_path: Path
    ) -> None:
        """`audit()` must never be blind to a folder the engine itself writes.

        Regression guard for the conflation bug: the artifacts folder — the one
        folder the engine writes deliverables into — was absent from the
        expected-layout list for several releases, so the audit reported a
        complete-looking vault while the engine wrote somewhere the operator was
        never told about.

        The fix is NOT to union the two lists. `engine_folders` is derived from
        the slot vocabulary itself, so every write target is reported even when
        the operator's `expected_folders:` mentions none of them — which is the
        case constructed here.
        """
        vault = tmp_path / "Vault"
        vault.mkdir()
        layout = VaultLayout(
            folders={
                SLOT_HIVEPILOT: _HIVEPILOT_SUBTREE,
                SLOT_ARTIFACTS: _ARTIFACTS_FOLDER,
                SLOT_DECISIONS: _DECISIONS_FOLDER,
                SLOT_SECURITY: _SECURITY_FOLDER,
            },
            # Deliberately mentions NOT ONE engine folder.
            expected_folders=("SomethingElse",),
            frozen_folders=(),
        )
        report = ObsidianService(vault_path=vault, dry_run=True, layout=layout).audit()

        assert set(report["engine_folders"]) == set(VAULT_FOLDER_SLOTS)
        for slot in VAULT_FOLDER_SLOTS:
            info = report["engine_folders"][slot]
            assert info["configured"] is True
            assert info["exists"] is False  # nothing was created in this vault
        assert report["engine_folders"][SLOT_ARTIFACTS]["folder"] == _ARTIFACTS_FOLDER
        # ...and the operator's declared layout is untouched by any of it.
        assert report["present"] == []
        assert report["missing"] == ["SomethingElse"]

    def test_audit_distinguishes_write_slots_from_the_read_slot(self, tmp_path: Path) -> None:
        """The old single list flattened away which folders HivePilot WRITES."""
        vault = tmp_path / "Vault"
        vault.mkdir()
        report = ObsidianService(vault_path=vault, dry_run=True).audit()
        access = {slot: info["access"] for slot, info in report["engine_folders"].items()}

        assert access[SLOT_ARTIFACTS] == vault_layout.ACCESS_WRITE
        assert access[SLOT_DECISIONS] == vault_layout.ACCESS_WRITE
        assert access[SLOT_HIVEPILOT] == vault_layout.ACCESS_WRITE
        assert access[SLOT_SECURITY] == vault_layout.ACCESS_READ

    def test_audit_marks_an_unconfigured_slot_as_unconfigured_not_missing(
        self, tmp_path: Path
    ) -> None:
        """`exists: False` on an unconfigured slot must not be readable as "the
        folder is not there" — there is no folder to look for."""
        vault = tmp_path / "Vault"
        vault.mkdir()
        layout = VaultLayout(folders={}, expected_folders=(), frozen_folders=())
        report = ObsidianService(vault_path=vault, dry_run=True, layout=layout).audit()

        for slot in VAULT_FOLDER_SLOTS:
            info = report["engine_folders"][slot]
            assert info["configured"] is False
            assert info["exists"] is False
            assert info["folder"] == ""

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
        (vault / _HIVEPILOT_SUBTREE).mkdir()
        # Only create Agents, not the rest
        (vault / _HIVEPILOT_SUBTREE / "Agents").mkdir()

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
        # Frontmatter block appears exactly once (open + close = two `---`
        # lines) — second append did not re-write the file with a fresh
        # frontmatter block.
        assert content.count("---\n") == 2

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


class TestWriteArtifact:
    def test_write_creates_file_under_role_subfolder(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=False)

        today = __import__("datetime").date.today().isoformat()
        result = svc.write_artifact(
            role="cto",
            slug="run43-cto-technical-spec",
            title="Technical Spec — Run 43",
            body="## Technical Spec\n\nDeliverable body.",
            frontmatter_fields={
                "type": "artifact",
                "status": "complete",
                "created": today,
                "agent": "hivepilot",
                "run_id": 43,
                "stage": "CTO Review",
                "role": "cto",
            },
        )

        expected_path = vault / _ARTIFACTS_FOLDER / "cto" / f"{today}-run43-cto-technical-spec.md"
        assert expected_path.exists()
        assert result["path"] == str(expected_path)
        assert result.get("dry_run") is False
        content = expected_path.read_text(encoding="utf-8")
        assert "type: artifact" in content
        assert "role: cto" in content
        assert "Deliverable body." in content

    def test_write_artifact_dry_run_no_file_created(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=True)

        result = svc.write_artifact(
            role="pm",
            slug="run1-pm-brief",
            title="PM Brief",
            body="Brief body.",
            frontmatter_fields={"type": "artifact", "status": "complete"},
        )

        assert result.get("dry_run") is True
        assert not (vault / _ARTIFACTS_FOLDER).exists()

    def test_write_artifact_redacts_secret(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=False)
        marker = "ARTIFACT-SERVICE-MARKER-do-not-leak"
        config_provenance.register_secret_value(marker)

        result = svc.write_artifact(
            role="cto",
            slug="run2-cto-spec",
            title="Spec",
            body=f"echoed {marker}",
            frontmatter_fields={"type": "artifact"},
        )

        written = Path(result["path"]).read_text(encoding="utf-8")
        assert marker not in written
        assert config_provenance.REDACTED in written

    def test_write_artifact_role_traversal_is_contained(self, tmp_path: Path) -> None:
        """A role string with path-traversal characters must not escape
        the artifacts folder — the role is slugified defensively."""
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=False)

        result = svc.write_artifact(
            role="../../etc",
            slug="evil",
            title="Evil",
            body="bad",
            frontmatter_fields={"type": "artifact"},
        )

        written_path = Path(result["path"])
        artifacts_root = (vault / _ARTIFACTS_FOLDER).resolve()
        written_path.relative_to(artifacts_root)  # raises ValueError if escaped


class TestGuard:
    def test_write_note_outside_hivepilot_raises(self, tmp_path: Path) -> None:
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=False)

        with pytest.raises(ObsidianWriteError, match="outside allowed"):
            svc.write_note(
                subpath="../Inbox/evil.md",
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
        """ADR internal guard: the service always targets the decisions folder; test the guard by
        verifying write_note rejects targeting that folder directly."""
        vault = _make_full_vault(tmp_path)
        svc = ObsidianService(vault_path=vault, dry_run=False)

        # Attempting write_note (not write_adr) into the decisions folder must fail
        with pytest.raises(ObsidianWriteError, match="outside allowed"):
            svc.write_note(
                subpath=f"../{_DECISIONS_FOLDER}/adr-test.md",
                title="ADR",
                body="body",
                frontmatter_fields={
                    "type": "adr",
                    "status": "draft",
                    "created": "2026-06-18",
                    "agent": "attacker",
                },
            )


# ---------------------------------------------------------------------------
# Write targets come from config, and an unconfigured slot REFUSES
# ---------------------------------------------------------------------------


class TestWriteTargetsAreConfigOwned:
    """Every write path must build its root from the configured folder name."""

    @staticmethod
    def _layout(**folders: str) -> VaultLayout:
        return VaultLayout(folders=folders, expected_folders=(), frozen_folders=())

    def test_write_note_uses_the_configured_hivepilot_folder(self, tmp_path: Path) -> None:
        vault = tmp_path / "V"
        vault.mkdir()
        layout = self._layout(hivepilot="Custom Engine Home")
        svc = ObsidianService(vault_path=vault, dry_run=False, layout=layout)

        result = svc.write_note(
            subpath="Tasks/note.md",
            title="T",
            body="b",
            frontmatter_fields={"type": "task"},
        )

        assert result["path"] == str(vault / "Custom Engine Home" / "Tasks" / "note.md")
        assert (vault / "Custom Engine Home" / "Tasks" / "note.md").is_file()

    def test_write_adr_uses_the_configured_decisions_folder(self, tmp_path: Path) -> None:
        vault = tmp_path / "V"
        vault.mkdir()
        layout = self._layout(decisions="Choices")
        svc = ObsidianService(vault_path=vault, dry_run=False, layout=layout)

        result = svc.write_adr(
            title="Pick a thing",
            context="c",
            options=["a"],
            decision="a",
            consequences="k",
            security_impact="none",
            review_date="2027-01-01",
        )

        assert str(vault / "Choices") in result["path"]
        assert list((vault / "Choices").iterdir())

    def test_write_artifact_uses_the_configured_artifacts_folder(self, tmp_path: Path) -> None:
        vault = tmp_path / "V"
        vault.mkdir()
        layout = self._layout(artifacts="Outputs")
        svc = ObsidianService(vault_path=vault, dry_run=False, layout=layout)

        result = svc.write_artifact(
            role="cto",
            slug="spec",
            title="Spec",
            body="body",
            frontmatter_fields={"type": "artifact"},
        )

        assert str(vault / "Outputs" / "cto") in result["path"]

    def test_append_daily_uses_the_configured_hivepilot_folder(self, tmp_path: Path) -> None:
        vault = tmp_path / "V"
        vault.mkdir()
        layout = self._layout(hivepilot="Custom Engine Home")
        svc = ObsidianService(vault_path=vault, dry_run=False, layout=layout)

        result = svc.append_daily("- entry")

        assert str(vault / "Custom Engine Home" / "Runs") in result["path"]


class TestUnconfiguredSlotRefusesInsteadOfWritingToTheVaultRoot:
    """THE trap, at the write path.

    An absent folder name must not degrade into `<vault>/`, `<vault>/None`, or a
    folder the engine invented. `_emit` calls `mkdir(parents=True)`, so a guess
    would not be a harmless miss — it would create a new top-level folder in what
    is typically a synced git repo.
    """

    EMPTY = VaultLayout(folders={}, expected_folders=(), frozen_folders=())

    def _svc(self, tmp_path: Path) -> ObsidianService:
        vault = tmp_path / "V"
        vault.mkdir()
        return ObsidianService(vault_path=vault, dry_run=False, layout=self.EMPTY)

    def test_write_note_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(ObsidianWriteError, match="not configured"):
            self._svc(tmp_path).write_note(
                subpath="Tasks/n.md", title="T", body="b", frontmatter_fields={}
            )

    def test_write_adr_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(ObsidianWriteError, match="not configured"):
            self._svc(tmp_path).write_adr(
                title="T",
                context="c",
                options=[],
                decision="d",
                consequences="k",
                security_impact="s",
                review_date="2027-01-01",
            )

    def test_write_artifact_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(ObsidianWriteError, match="not configured"):
            self._svc(tmp_path).write_artifact(
                role="cto", slug="s", title="T", body="b", frontmatter_fields={}
            )

    def test_append_daily_refuses(self, tmp_path: Path) -> None:
        with pytest.raises(ObsidianWriteError, match="not configured"):
            self._svc(tmp_path).append_daily("- entry")

    def test_refusal_names_the_slot_and_the_config_key(self, tmp_path: Path) -> None:
        """A refusal an operator cannot act on is only half a guard."""
        with pytest.raises(ObsidianWriteError) as exc:
            self._svc(tmp_path).write_artifact(
                role="cto", slug="s", title="T", body="b", frontmatter_fields={}
            )
        message = str(exc.value)
        assert SLOT_ARTIFACTS in message
        assert vault_layout.VAULT_FOLDERS_KEY in message

    def test_nothing_is_created_anywhere_in_the_vault(self, tmp_path: Path) -> None:
        vault = tmp_path / "V"
        vault.mkdir()
        svc = ObsidianService(vault_path=vault, dry_run=False, layout=self.EMPTY)
        before = set(vault.rglob("*"))

        for call in (
            lambda: svc.write_note(subpath="a.md", title="T", body="b", frontmatter_fields={}),
            lambda: svc.write_artifact(
                role="cto", slug="s", title="T", body="b", frontmatter_fields={}
            ),
            lambda: svc.append_daily("- e"),
        ):
            with pytest.raises(ObsidianWriteError):
                call()

        assert set(vault.rglob("*")) == before
        assert list(vault.iterdir()) == []

    def test_a_dry_run_also_refuses_rather_than_previewing_a_root_path(
        self, tmp_path: Path
    ) -> None:
        """A dry-run preview of `<vault>/Tasks/n.md` would be a lie an operator
        could act on — the refusal must not depend on dry_run."""
        vault = tmp_path / "V"
        vault.mkdir()
        svc = ObsidianService(vault_path=vault, dry_run=True, layout=self.EMPTY)
        with pytest.raises(ObsidianWriteError, match="not configured"):
            svc.write_note(subpath="Tasks/n.md", title="T", body="b", frontmatter_fields={})


class TestMalformedSlotValuesNeverReachAWrite:
    """A blank or escaping folder name must never become a writable root.

    The loader already drops such values, but `VaultLayout` is public and
    injectable, and the downstream `_resolve_safe` guard is NOT enough on its
    own: it checks containment relative to the ALLOWED ROOT, so a layout
    carrying ".." makes the vault's PARENT the allowed root and every write
    "safely" lands outside the vault. Hence validation at construction.
    """

    @pytest.mark.parametrize("bad", ["", "   ", "..", ".", "sub/dir", "../escape", "back\\slash"])
    def test_constructing_a_layout_with_a_bad_name_is_refused(self, bad: str) -> None:
        with pytest.raises(vault_layout.VaultLayoutError):
            VaultLayout(folders={SLOT_ARTIFACTS: bad}, expected_folders=(), frozen_folders=())

    @pytest.mark.parametrize("bad", ["..", "../escape", "sub/dir", ""])
    def test_a_bad_audit_list_entry_is_refused(self, bad: str) -> None:
        """`(vault / "..").is_dir()` is always True — an escaping expected-layout
        entry would report a folder "present" that is not even in the vault."""
        with pytest.raises(vault_layout.VaultLayoutError):
            VaultLayout(folders={}, expected_folders=(bad,), frozen_folders=())
        with pytest.raises(vault_layout.VaultLayoutError):
            VaultLayout(folders={}, expected_folders=(), frozen_folders=(bad,))

    def test_an_unknown_slot_key_is_refused(self) -> None:
        with pytest.raises(vault_layout.VaultLayoutError, match="unknown vault folder slot"):
            VaultLayout(folders={"artefacts": "Typo"}, expected_folders=(), frozen_folders=())

    def test_no_escaping_layout_can_ever_reach_the_filesystem(self, tmp_path: Path) -> None:
        """End-to-end: because construction is refused, there is no way to get an
        ObsidianService whose allowed root is outside the vault."""
        vault = tmp_path / "V"
        vault.mkdir()
        sibling = tmp_path / "escape"
        sibling.mkdir()

        with pytest.raises(vault_layout.VaultLayoutError):
            layout = VaultLayout(
                folders={SLOT_ARTIFACTS: "../escape"}, expected_folders=(), frozen_folders=()
            )
            ObsidianService(vault_path=vault, dry_run=False, layout=layout).write_artifact(
                role="cto", slug="s", title="T", body="b", frontmatter_fields={}
            )

        assert list(sibling.iterdir()) == []
        assert list(vault.iterdir()) == []
