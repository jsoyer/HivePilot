"""Tests for `hivepilot.services.file_ownership` (Phase 16 C1 — detection layer).

This is a PURE detection/declaration layer: no orchestrator/git_service wiring
here (that gate is deferred to a follow-up sprint that owns those files). These
tests only cover:

1. `detect_conflicts` — glob matching (incl. `**`/`*`/`?`), same-role-owns-own-
   file is NOT a conflict, overlapping ownership, empty ownership -> no
   conflicts, deterministic sorted + deduplicated output.
2. `load_file_ownership` — valid file parses, absent file -> `{}`, malformed
   file (non-dict / non-list-of-str values) -> `ValueError` (fail-closed on
   bad config).
3. `format_conflicts_as_needs_human` — contains paths + role names, never file
   contents.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hivepilot.services.file_ownership import (
    Conflict,
    detect_conflicts,
    format_conflicts_as_needs_human,
    load_file_ownership,
)


class TestDetectConflicts:
    def test_other_role_changing_owned_file_is_a_conflict(self) -> None:
        ownership = {"backend": ["hivepilot/**"]}
        changes = {"docs": ["hivepilot/x.py"]}

        conflicts = detect_conflicts(ownership, changes)

        assert conflicts == [
            Conflict(path="hivepilot/x.py", owner_role="backend", offending_role="docs")
        ]

    def test_owning_role_changing_its_own_file_is_not_a_conflict(self) -> None:
        ownership = {"backend": ["hivepilot/**"]}
        changes = {"backend": ["hivepilot/x.py"]}

        assert detect_conflicts(ownership, changes) == []

    def test_unowned_file_is_not_a_conflict(self) -> None:
        ownership = {"backend": ["hivepilot/**"]}
        changes = {"docs": ["README.md"]}

        assert detect_conflicts(ownership, changes) == []

    def test_overlapping_ownership_both_flag_the_offending_role(self) -> None:
        ownership = {
            "backend": ["hivepilot/**"],
            "core": ["hivepilot/services/**"],
        }
        changes = {"docs": ["hivepilot/services/foo.py"]}

        conflicts = detect_conflicts(ownership, changes)

        assert conflicts == [
            Conflict(path="hivepilot/services/foo.py", owner_role="backend", offending_role="docs"),
            Conflict(path="hivepilot/services/foo.py", owner_role="core", offending_role="docs"),
        ]

    def test_double_star_matches_nested_paths(self) -> None:
        ownership = {"backend": ["hivepilot/**/*.py"]}
        changes = {"docs": ["hivepilot/services/deep/nested/foo.py"]}

        conflicts = detect_conflicts(ownership, changes)

        assert len(conflicts) == 1
        assert conflicts[0].path == "hivepilot/services/deep/nested/foo.py"

    def test_mid_pattern_double_star_matches_full_segments_only(self) -> None:
        """`src/**/config.yaml` must match zero-or-more FULL path segments
        between `src/` and `config.yaml` -- never a partial-segment prefix
        like `myconfig.yaml`. Regression for a bug where `**/` collapsed to a
        bare `.*` that ignored the left-hand `/` boundary."""
        ownership = {"backend": ["src/**/config.yaml"]}

        assert detect_conflicts(ownership, {"docs": ["src/config.yaml"]}) == [
            Conflict(path="src/config.yaml", owner_role="backend", offending_role="docs")
        ]
        assert detect_conflicts(ownership, {"docs": ["src/a/b/config.yaml"]}) == [
            Conflict(path="src/a/b/config.yaml", owner_role="backend", offending_role="docs")
        ]
        assert detect_conflicts(ownership, {"docs": ["src/myconfig.yaml"]}) == []

    def test_leading_double_star_matches_full_segments_only(self) -> None:
        """`**/CODEOWNERS` matches the file at the root and in any subdir,
        but never as a suffix of a differently-named file (`xCODEOWNERS`)."""
        ownership = {"backend": ["**/CODEOWNERS"]}

        assert detect_conflicts(ownership, {"docs": ["CODEOWNERS"]}) == [
            Conflict(path="CODEOWNERS", owner_role="backend", offending_role="docs")
        ]
        assert detect_conflicts(ownership, {"docs": ["a/b/CODEOWNERS"]}) == [
            Conflict(path="a/b/CODEOWNERS", owner_role="backend", offending_role="docs")
        ]
        assert detect_conflicts(ownership, {"docs": ["xCODEOWNERS"]}) == []

    def test_trailing_double_star_still_matches_everything_under_dir(self) -> None:
        """A trailing `**` (no following `/`, e.g. `hivepilot/**`) keeps the
        original bare `.*` semantics -- unaffected by the mid-pattern fix."""
        ownership = {"backend": ["hivepilot/**"]}

        conflicts = detect_conflicts(ownership, {"docs": ["hivepilot/a/b/c.py"]})

        assert len(conflicts) == 1

    def test_double_star_slash_star_still_matches_nested_files(self) -> None:
        """`hivepilot/**/*.py` must still match a `.py` file nested arbitrarily
        deep under `hivepilot/`."""
        ownership = {"backend": ["hivepilot/**/*.py"]}

        conflicts = detect_conflicts(ownership, {"docs": ["hivepilot/a/b/c.py"]})

        assert len(conflicts) == 1
        assert conflicts[0].path == "hivepilot/a/b/c.py"

        conflicts_shallow = detect_conflicts(ownership, {"docs": ["hivepilot/c.py"]})
        assert len(conflicts_shallow) == 1

    def test_single_star_does_not_cross_directory_boundary(self) -> None:
        ownership = {"backend": ["hivepilot/*.py"]}
        changes = {"docs": ["hivepilot/services/foo.py"]}

        assert detect_conflicts(ownership, changes) == []

    def test_question_mark_matches_single_character(self) -> None:
        ownership = {"backend": ["hivepilot/v?.py"]}
        changes = {"docs": ["hivepilot/v1.py"]}

        conflicts = detect_conflicts(ownership, changes)

        assert len(conflicts) == 1

    def test_question_mark_does_not_match_multiple_characters(self) -> None:
        ownership = {"backend": ["hivepilot/v?.py"]}
        changes = {"docs": ["hivepilot/v12.py"]}

        assert detect_conflicts(ownership, changes) == []

    def test_empty_ownership_yields_no_conflicts(self) -> None:
        assert detect_conflicts({}, {"docs": ["hivepilot/x.py"]}) == []

    def test_role_owning_nothing_yields_no_conflicts_for_that_role(self) -> None:
        ownership: dict[str, list[str]] = {"backend": []}
        changes = {"docs": ["hivepilot/x.py"]}

        assert detect_conflicts(ownership, changes) == []

    def test_output_is_sorted_and_deterministic(self) -> None:
        ownership = {
            "backend": ["hivepilot/**"],
            "core": ["hivepilot/**"],
        }
        changes = {
            "docs": ["hivepilot/z.py", "hivepilot/a.py"],
        }

        conflicts = detect_conflicts(ownership, changes)

        paths_owners = [(c.path, c.owner_role) for c in conflicts]
        assert paths_owners == sorted(paths_owners)

    def test_no_duplicate_conflicts_when_glob_lists_overlap(self) -> None:
        ownership = {"backend": ["hivepilot/**", "hivepilot/services/*.py"]}
        changes = {"docs": ["hivepilot/services/foo.py"]}

        conflicts = detect_conflicts(ownership, changes)

        assert conflicts == [
            Conflict(path="hivepilot/services/foo.py", owner_role="backend", offending_role="docs")
        ]

    def test_role_cannot_conflict_with_itself_even_with_multiple_globs(self) -> None:
        ownership = {"backend": ["hivepilot/**"]}
        changes = {"backend": ["hivepilot/services/foo.py", "hivepilot/x.py"]}

        assert detect_conflicts(ownership, changes) == []


class TestLoadFileOwnership:
    def test_valid_file_parses(self, tmp_path: Path) -> None:
        path = tmp_path / "ownership.yaml"
        path.write_text("backend:\n  - hivepilot/**\ndocs:\n  - docs/**\n", encoding="utf-8")

        result = load_file_ownership(path)

        assert result == {"backend": ["hivepilot/**"], "docs": ["docs/**"]}

    def test_absent_file_returns_empty_dict(self, tmp_path: Path) -> None:
        assert load_file_ownership(tmp_path / "does-not-exist.yaml") == {}

    def test_non_dict_top_level_raises_value_error(self, tmp_path: Path) -> None:
        path = tmp_path / "ownership.yaml"
        path.write_text("- backend\n- docs\n", encoding="utf-8")

        with pytest.raises(ValueError):
            load_file_ownership(path)

    def test_non_list_value_raises_value_error(self, tmp_path: Path) -> None:
        path = tmp_path / "ownership.yaml"
        path.write_text("backend: hivepilot/**\n", encoding="utf-8")

        with pytest.raises(ValueError):
            load_file_ownership(path)

    def test_non_string_glob_entry_raises_value_error(self, tmp_path: Path) -> None:
        path = tmp_path / "ownership.yaml"
        path.write_text("backend:\n  - 123\n", encoding="utf-8")

        with pytest.raises(ValueError):
            load_file_ownership(path)

    def test_empty_file_returns_empty_dict(self, tmp_path: Path) -> None:
        path = tmp_path / "ownership.yaml"
        path.write_text("", encoding="utf-8")

        assert load_file_ownership(path) == {}


class TestFormatConflictsAsNeedsHuman:
    def test_contains_paths_and_roles(self) -> None:
        conflicts = [
            Conflict(path="hivepilot/x.py", owner_role="backend", offending_role="docs"),
        ]

        message = format_conflicts_as_needs_human(conflicts)

        assert "hivepilot/x.py" in message
        assert "backend" in message
        assert "docs" in message

    def test_no_file_contents_leak(self) -> None:
        conflicts = [
            Conflict(path="hivepilot/secret.py", owner_role="backend", offending_role="docs"),
        ]

        message = format_conflicts_as_needs_human(conflicts)

        # Only path + role names are permitted in the message; assert the
        # message is a bounded summary, not something that could contain file
        # bytes (a crude proxy: it must stay short per-conflict).
        assert len(message) < 500

    def test_empty_conflicts_yields_clear_message(self) -> None:
        message = format_conflicts_as_needs_human([])

        assert "no" in message.lower() or "clean" in message.lower()
