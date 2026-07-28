"""Per-project Obsidian vault resolution (per-project-vault PRD).

``Settings.obsidian_vault`` is a single GLOBAL path, so every project's
HivePilot artifacts land in the SAME vault. HivePilot is a generic engine and
the unit of configuration is a *pipeline*: several pipelines commonly coexist
on ONE host (the operator's own HivePilot work vs. a product pipeline). A
setting that is per-deployment global but conceptually belongs to a pipeline
is an architectural bug, not a missing feature -- the test being "would two
pipelines on one machine need different values?".

This module is the ONE place that answers "which vault does this project
write to (and read back from)". Every vault consumer resolves through it so
the write direction and the ``obsidian`` plugin's ``recall`` read direction
can never drift apart.

Resolution order
----------------
1. ``ProjectConfig.obsidian_vault`` -- the per-project override.
2. ``Settings.obsidian_vault`` -- the global default, unchanged.

Semantics (deliberate, fail-closed)
-----------------------------------
absent / ``None``
    Inherit the global setting. Byte-identical to before this field existed --
    every pre-existing deployment is unaffected until someone opts in.
empty string / whitespace-only
    **Rejected at config-load time** (``ProjectConfig`` validator), NOT
    silently treated as "use the global". An empty value on a routing
    decision is always a config mistake (a typo, or an unexpanded
    ``${VAULT}`` template) and treating it as "no constraint" would silently
    route a project's artifacts back into the shared vault the operator was
    explicitly moving away from.
relative path
    **Rejected at config-load time.** A relative vault path resolves against
    whatever cwd the daemon happens to have -- exactly the cwd-silo bug class
    that produced three divergent ``state.db`` files on the operator's box.
    ``~`` is expanded first, so ``~/vault`` is fine.
set, absolute, but the directory does not exist
    **Loud failure** (``VaultResolutionError``) at resolve time. HivePilot
    never creates the vault directory itself: a silently created directory is
    how artifacts end up somewhere nobody looks. Note this is stricter than
    the GLOBAL setting, whose long-standing lenient behaviour ("vault absent
    -> vault writes are silent no-ops") is deliberately preserved -- only an
    EXPLICIT per-project override fails loudly.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from hivepilot.models import ProjectConfig


class VaultResolutionError(RuntimeError):
    """A configured Obsidian vault destination cannot be used.

    Raised instead of silently degrading to a different vault (or to no vault
    at all): an operator who explicitly routed a project's artifacts somewhere
    must never have them written elsewhere, or dropped, without being told.
    """


class _UseSettings:
    """Sentinel type for the ``global_vault`` parameter.

    Distinguishes "caller did not supply a fallback -- read the global
    setting" from "caller supplied ``None``, meaning there is no global
    vault". A plain ``None`` default could not express both.
    """


USE_SETTINGS = _UseSettings()
"""Default ``global_vault`` argument: read `Settings.obsidian_vault` directly.

