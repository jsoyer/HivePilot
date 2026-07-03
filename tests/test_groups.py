"""Component groups (E1): a product (acme) expands to its component repos."""

from __future__ import annotations

from typer.testing import CliRunner

from hivepilot.cli import app
from hivepilot.services.project_service import load_groups, resolve_targets


def test_load_groups_has_acme() -> None:
    g = load_groups().groups
    assert "acme" in g
    assert g["acme"].hub == "acme-api"
    assert "acme-web" in g["acme"].components
    assert "acme-worker" in g["acme"].components


def test_resolve_targets_group_expands() -> None:
    targets = resolve_targets("acme")
    assert "acme-web" in targets
    assert "acme-worker" in targets
    assert len(targets) == 2  # the acme group fans out to its two components


def test_resolve_targets_plain_project_passthrough() -> None:
    assert resolve_targets("acme-api") == ["acme-api"]


def test_resolve_targets_unknown_passthrough() -> None:
    assert resolve_targets("totally-unknown") == ["totally-unknown"]


def test_groups_command_lists_acme() -> None:
    result = CliRunner().invoke(app, ["groups"])
    assert result.exit_code == 0
    assert "acme" in result.output
    assert "components" in result.output
