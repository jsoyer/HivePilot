"""Tests for the `hivepilot playbooks` CLI command group (Phase 16).

Covers:
- `playbooks list` shows all registered playbooks.
- `playbooks show <name>` renders flow summary / README / file listing;
  unknown name -> exit 1.
- `playbooks scaffold <name>` writes files; conflict without --force ->
  exit 1; --force overwrites; unknown name -> exit 1.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from hivepilot.cli import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# playbooks list
# ---------------------------------------------------------------------------


def test_playbooks_list_shows_all_playbooks() -> None:
    result = runner.invoke(app, ["playbooks", "list"])

    assert result.exit_code == 0, result.output
    assert "plan-build-review" in result.output
    assert "explore-synthesize" in result.output
    assert "propose-challenge-revise" in result.output


# ---------------------------------------------------------------------------
# playbooks show
# ---------------------------------------------------------------------------


def test_playbooks_show_renders_flow_summary_and_files() -> None:
    result = runner.invoke(app, ["playbooks", "show", "plan-build-review"])

    assert result.exit_code == 0, result.output
    assert "plan-build-review" in result.output
    assert "pipeline.yaml" in result.output
    assert "roles.yaml" in result.output
    assert "tasks.yaml" in result.output


def test_playbooks_show_unknown_name_exits_1() -> None:
    result = runner.invoke(app, ["playbooks", "show", "does-not-exist"])

    assert result.exit_code == 1
    assert "does-not-exist" in result.output or "Unknown" in result.output


# ---------------------------------------------------------------------------
# playbooks scaffold
# ---------------------------------------------------------------------------


def test_playbooks_scaffold_writes_files(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["playbooks", "scaffold", "propose-challenge-revise", "--target", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    base = tmp_path / "playbooks" / "propose-challenge-revise"
    assert (base / "pipeline.yaml").exists()
    assert (base / "roles.yaml").exists()
    assert (base / "tasks.yaml").exists()
    assert (base / "README.md").exists()


def test_playbooks_scaffold_unknown_name_exits_1(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["playbooks", "scaffold", "does-not-exist", "--target", str(tmp_path)]
    )

    assert result.exit_code == 1


def test_playbooks_scaffold_conflict_without_force_exits_1(tmp_path: Path) -> None:
    first = runner.invoke(
        app, ["playbooks", "scaffold", "explore-synthesize", "--target", str(tmp_path)]
    )
    assert first.exit_code == 0, first.output

    second = runner.invoke(
        app, ["playbooks", "scaffold", "explore-synthesize", "--target", str(tmp_path)]
    )
    assert second.exit_code == 1
    assert "--force" in second.output


def test_playbooks_scaffold_force_overwrites(tmp_path: Path) -> None:
    runner.invoke(app, ["playbooks", "scaffold", "explore-synthesize", "--target", str(tmp_path)])
    readme = tmp_path / "playbooks" / "explore-synthesize" / "README.md"
    readme.write_text("# sentinel\n")

    result = runner.invoke(
        app,
        [
            "playbooks",
            "scaffold",
            "explore-synthesize",
            "--target",
            str(tmp_path),
            "--force",
        ],
    )

    assert result.exit_code == 0, result.output
    assert readme.read_text() != "# sentinel\n"
