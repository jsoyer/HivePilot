"""Autopilot-specific policy extensions.

Adds five fields to the per-project policy surface, read from the SAME
``policies.yaml`` the rest of the codebase already loads:

- ``auto_dispatch``: an explicit per-project allowlist of pipeline names the
  autopilot drain is permitted to dispatch unattended. Absent/empty/unlisted
  ⇒ disabled for that (project, pipeline) pair.
- ``budget_daily_usd``: a positive daily USD spend ceiling. Absent, ``None``,
  or ``<= 0`` ⇒ disabled (no budget configured means no auto-dispatch,
  never an unbounded budget).
- ``outward_actions`` (propose→ratify→dispatch PRD, spec §6): an explicit
  per-project ALLOWLIST of outward-action tokens (see
  ``autopilot_queue.OUTWARD_ACTIONS``) a ratified partition may ever be
  consented to perform for this project. **Absent or empty ⇒ nothing
  outward may ever be consented to** — a ticked consent checkbox can never
  authorize what policy never allowed. This is deliberately spelled out
  because "empty value read as 'no constraint'" is a documented recurring
  fail-open bug class in this repo.
- ``max_partition_cost_usd`` (spec §5.3): a positive per-partition USD
  admission ceiling. Absent/``None``/``<= 0`` ⇒ no partition may be
  ratified for this project (never an unbounded ceiling).
- ``max_task_wall_clock_seconds`` (spec §5.3): a positive per-task wall
  clock ceiling. Absent/``None``/``<= 0`` ⇒ no partition may be ratified
  for this project (never an unbounded ceiling).

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

    The three partition fields default to the most restrictive possible
    value for the same reason: an empty ``outward_actions`` allowlist means
    "nothing outward", and a ``None`` ceiling means "no partition may be
    ratified", never "unbounded".
    """

    auto_dispatch: list[str] = field(default_factory=list)
    require_approval: bool = True
    budget_daily_usd: float | None = None
    outward_actions: list[str] = field(default_factory=list)
    max_partition_cost_usd: float | None = None
    max_task_wall_clock_seconds: int | None = None


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

    Malformed values fail closed: a non-list ``auto_dispatch``/
    ``outward_actions`` becomes an empty list; a non-numeric
    ``budget_daily_usd``/``max_partition_cost_usd``/
    ``max_task_wall_clock_seconds`` becomes ``None`` (all of which resolve
    to "disabled"/"deny" in their respective gates, never to "allow
    everything").
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

    # Propose->ratify->dispatch PRD (spec §6): an ALLOWLIST, never a
    # denylist. A missing key, an explicit `outward_actions: []`, a non-list
    # value, and a list whose entries aren't strings ALL resolve to the empty
    # allowlist -- which `partition_service` treats as "nothing outward may
    # ever be consented to for this project", never as "unconstrained".
    raw_outward = merged.get("outward_actions")
    if isinstance(raw_outward, list):
        outward_actions = [str(item) for item in raw_outward if isinstance(item, str) and item]
    else:
        outward_actions = []

    return AutopilotPolicy(
        auto_dispatch=auto_dispatch,
        require_approval=require_approval,
        budget_daily_usd=budget_daily_usd,
        outward_actions=outward_actions,
        max_partition_cost_usd=_positive_float(merged.get("max_partition_cost_usd")),
        max_task_wall_clock_seconds=_positive_int(merged.get("max_task_wall_clock_seconds")),
    )


def _positive_float(raw: object) -> float | None:
    """Coerce *raw* to a strictly-positive float, or ``None``.

    Fail-closed: absent, ``None``, non-numeric, ``bool``, zero and negative
    all resolve to ``None`` (which every consuming gate treats as "deny"),
    never to an unbounded ceiling.
    """
    # `bool` is excluded explicitly because it is an `int` subclass in
    # Python: without this, `max_partition_cost_usd: true` would silently
    # resolve to a $1.00 ceiling instead of being rejected as malformed.
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw)
        except ValueError:
            return None
    else:
        return None
    return value if value > 0 else None


def _positive_int(raw: object) -> int | None:
    """Coerce *raw* to a strictly-positive int, or ``None`` -- same
    fail-closed contract as ``_positive_float``. A fractional value is
    truncated, so anything under 1 second resolves to ``None`` (deny)
    rather than to a zero-second, unenforceable ceiling."""
    value = _positive_float(raw)
    if value is None:
        return None
    truncated = int(value)
    return truncated if truncated > 0 else None
