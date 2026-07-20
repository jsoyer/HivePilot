"""Autopilot-specific policy extensions.

Adds two fields to the per-project policy surface, read from the SAME
``policies.yaml`` the rest of the codebase already loads:

- ``auto_dispatch``: an explicit per-project allowlist of pipeline names the
  autopilot drain is permitted to dispatch unattended. Absent/empty/unlisted
  ⇒ disabled for that (project, pipeline) pair.
- ``budget_daily_usd``: a positive daily USD spend ceiling. Absent, ``None``,
  or ``<= 0`` ⇒ disabled (no budget configured means no auto-dispatch,
  never an unbounded budget).

This module deliberately does **not** modify ``hivepilot/services/policy_service.py``
or ``hivepilot/models.py`` (both are owned by parallel work) -- it reuses
``policy_service.load_policies()`` (an unmodified, already-public raw-dict
loader) as its only read-only building block from that module.
``require_approval`` is resolved locally in ``get_autopilot_policy`` (fail-
closed to ``True`` when absent) rather than via ``policy_service.get_policy``,
whose own default for that field is ``False`` -- see that function's
docstring for why reusing it here would be unsafe.

Disabled-by-default invariant: a project with no ``auto_dispatch`` block in
``policies.yaml`` resolves to an empty allowlist, which the gate in
``autopilot_queue.py`` always treats as "nothing may auto-dispatch" -- the
drain can still *propose* objectives, it just never promotes/dispatches them
without an explicit allowlist entry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from hivepilot.services import policy_service


@dataclass(frozen=True)
class AutopilotPolicy:
    """Resolved autopilot policy for a single project.

    ``require_approval`` defaults to ``True`` here (fail-closed) so that any
    future caller constructing this dataclass directly -- bypassing
    ``get_autopilot_policy`` -- can never accidentally end up with
    "approval not required" as the default.
    """

    auto_dispatch: list[str] = field(default_factory=list)
    require_approval: bool = True
    budget_daily_usd: float | None = None


def _raw_policies() -> dict:
    """Return the raw ``policies.yaml`` mapping, normalized past its
    top-level ``policies:`` key (mirrors ``policy_service``'s own internal
    cache-population normalization, without depending on its private
    ``_cache``/``_get_policies``)."""
    raw = policy_service.load_policies()
    return raw.get("policies", raw) or {}


def get_autopilot_policy(project_name: str) -> AutopilotPolicy:
    """Resolve the autopilot policy for *project_name*.

    Merges ``policies.default`` then ``policies.projects.<project_name>``
    (project overrides win) for ``auto_dispatch``/``budget_daily_usd`` --
    the same default/project merge order ``policy_service.get_policy`` uses.
    ``require_approval`` is resolved from that SAME merged project/default
    block directly (project value wins if present, else the default-block
    value, else ``True``) -- it deliberately does NOT reuse
    ``policy_service.get_policy``'s own ``require_approval`` (which defaults
    to ``False`` when the key is absent everywhere): reusing that default
    here would silently fail-open the autopilot gate for any project that
    configures ``auto_dispatch``/``budget_daily_usd`` but never explicitly
    sets ``require_approval``.

    Malformed values fail closed: a non-list ``auto_dispatch`` becomes an
    empty list; a non-numeric ``budget_daily_usd`` becomes ``None`` (both
    resolve to "disabled" in the gate, never to "allow everything").
    """
    policies = _raw_policies()
    default_block = policies.get("default") or {}
    project_block = (policies.get("projects") or {}).get(project_name) or {}
    merged = {**default_block, **project_block}

    auto_dispatch = merged.get("auto_dispatch") or []
    if not isinstance(auto_dispatch, list):
        auto_dispatch = []
    else:
        auto_dispatch = [str(item) for item in auto_dispatch]

    raw_budget = merged.get("budget_daily_usd")
    budget_daily_usd: float | None
    try:
        budget_daily_usd = float(raw_budget) if raw_budget is not None else None
    except (TypeError, ValueError):
        budget_daily_usd = None

    # Fail-closed contract (F1): require_approval must default to True when
    # absent from BOTH the project and default blocks -- policy_service's own
    # get_policy() defaults this to False for its own (lower-stakes) callers,
    # which would silently fail-open the autopilot gate if reused here. Project
    # value wins if explicitly set, else the default-block value, else True.
    if "require_approval" in project_block:
        require_approval = bool(project_block["require_approval"])
    elif "require_approval" in default_block:
        require_approval = bool(default_block["require_approval"])
    else:
        require_approval = True

    return AutopilotPolicy(
        auto_dispatch=auto_dispatch,
        require_approval=require_approval,
        budget_daily_usd=budget_daily_usd,
    )
