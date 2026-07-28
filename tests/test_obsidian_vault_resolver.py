"""
Tests for `hivepilot.services.obsidian_vault_resolver` — per-project Obsidian
vault destination (per-project-vault PRD).

Named `obsidian_vault_resolver` (not `vault_resolver`) to avoid colliding with
`tests/test_vault_resolver.py`, which covers the HashiCorp **Vault** secrets
backend — an entirely different "vault".

Why this exists: `Settings.obsidian_vault` is a single GLOBAL path, so every
project's HivePilot artifacts land in the SAME vault. HivePilot is a generic
engine and several pipelines commonly coexist on ONE host (the operator's own
HivePilot work vs. a product pipeline), so the vault destination conceptually
belongs to the project/pipeline, not to the deployment.

Contract under test:
- `ProjectConfig.obsidian_vault` (absent/`None`) -> inherit the global setting,
  byte-identical to before this field existed.
- An override must be ABSOLUTE after `~` expansion. A relative path is the
  cwd-silo bug class (three divergent `state.db` files on the operator's box)
  and is rejected at CONFIG LOAD time, loudly.
- An EMPTY / whitespace-only override is rejected at load time too — it must
  never silently degrade to "use the global vault" (fail-closed: an empty
  value on a routing decision means reject, never "no constraint").
- A resolvable-but-absent vault directory is a LOUD failure at resolve time.
  HivePilot never creates the vault directory itself — a silently created
  directory is how artifacts end up somewhere nobody looks.
- A run whose target projects resolve to DIFFERENT vaults fails closed: one
  run writes one aggregated stage artifact, so silently picking one project's
  vault would cross-write another project's work into it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

import hivepilot.config as config_mod
from hivepilot.models import ProjectConfig
from hivepilot.services.obsidian_vault_resolver import (
    VaultResolutionError,
    project_vault_override,
    resolve_vault_for_projects,
    resolve_vault_path,
)


def _project(tmp_path: Path, name: str = "proj", **kwargs) -> ProjectConfig:
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    return ProjectConfig(path=repo, **kwargs)


# ---------------------------------------------------------------------------
# ProjectConfig.obsidian_vault — load-time validation
# ---------------------------------------------------------------------------


class TestProjectConfigVaultField:
    def test_absent_override_defaults_to_none(self, tmp_path: Path) -> None:
        """No `obsidian_vault:` key -> `None` -> inherit the global setting."""
        project = _project(tmp_path)
        assert project.obsidian_vault is None

    def test_explicit_null_is_treated_as_absent(self, tmp_path: Path) -> None:
        project = _project(tmp_path, obsidian_vault=None)
        assert project.obsidian_vault is None

    def test_absolute_override_is_kept_and_resolved(self, tmp_path: Path) -> None:
        vault = tmp_path / "personal-vault"
        vault.mkdir()
        project = _project(tmp_path, obsidian_vault=str(vault))
        assert project.obsidian_vault == vault.resolve()
        assert project.obsidian_vault.is_absolute()

    def test_tilde_override_is_expanded_to_an_absolute_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        project = _project(tmp_path, obsidian_vault="~/my-vault")
        assert project.obsidian_vault is not None
        assert project.obsidian_vault.is_absolute()
        assert "~" not in str(project.obsidian_vault)

    def test_relative_override_is_rejected_at_load(self, tmp_path: Path) -> None:
        """The cwd-silo bug class: a relative vault path resolves against
        whatever cwd the daemon happens to have. Reject loudly, at load."""
        with pytest.raises(ValidationError) as excinfo:
            _project(tmp_path, obsidian_vault="obsidian-vault")
        assert "absolute" in str(excinfo.value).lower()

    def test_dot_relative_override_is_rejected_at_load(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            _project(tmp_path, obsidian_vault="../vaults/personal")

    def test_empty_string_override_is_rejected_not_silently_global(self, tmp_path: Path) -> None:
        """Fail-closed: an empty override is ALWAYS a config mistake (typo, or
        an unexpanded `${VAULT}` template). Treating it as "use the global"
        would silently route a project's artifacts back into the shared vault
        the operator was explicitly moving away from."""
        with pytest.raises(ValidationError) as excinfo:
            _project(tmp_path, obsidian_vault="")
        assert "empty" in str(excinfo.value).lower()

    def test_whitespace_only_override_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValidationError):
            _project(tmp_path, obsidian_vault="   ")


# ---------------------------------------------------------------------------
# project_vault_override / resolve_vault_path
# ---------------------------------------------------------------------------


class TestProjectVaultOverride:
    def test_returns_none_when_project_has_no_override(self, tmp_path: Path) -> None:
        assert project_vault_override(_project(tmp_path)) is None

    def test_returns_none_for_none_project(self) -> None:
        assert project_vault_override(None) is None

    def test_returns_the_override_when_the_directory_exists(self, tmp_path: Path) -> None:
        vault = tmp_path / "vault"
        vault.mkdir()
        project = _project(tmp_path, obsidian_vault=str(vault))
        assert project_vault_override(project) == vault.resolve()

    def test_missing_override_directory_fails_loudly_and_is_not_created(
        self, tmp_path: Path
    ) -> None:
        missing = tmp_path / "nope"
        project = _project(tmp_path, obsidian_vault=str(missing))
        with pytest.raises(VaultResolutionError) as excinfo:
            project_vault_override(project)
        assert str(missing) in str(excinfo.value)
        assert not missing.exists(), "the vault directory must never be auto-created"

    def test_override_pointing_at_a_file_fails_loudly(self, tmp_path: Path) -> None:
        not_a_dir = tmp_path / "vault.md"
        not_a_dir.write_text("x")
        project = _project(tmp_path, obsidian_vault=str(not_a_dir))
        with pytest.raises(VaultResolutionError):
            project_vault_override(project)


class TestResolveVaultPath:
    def test_project_with_override_resolves_to_that_vault(self, tmp_path: Path) -> None:
        vault = tmp_path / "personal"
        vault.mkdir()
        project = _project(tmp_path, obsidian_vault=str(vault))
        assert resolve_vault_path(project) == vault.resolve()

    def test_project_without_override_falls_back_to_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REGRESSION GUARD: every existing deployment (no override anywhere)
        must keep resolving to the global setting, unchanged."""
        global_vault = tmp_path / "global"
        global_vault.mkdir()
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", global_vault, raising=False)
        assert resolve_vault_path(_project(tmp_path)) == global_vault

    def test_none_project_falls_back_to_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        global_vault = tmp_path / "global"
        global_vault.mkdir()
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", global_vault, raising=False)
        assert resolve_vault_path(None) == global_vault

    def test_missing_global_vault_still_returns_none_silently(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REGRESSION GUARD: the global setting's lenient "absent vault ->
        writes are silent no-ops" behaviour is deliberately UNCHANGED. Only an
        EXPLICIT per-project override fails loudly."""
        monkeypatch.setattr(
            config_mod.settings, "obsidian_vault", tmp_path / "absent", raising=False
        )
        assert resolve_vault_path(_project(tmp_path)) is None

    def test_override_wins_over_a_present_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        global_vault = tmp_path / "global"
        global_vault.mkdir()
        override = tmp_path / "personal"
        override.mkdir()
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", global_vault, raising=False)
        project = _project(tmp_path, obsidian_vault=str(override))
        assert resolve_vault_path(project) == override.resolve()


# ---------------------------------------------------------------------------
# resolve_vault_for_projects — one run writes ONE aggregated artifact
# ---------------------------------------------------------------------------


class TestResolveVaultForProjects:
    def test_no_overrides_resolves_to_the_global_vault(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        global_vault = tmp_path / "global"
        global_vault.mkdir()
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", global_vault, raising=False)
        projects = [_project(tmp_path, "a"), _project(tmp_path, "b")]
        assert resolve_vault_for_projects(projects) == global_vault

    def test_empty_project_list_resolves_to_the_global_vault(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        global_vault = tmp_path / "global"
        global_vault.mkdir()
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", global_vault, raising=False)
        assert resolve_vault_for_projects([]) == global_vault

    def test_identical_overrides_resolve_to_that_vault(self, tmp_path: Path) -> None:
        vault = tmp_path / "shared"
        vault.mkdir()
        projects = [
            _project(tmp_path, "a", obsidian_vault=str(vault)),
            _project(tmp_path, "b", obsidian_vault=str(vault)),
        ]
        assert resolve_vault_for_projects(projects) == vault.resolve()

    def test_divergent_overrides_fail_closed(self, tmp_path: Path) -> None:
        """A run writes ONE aggregated stage artifact. Silently picking the
        first project's vault would cross-write the other project's work."""
        personal = tmp_path / "personal"
        personal.mkdir()
        product = tmp_path / "product"
        product.mkdir()
        projects = [
            _project(tmp_path, "hivepilot", obsidian_vault=str(personal)),
            _project(tmp_path, "noxys", obsidian_vault=str(product)),
        ]
        with pytest.raises(VaultResolutionError) as excinfo:
            resolve_vault_for_projects(projects)
        message = str(excinfo.value)
        assert "hivepilot" in message and "noxys" in message

    def test_override_diverging_from_the_global_fallback_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        global_vault = tmp_path / "global"
        global_vault.mkdir()
        personal = tmp_path / "personal"
        personal.mkdir()
        monkeypatch.setattr(config_mod.settings, "obsidian_vault", global_vault, raising=False)
        projects = [
            _project(tmp_path, "a", obsidian_vault=str(personal)),
            _project(tmp_path, "b"),
        ]
        with pytest.raises(VaultResolutionError):
            resolve_vault_for_projects(projects)
