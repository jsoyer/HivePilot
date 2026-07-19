"""Built-in `plugins` graph source (Mirador Graph View PRD, Sprint 1).

Renders the SAME data `plugins list`/`GET /v1/plugins/health` already
expose — loaded plugins, the runner kinds/notifiers/secrets backends/
panels/skills/hooks each contributes, plus role -> runner bindings — as a
graph instead of a flat table. Secret-consumption edges (which plugin reads
which secret NAME) are explicitly OUT of scope for Sprint 1.

No-secret discipline: a `secret` node/detail exposes the secret's NAME and
`enabled`/`disabled` status only — never a resolved value. Nothing in this
module ever calls a `SecretsBackend.resolve(...)`.
"""

from __future__ import annotations

from typing import Any, Mapping

from hivepilot.graph import (
    GraphContext,
    GraphData,
    GraphDetail,
    GraphEdge,
    GraphNode,
    GraphSourceSpec,
)
from hivepilot.plugins import PanelTableSection, PanelTextSection

_PLUGIN_PREFIX = "plugin:"
_ROLE_PREFIX = "role:"
_RUNNER_PREFIX = "runner:"
_NOTIFIER_PREFIX = "notifier:"
_SECRET_PREFIX = "secret:"
_PANEL_PREFIX = "panel:"
_SKILL_PREFIX = "skill:"
_HOOK_PREFIX = "hook:"


def _get_plugin_manager() -> Any:
    """Resolve the SAME live `PluginManager` instance the API server itself
    uses (`api_service._get_orchestrator().plugins`), imported HERE at CALL
    time rather than at this module's own top level.

    Why lazy: `hivepilot/services/api_service.py` imports
    `hivepilot.graph_sources` at module scope (to trigger this package's
    built-in graph-source registration — see
    `hivepilot/graph_sources/__init__.py`). A top-level import of
    `api_service` back from this module would therefore be a circular
    import (api_service -> graph_sources -> plugins_source -> api_service,
    the last hop hitting a partially-initialized module). Deferring the
    import to call time (well after the app has finished importing)
    resolves the already-fully-initialized module from `sys.modules`.

    Why the SAME instance, not a fresh `PluginManager()`: `hivepilot/
    plugins.py`'s `_scan_local_plugins` re-execs every local-file plugin's
    source EVERY scan (its own hot-reload-driven design — see that
    function's docstring), producing brand-new class objects each time.
    Constructing a second `PluginManager()` in the same process would try
    to re-register those new objects into the already-populated,
    process-global `RUNNER_MAP`/`NOTIFIER_MAP`/`SECRETS_MAP`, colliding
    with the first instance's own registration (`RunnerKindCollisionError`
    et al. — identity, not equality, is what `_stage_kind` compares).
    Reusing the live singleton sidesteps that entirely.
    """
    from hivepilot.services.api_service import _get_orchestrator

    return _get_orchestrator().plugins


def _plugin_node_id(name: str) -> str:
    return f"{_PLUGIN_PREFIX}{name}"


def _role_node_id(name: str) -> str:
    return f"{_ROLE_PREFIX}{name}"


def _runner_node_id(kind: str) -> str:
    return f"{_RUNNER_PREFIX}{kind}"


def _build_graph(ctx: GraphContext) -> GraphData:  # noqa: ARG001 - ctx unused by this source
    from hivepilot.registry import RUNNER_MAP, SECRETS_MAP
    from hivepilot.roles import ROLES
    from hivepilot.services.notification_service import NOTIFIER_MAP

    plugins = _get_plugin_manager()
    health = plugins.check_all()

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    seen_runner_kinds: set[str] = set()

    _CONTRIBUTION_KIND_TO_PREFIX: dict[str, tuple[str, str, Mapping[str, Any] | None]] = {
        "notifiers": ("notifier", _NOTIFIER_PREFIX, NOTIFIER_MAP),
        "secrets": ("secret", _SECRET_PREFIX, SECRETS_MAP),
        "panels": ("panel", _PANEL_PREFIX, None),
        "skills": ("skill", _SKILL_PREFIX, None),
    }

    for record in plugins.loaded:
        contributions = record.contributions or {}
        plugin_status = health[record.name].status if record.name in health else "enabled"
        plugin_id = _plugin_node_id(record.name)
        nodes.append(
            GraphNode(
                id=plugin_id,
                label=record.name,
                kind="plugin",
                status=plugin_status,
                group=record.source,
                badges=tuple(sorted(contributions)),
                meta={"source": record.source},
            )
        )

        for kind in contributions.get("runners", []):
            seen_runner_kinds.add(kind)
            nodes.append(
                GraphNode(
                    id=_runner_node_id(kind),
                    label=kind,
                    kind="runner",
                    status="enabled" if kind in RUNNER_MAP else "disabled",
                    group="runner",
                )
            )
            edges.append(
                GraphEdge(source=plugin_id, target=_runner_node_id(kind), kind="contributes")
            )

        for contribution_kind, (
            node_kind,
            prefix,
            live_map,
        ) in _CONTRIBUTION_KIND_TO_PREFIX.items():
            for name in contributions.get(contribution_kind, []):
                node_id = f"{prefix}{name}"
                status = None
                if live_map is not None:
                    status = "enabled" if name in live_map else "disabled"
                nodes.append(
                    GraphNode(
                        id=node_id, label=name, kind=node_kind, status=status, group=node_kind
                    )
                )
                edges.append(GraphEdge(source=plugin_id, target=node_id, kind="contributes"))

        for hook_name in contributions.get("hooks", []):
            hook_id = f"{_HOOK_PREFIX}{record.name}:{hook_name}"
            nodes.append(
                GraphNode(id=hook_id, label=hook_name, kind="hook", status=None, group="hook")
            )
            edges.append(GraphEdge(source=plugin_id, target=hook_id, kind="contributes"))

    # Built-in runner kinds registered directly in hivepilot/registry.py
    # (never attributed to a plugin's `contributions`, since they aren't
    # plugin-contributed) — surfaced too, so the graph shows every ACTIVE
    # runner kind, not only plugin-sourced ones.
    for kind in sorted(RUNNER_MAP):
        if kind in seen_runner_kinds:
            continue
        nodes.append(
            GraphNode(
                id=_runner_node_id(kind),
                label=kind,
                kind="runner",
                status="enabled",
                group="built-in",
            )
        )

    # Role nodes + role -> runner edges (roles.py ROLES — the role/runner
    # binding table, see hivepilot/roles.py `Role.runner`).
    for role_name, role in sorted(ROLES.items()):
        role_id = _role_node_id(role_name)
        nodes.append(
            GraphNode(
                id=role_id, label=role.title or role_name, kind="role", status=None, group="role"
            )
        )
        if role.runner:
            edges.append(
                GraphEdge(source=role_id, target=_runner_node_id(role.runner), kind="uses")
            )

    return GraphData(source="plugins", nodes=tuple(nodes), edges=tuple(edges), layout_hint="dag")


