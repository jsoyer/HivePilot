"""HP-121: project_id / tenant → named vault, fail-closed, isolation.

The mapping table is the CoS routing decision. While it is active, an
unmapped or ambiguous lookup must never inherit HIVEPILOT_OBSIDIAN_VAULT.
Jsoyer work and Noxys work must not land in each other's vault.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from hivepilot.models import NamedVault, ProjectConfig, VaultRoutesFile
from hivepilot.services.obsidian_vault_resolver import (
    VaultResolutionError,
    resolve_vault_for_projects,
    resolve_vault_path,
)
from hivepilot.services.vault_routes import (
    VaultRouteError,
    load_vault_routes,
    named_vault_path,
    resolve_route,
)


def _vault(tmp_path: Path, name: str) -> Path:
    vault = tmp_path / name
    vault.mkdir()
    return vault.resolve()


def _project(tmp_path: Path, name: str = "proj", **kwargs) -> ProjectConfig:
    repo = tmp_path / name
    repo.mkdir(parents=True, exist_ok=True)
    return ProjectConfig(path=repo, **kwargs)


def _table(jsoyer: Path, noxys: Path) -> VaultRoutesFile:
    return VaultRoutesFile(
        vaults={
            "jsoyer": NamedVault(
                repo="https://github.com/jsoyer/obsidian-vault", path=jsoyer
            ),
            "noxys": NamedVault(path=noxys),
        },
        by_project={"hivepilot": "jsoyer", "noxys": "noxys"},
        by_tenant={"jsoyer": "jsoyer", "noxys": "noxys"},
    )


# ---------------------------------------------------------------------------
# Load-time table validation
# ---------------------------------------------------------------------------


class TestVaultRoutesFile:
    def test_empty_table_is_inactive(self) -> None:
        assert VaultRoutesFile().is_active() is False

    def test_shipped_example_parses_and_names_canonical_vaults(self) -> None:
        raw = yaml.safe_load(
            Path("examples/vault_routes.yaml").read_text(encoding="utf-8")
        )
        table = VaultRoutesFile.model_validate(raw)
        assert table.is_active()
        assert table.vaults["jsoyer"].repo == "https://github.com/jsoyer/obsidian-vault"
        assert "noxys" in table.vaults
        assert table.by_project["hivepilot"] == "jsoyer"
        assert table.by_tenant["noxys"] == "noxys"

    def test_routes_without_vaults_refuse_to_load(self) -> None:
        with pytest.raises(ValidationError, match="vaults"):
            VaultRoutesFile(by_project={"hivepilot": "jsoyer"})

    def test_unknown_vault_id_refuses_to_load(self, tmp_path: Path) -> None:
        vault = _vault(tmp_path, "jsoyer")
        with pytest.raises(ValidationError, match="unknown"):
            VaultRoutesFile(
                vaults={"jsoyer": NamedVault(path=vault)},
                by_project={"hivepilot": "missing"},
            )

    def test_empty_route_value_is_rejected(self, tmp_path: Path) -> None:
        vault = _vault(tmp_path, "jsoyer")
        with pytest.raises(ValidationError, match="empty"):
            VaultRoutesFile(
                vaults={"jsoyer": NamedVault(path=vault)},
                by_project={"hivepilot": "  "},
            )

    def test_relative_named_path_is_rejected_at_load(self) -> None:
        with pytest.raises(ValidationError, match="absolute"):
            NamedVault(path="obsidian-vault")

    def test_empty_named_path_is_rejected_at_load(self) -> None:
        with pytest.raises(ValidationError, match="empty"):
            NamedVault(path="   ")

    def test_missing_file_loads_as_inactive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hivepilot.config as config_mod

        monkeypatch.setattr(
            config_mod.settings, "vault_routes_file", tmp_path / "absent.yaml"
        )
        monkeypatch.setattr(config_mod.settings, "base_dir", tmp_path)
        monkeypatch.setattr(
            config_mod.settings, "xdg_config_home", tmp_path / "xdg"
        )
        assert load_vault_routes(tmp_path / "absent.yaml").is_active() is False


# ---------------------------------------------------------------------------
# Fail-closed routing
# ---------------------------------------------------------------------------


class TestResolveRouteFailClosed:
    def test_inactive_table_returns_none_so_legacy_fallback_may_run(
        self, tmp_path: Path
    ) -> None:
        assert (
            resolve_route(project_id="hivepilot", routes=VaultRoutesFile()) is None
        )

    def test_unmapped_project_fails_and_does_not_use_global(
        self, tmp_path: Path
    ) -> None:
        jsoyer = _vault(tmp_path, "jsoyer")
        noxys = _vault(tmp_path, "noxys")
        table = _table(jsoyer, noxys)
        with pytest.raises(VaultRouteError, match="Unmapped"):
            resolve_route(project_id="example-api", tenant="default", routes=table)

    def test_ambiguous_project_vs_tenant_fails(self, tmp_path: Path) -> None:
        table = _table(_vault(tmp_path, "jsoyer"), _vault(tmp_path, "noxys"))
        with pytest.raises(VaultRouteError, match="Ambiguous"):
            resolve_route(project_id="hivepilot", tenant="noxys", routes=table)

    def test_override_disagreeing_with_table_is_ambiguous(self, tmp_path: Path) -> None:
        jsoyer = _vault(tmp_path, "jsoyer")
        noxys = _vault(tmp_path, "noxys")
        other = _vault(tmp_path, "other")
        table = _table(jsoyer, noxys)
        with pytest.raises(VaultRouteError, match="Ambiguous"):
            resolve_route(project_id="hivepilot", override=other, routes=table)

    def test_named_vault_without_path_fails_closed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hivepilot.config as config_mod

        monkeypatch.setattr(config_mod.settings, "vault_jsoyer", None, raising=False)
        table = VaultRoutesFile(
            vaults={"jsoyer": NamedVault(repo="https://github.com/jsoyer/obsidian-vault")},
            by_project={"hivepilot": "jsoyer"},
        )
        with pytest.raises(VaultRouteError, match="has no path"):
            resolve_route(project_id="hivepilot", routes=table)

    def test_named_vault_missing_directory_is_not_created(self, tmp_path: Path) -> None:
        missing = tmp_path / "not-created"
        table = VaultRoutesFile(
            vaults={"jsoyer": NamedVault(path=missing)},
            by_project={"hivepilot": "jsoyer"},
        )
        with pytest.raises(VaultRouteError, match="not an existing directory"):
            named_vault_path("jsoyer", table.vaults["jsoyer"])
        assert not missing.exists()


# ---------------------------------------------------------------------------
# Multi-tenant isolation
# ---------------------------------------------------------------------------


class TestMultiTenantIsolation:
    def test_hivepilot_project_writes_to_jsoyer_not_noxys(self, tmp_path: Path) -> None:
        jsoyer = _vault(tmp_path, "jsoyer")
        noxys = _vault(tmp_path, "noxys")
        table = _table(jsoyer, noxys)
        project = _project(tmp_path, "hivepilot")
        assert (
            resolve_vault_path(
                project, tmp_path / "global", project_id="hivepilot", routes=table
            )
            == jsoyer
        )

    def test_noxys_project_writes_to_noxys_not_jsoyer(self, tmp_path: Path) -> None:
        jsoyer = _vault(tmp_path, "jsoyer")
        noxys = _vault(tmp_path, "noxys")
        table = _table(jsoyer, noxys)
        project = _project(tmp_path, "noxys")
        assert (
            resolve_vault_path(
                project, tmp_path / "global", project_id="noxys", tenant="noxys", routes=table
            )
            == noxys
        )

    def test_tenant_jsoyer_does_not_resolve_to_noxys_vault(self, tmp_path: Path) -> None:
        jsoyer = _vault(tmp_path, "jsoyer")
        noxys = _vault(tmp_path, "noxys")
        table = _table(jsoyer, noxys)
        assert (
            resolve_vault_path(
                _project(tmp_path, "misc"),
                tmp_path / "global",
                tenant="jsoyer",
                routes=table,
            )
            == jsoyer
        )

    def test_unmapped_does_not_fall_back_to_global_via_resolver(
        self, tmp_path: Path
    ) -> None:
        global_vault = _vault(tmp_path, "global")
        table = _table(_vault(tmp_path, "jsoyer"), _vault(tmp_path, "noxys"))
        with pytest.raises(VaultResolutionError, match="Unmapped"):
            resolve_vault_path(
                _project(tmp_path, "example-api"),
                global_vault,
                project_id="example-api",
                routes=table,
            )

    def test_two_tenants_in_one_run_fail_closed(self, tmp_path: Path) -> None:
        jsoyer = _vault(tmp_path, "jsoyer")
        noxys = _vault(tmp_path, "noxys")
        table = _table(jsoyer, noxys)
        projects = {
            "hivepilot": _project(tmp_path, "hivepilot"),
            "noxys": _project(tmp_path, "noxys"),
        }
        with pytest.raises(VaultResolutionError):
            resolve_vault_for_projects(projects, tmp_path / "global", routes=table)

    def test_same_vault_on_both_keys_is_not_ambiguous(self, tmp_path: Path) -> None:
        jsoyer = _vault(tmp_path, "jsoyer")
        noxys = _vault(tmp_path, "noxys")
        table = _table(jsoyer, noxys)
        assert (
            resolve_vault_path(
                _project(tmp_path, "hivepilot"),
                tmp_path / "global",
                project_id="hivepilot",
                tenant="jsoyer",
                routes=table,
            )
            == jsoyer
        )

    def test_inactive_table_still_inherits_global(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OSS / single-vault deployments without the file stay unchanged."""
        import hivepilot.config as config_mod

        global_vault = _vault(tmp_path, "global")
        monkeypatch.setattr(
            config_mod.settings, "obsidian_vault", global_vault, raising=False
        )
        assert (
            resolve_vault_path(
                _project(tmp_path, "example-api"),
                global_vault,
                project_id="example-api",
                routes=VaultRoutesFile(),
            )
            == global_vault
        )

    def test_env_path_for_jsoyer_when_yaml_omits_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hivepilot.config as config_mod

        jsoyer = _vault(tmp_path, "jsoyer")
        monkeypatch.setattr(config_mod.settings, "vault_jsoyer", jsoyer, raising=False)
        table = VaultRoutesFile(
            vaults={"jsoyer": NamedVault(repo="https://github.com/jsoyer/obsidian-vault")},
            by_project={"hivepilot": "jsoyer"},
        )
        assert resolve_route(project_id="hivepilot", routes=table) == jsoyer