Call sites that already compute their own global-vault expression (and whose
tests patch `settings` in *their* module namespace) pass that value in
explicitly instead, so this module never changes their existing behaviour.
"""


def _global_vault() -> Path | None:
    """The global ``Settings.obsidian_vault``, or ``None`` when it is unset or
    absent on disk.

    Imported lazily so a settings change between calls is honoured (the same
    lazy-``settings`` discipline ``plugins/obsidian.py`` uses) and so importing
    this module has no config side effects. The existence check is
    intentionally byte-identical to the expression every call site used before
    this module existed (``settings.obsidian_vault if
    settings.obsidian_vault.exists() else None``) -- widening it would change
    behaviour for deployments that never opt in to an override.
    """
    from hivepilot.config import settings

    vault = settings.obsidian_vault
    if not vault:
        return None
    return vault if Path(vault).exists() else None


def project_vault_override(project: ProjectConfig | None) -> Path | None:
    """Return *project*'s explicit vault override, or ``None`` when it has none.

    Raises
    ------
    VaultResolutionError
        When the override is set but does not point at an existing directory.
        The directory is NEVER created here.
    """
    if project is None:
        return None
    override = getattr(project, "obsidian_vault", None)
    if override is None:
        return None

    # Already validated absolute + non-empty at config-load time
    # (`ProjectConfig.validate_obsidian_vault`); re-derived here rather than
    # trusted blindly so a directly-constructed ProjectConfig can't bypass it.
    path = Path(override).expanduser()
    if not path.is_absolute():
        raise VaultResolutionError(
            f"Project vault override {str(override)!r} is not an absolute path. "
            "A relative vault path resolves against the daemon's current working "
            "directory, which silently sends artifacts to a different place per "
            "invocation -- declare an absolute path (or one starting with '~/')."
        )
    if not path.is_dir():
        raise VaultResolutionError(
            f"Project vault override points at {path}, which is not an existing "
            "directory. HivePilot never creates a vault directory itself (a "
            "silently created vault is how artifacts end up somewhere nobody "
            "looks) -- create it, or remove the `obsidian_vault:` override to "
            "fall back to the global HIVEPILOT_OBSIDIAN_VAULT."
        )
    return path.resolve()


def resolve_vault_path(
    project: ProjectConfig | None,
    global_vault: Path | None | _UseSettings = USE_SETTINGS,
) -> Path | None:
    """Return the vault root *project*'s artifacts belong in.

    ``None`` means "no vault configured" -- vault writes are silent no-ops,
    the pre-existing behaviour for a deployment whose global vault is unset or
    absent.

    *global_vault* is the fallback used when *project* declares no override.
    Left at :data:`USE_SETTINGS` it is read from `Settings.obsidian_vault`;
    call sites that already compute their own global expression pass it in so
    this function is a pure addition on top of their existing behaviour.

    Raises
    ------
    VaultResolutionError
        When *project* declares an override that cannot be used.
    """
    override = project_vault_override(project)
    if override is not None:
        return override
    if isinstance(global_vault, _UseSettings):
        return _global_vault()
    return global_vault


def resolve_prompt_vault(settings_obj: Any, project: ProjectConfig | None) -> str:
    """Return the ``{OBSIDIAN_VAULT}`` prompt-variable value for *project*.

    Agents are told where "the vault" is via this substitution
    (``hivepilot.utils.prompt_vars.render_prompt_vars``). Once the destination
    is per-project, telling every agent the GLOBAL path would point a
    product-pipeline agent at the operator's personal vault.

    Returns ``""`` (the pre-existing "no vault configured" value) rather than
    raising: rendering a prompt must not crash a run, and an empty value is
    the honest signal. Unlike a wrong path, it cannot mislead an agent. The
    same misconfiguration fails loudly up front in
    ``Orchestrator._run_pipeline_body``.

    *settings_obj* is passed in (rather than read from `hivepilot.config`)
    because runners hold their own `self.settings` reference, which tests
    substitute.
    """
    from hivepilot.utils.logging import get_logger

    try:
        override = project_vault_override(project)
    except VaultResolutionError as exc:
        get_logger(__name__).warning("vault.prompt_var_unresolvable", error=str(exc))
        return ""
    if override is not None:
        return str(override)
    global_vault = getattr(settings_obj, "obsidian_vault", None)
    return str(global_vault) if global_vault else ""


def _normalise(path: Path | None) -> str | None:
    return None if path is None else str(Path(path).expanduser().resolve())


def resolve_vault_for_projects(
    projects: Iterable[ProjectConfig],
    global_vault: Path | None | _UseSettings = USE_SETTINGS,
) -> Path | None:
    """Return the single vault root a run over *projects* writes to.

    A pipeline run writes ONE aggregated stage artifact per stage (see
    ``Orchestrator._run_pipeline_body`` -> ``pipelines.write_stage_artifact``),
    so a run has exactly one vault destination. When the run's target projects
    resolve to DIFFERENT vaults this fails closed rather than picking one:
    silently choosing the first project's vault would cross-write another
    project's work into it, which is precisely the outcome a per-project vault
    exists to prevent.

    No existing deployment can hit this: with no override anywhere, every
    project resolves to the same global vault.

    Raises
    ------
    VaultResolutionError
        On a divergent or unusable destination.
    """
    projects = list(projects)
    if not projects:
        return _global_vault() if isinstance(global_vault, _UseSettings) else global_vault

    by_vault: dict[str | None, list[str]] = {}
    first: Path | None = None
    for project in projects:
        resolved = resolve_vault_path(project, global_vault)
        if not by_vault:
            first = resolved
        by_vault.setdefault(_normalise(resolved), []).append(project.path.name)

    if len(by_vault) > 1:
        detail = "; ".join(
            f"{vault or '<no vault>'} <- {', '.join(sorted(names))}"
            for vault, names in sorted(by_vault.items(), key=lambda kv: str(kv[0]))
        )
        raise VaultResolutionError(
            "This run's target projects resolve to different Obsidian vaults, but a "
            f"run writes one aggregated artifact per stage: {detail}. Give every "
            "project in the run the same `obsidian_vault:` value in projects.yaml, "
            "or run them as separate pipelines."
        )

    return first