def _plugin_detail(name: str) -> GraphDetail | None:
    plugins = _get_plugin_manager()
    record = next((r for r in plugins.loaded if r.name == name), None)
    if record is None:
        return None
    health = plugins.check_all().get(name)
    contributions = record.contributions or {}
    rows = [[kind, ", ".join(names)] for kind, names in sorted(contributions.items()) if names]
    sections: list[Any] = [
        PanelTextSection(kind="text", content=f"source={record.source} location={record.location}")
    ]
    if rows:
        sections.append(
            PanelTableSection(kind="table", columns=["contribution", "names"], rows=rows)
        )
    tags: tuple[str, ...] = ("plugin", record.source)
    if health is not None:
        tags = tags + (health.status,)
    return GraphDetail(title=name, tags=tags, sections=tuple(sections))


def _role_detail(name: str) -> GraphDetail | None:
    from hivepilot.roles import ROLES

    role = ROLES.get(name)
    if role is None:
        return None
    sections = (
        PanelTextSection(kind="text", content=role.title or name),
        PanelTableSection(
            kind="table",
            columns=["attribute", "value"],
            rows=[["runner", role.runner or "-"], ["model", role.model or "-"]],
        ),
    )
    return GraphDetail(title=role.title or name, tags=("role",), sections=sections)


def _simple_kind_detail(kind: str, name: str) -> GraphDetail:
    """Generic detail for runner/notifier/secret/panel/skill nodes: NAME +
    enabled/disabled status only. For a `secret` node in particular this is
    the whole no-secret-leak contract — never a resolved value, since
    nothing here ever calls `SecretsBackend.resolve(...)`."""
    from hivepilot.registry import RUNNER_MAP, SECRETS_MAP
    from hivepilot.services.notification_service import NOTIFIER_MAP

    live_maps: dict[str, Mapping[str, Any]] = {
        "runner": RUNNER_MAP,
        "notifier": NOTIFIER_MAP,
        "secret": SECRETS_MAP,
    }
    live_map = live_maps.get(kind)
    status = "unknown"
    if live_map is not None:
        status = "enabled" if name in live_map else "disabled"
    return GraphDetail(
        title=name,
        tags=(kind, status),
        sections=(PanelTextSection(kind="text", content=f"{kind} '{name}' — status: {status}"),),
    )


def _node_detail(ctx: GraphContext, node_id: str) -> GraphDetail | None:  # noqa: ARG001
    if node_id.startswith(_PLUGIN_PREFIX):
        return _plugin_detail(node_id[len(_PLUGIN_PREFIX) :])
    if node_id.startswith(_ROLE_PREFIX):
        return _role_detail(node_id[len(_ROLE_PREFIX) :])
    if node_id.startswith(_RUNNER_PREFIX):
        return _simple_kind_detail("runner", node_id[len(_RUNNER_PREFIX) :])
    if node_id.startswith(_NOTIFIER_PREFIX):
        return _simple_kind_detail("notifier", node_id[len(_NOTIFIER_PREFIX) :])
    if node_id.startswith(_SECRET_PREFIX):
        return _simple_kind_detail("secret", node_id[len(_SECRET_PREFIX) :])
    if node_id.startswith(_PANEL_PREFIX):
        name = node_id[len(_PANEL_PREFIX) :]
        return GraphDetail(
            title=name,
            tags=("panel",),
            sections=(PanelTextSection(kind="text", content=f"panel '{name}'"),),
        )
    if node_id.startswith(_SKILL_PREFIX):
        name = node_id[len(_SKILL_PREFIX) :]
        return GraphDetail(
            title=name,
            tags=("skill",),
            sections=(PanelTextSection(kind="text", content=f"skill '{name}'"),),
        )
    if node_id.startswith(_HOOK_PREFIX):
        name = node_id[len(_HOOK_PREFIX) :]
        return GraphDetail(
            title=name,
            tags=("hook",),
            sections=(PanelTextSection(kind="text", content=f"hook '{name}'"),),
        )
    return None


PLUGINS_GRAPH_SOURCE = GraphSourceSpec(
    name="plugins",
    data=_build_graph,
    node_detail=_node_detail,
    title="Plugins",
    min_role="read",
    params=(),
)
