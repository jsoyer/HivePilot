"""Obsidian vault routing table (HP-121).

Maps ``project_id`` / ``tenant`` → a named vault path. This is engine config,
not a plugin: every writer still resolves through
``obsidian_vault_resolver``.

When the table is **inactive** (file missing, or no ``by_project`` /
``by_tenant`` routes) this module returns ``None`` and the resolver keeps
pre-HP-121 behaviour.

When the table is **active**, lookups are fail-closed: unmapped and
ambiguous results raise ``VaultRouteError``. The global
``HIVEPILOT_OBSIDIAN_VAULT`` is never used as a silent fallback.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from hivepilot.config import settings
from hivepilot.models import NamedVault, VaultRoutesFile


class VaultRouteError(RuntimeError):
    """The mapping table cannot choose a vault.

    Raised instead of falling back to the global Obsidian path: an unmapped
    or ambiguous (project vs tenant) lookup must never silently cross-write.
    """


def load_vault_routes(path: Path | None = None) -> VaultRoutesFile:
    """Load ``vault_routes.yaml`` through the standard config-path chain.

    A missing file is an empty, inactive table — not an error.
    """
    resolved = settings.resolve_config_path(path or settings.vault_routes_file)
    if not resolved.exists():
        return VaultRoutesFile()
    with resolved.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise VaultRouteError(
            f"{resolved} must be a mapping of vaults / by_project / by_tenant, "
            f"not {type(raw).__name__}"
        )
    return VaultRoutesFile.model_validate(raw)


def _env_path_for_vault(vault_id: str) -> Path | None:
    """``HIVEPILOT_VAULT_<ID>`` for the two canonical ids only.

    Other vault ids must declare ``path`` in the table. No invented Noxys
    filesystem default.
    """
    if vault_id == "jsoyer":
        return settings.vault_jsoyer
    if vault_id == "noxys":
        return settings.vault_noxys
    return None


def named_vault_path(vault_id: str, spec: NamedVault) -> Path:
    """Resolve a named vault to an existing absolute directory.

    Order: ``spec.path``, then ``HIVEPILOT_VAULT_<ID>`` for ``jsoyer`` /
    ``noxys``. Never creates the directory.
    """
    candidate = spec.path if spec.path is not None else _env_path_for_vault(vault_id)
    if candidate is None:
        hint = (
            f"set vaults.{vault_id}.path or HIVEPILOT_VAULT_{vault_id.upper()}"
            if vault_id in {"jsoyer", "noxys"}
            else f"set vaults.{vault_id}.path"
        )
        raise VaultRouteError(
            f"Named vault {vault_id!r} has no path ({hint}). "
            "HivePilot does not invent a checkout location."
        )
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        raise VaultRouteError(
            f"Named vault {vault_id!r} path {str(candidate)!r} is not absolute "
            "(cwd-silo). Use an absolute path or one starting with '~/'"
        )
    if not path.is_dir():
        raise VaultRouteError(
            f"Named vault {vault_id!r} points at {path}, which is not an "
            "existing directory. HivePilot never creates a vault directory."
        )
    return path.resolve()


def resolve_route(
    *,
    project_id: str | None = None,
    tenant: str | None = None,
    override: Path | None = None,
    routes: VaultRoutesFile | None = None,
) -> Path | None:
    """Return the routed vault, or ``None`` if the table is inactive.

    Raises
    ------
    VaultRouteError
        Unmapped, ambiguous, or an unusable named-vault path.
    """
    table = routes if routes is not None else load_vault_routes()
    if not table.is_active():
        return None

    project_key = project_id.strip() if project_id and project_id.strip() else None
    tenant_key = tenant.strip() if tenant and tenant.strip() else None

    project_vault_id = table.by_project.get(project_key) if project_key else None
    tenant_vault_id = table.by_tenant.get(tenant_key) if tenant_key else None

    if (
        project_vault_id
        and tenant_vault_id
        and project_vault_id != tenant_vault_id
    ):
        raise VaultRouteError(
            f"Ambiguous vault route: project_id {project_key!r} maps to "
            f"{project_vault_id!r} but tenant {tenant_key!r} maps to "
            f"{tenant_vault_id!r}. Align the mapping table or run the "
            "project under its own tenant — HivePilot will not pick a winner."
        )

    vault_id = project_vault_id or tenant_vault_id
    table_path = named_vault_path(vault_id, table.vaults[vault_id]) if vault_id else None

    candidates: dict[str, list[str]] = {}
    if table_path is not None:
        source = (
            f"by_project[{project_key!r}]"
            if project_vault_id
            else f"by_tenant[{tenant_key!r}]"
        )
        if project_vault_id and tenant_vault_id:
            source = (
                f"by_project[{project_key!r}]+by_tenant[{tenant_key!r}]"
            )
        candidates.setdefault(str(table_path), []).append(source)
    if override is not None:
        override_path = Path(override).expanduser().resolve()
        candidates.setdefault(str(override_path), []).append("project.obsidian_vault")

    if not candidates:
        raise VaultRouteError(
            "Unmapped vault route: "
            f"project_id={project_key!r} tenant={tenant_key!r} is not in "
            "vault_routes.yaml and the project declares no obsidian_vault:. "
            "The global HIVEPILOT_OBSIDIAN_VAULT is not used while the "
            "mapping table is active (fail-closed)."
        )
    if len(candidates) > 1:
        detail = "; ".join(
            f"{path} <- {', '.join(sources)}"
            for path, sources in sorted(candidates.items())
        )
        raise VaultRouteError(
            "Ambiguous vault route: mapping table and project override "
            f"disagree: {detail}. HivePilot will not pick a winner."
        )

    chosen = Path(next(iter(candidates)))
    if not chosen.is_dir():
        raise VaultRouteError(
            f"Routed vault {chosen} is not an existing directory. "
            "HivePilot never creates a vault directory."
        )
    return chosen
