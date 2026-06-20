"""Component groups (E1): a product (noxys) expands to its component repos."""

from __future__ import annotations

from typer.testing import CliRunner

from hivepilot.cli import app
from hivepilot.services.project_service import load_groups, resolve_targets


def test_load_groups_has_noxys() -> None:
    g = load_groups().groups
    assert "noxys" in g
    assert g["noxys"].hub == "noxys"
    assert "noxys-api" in g["noxys"].components
    assert "noxys-website" in g["noxys"].components


def test_resolve_targets_group_expands() -> None:
    targets = resolve_targets("noxys")
    assert "noxys-api" in targets
    assert len(targets) > 5  # a group fans out to many components


def test_resolve_targets_plain_project_passthrough() -> None:
    assert resolve_targets("noxys-api") == ["noxys-api"]


def test_resolve_targets_unknown_passthrough() -> None:
    assert resolve_targets("totally-unknown") == ["totally-unknown"]


def test_groups_command_lists_noxys() -> None:
    result = CliRunner().invoke(app, ["groups"])
    assert result.exit_code == 0
    assert "noxys" in result.output
    assert "components" in result.output
