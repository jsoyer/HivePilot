"""Graph-native backend contract for the Mirador Graph View PRD (Sprint 1).

A `GraphSource` is a named contribution — like a Mirador `panel`
(`hivepilot/plugins.py`) — that renders some slice of HivePilot's own state
(plugins, roles, runners, and later runs/pipelines) as a node/edge graph
instead of a flat panel. This module defines the frozen dataclass shapes
every graph source and consumer (the `/v1/graph/*` API, later sprints' web
renderer) must agree on, plus a small module-level registry and the
never-raise `run_graph_fetch` / `run_graph_node_detail` wrappers that keep a
broken/malformed source from ever reaching the API layer as a 500.

Deliberately mirrors `hivepilot/plugins.py`'s `PanelSpec` /
`normalize_panel_data` / `PluginManager.run_panel_fetch` pattern rather than
inventing a parallel design: `GraphDetail.sections` REUSES `PanelData`'s
closed section-kind system (`stat`/`table`/`text`) via `normalize_panel_data`
itself, so a graph node's detail view renders through the exact same
renderer-agnostic contract a Mirador panel does.

No web/Textual optional dependency: this module only imports from
`hivepilot.plugins` (itself dependency-light) and the standard library, so
`python -c "import hivepilot.graph"` succeeds in any HivePilot install.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from hivepilot.plugins import (
    PanelStatSection,
    PanelTableSection,
    PanelTextSection,
    normalize_panel_data,
)
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

# The closed set of `GraphData.layout_hint` values a consumer (later
# sprints' web renderer) understands. `None` is also valid (no hint / let
# the renderer choose) but is handled separately, mirroring
# `PANEL_STAT_STATUSES`' "unset" handling in `hivepilot/plugins.py`.
GRAPH_LAYOUT_HINTS = ("grid", "dag")


@dataclass(frozen=True, slots=True)
class GraphNode:
    """One node in a graph source's response.

    `meta` is arbitrary, renderer-agnostic key/value context (e.g. plugin
    `source`) — like `HealthStatus.detail` / panel section text, it must
    NEVER contain a secret/token VALUE, only presence/config/name-level
    facts. `id` must be unique within a single `GraphData` response (not
    globally); a source is free to namespace its own ids (e.g.
    `"plugin:hugo"`) to keep that guarantee cheap.
    """

    id: str
    label: str
    kind: str
    status: str | None = None
    group: str | None = None
    badges: tuple[str, ...] = ()
    meta: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GraphEdge:
    """One directed edge in a graph source's response. `source`/`target`
    reference `GraphNode.id` values from the SAME `GraphData.nodes` — this
    module does not itself validate referential integrity (a source
    referencing an id it didn't also emit as a node is a source-authoring
    bug, not something `normalize_graph_data` rejects in Sprint 1)."""

    source: str
    target: str
    kind: str | None = None
    label: str | None = None


@dataclass(frozen=True, slots=True)
class GraphData:
    """A graph source's full response: every node + edge it currently has,
    plus an optional rendering hint. `source` is always the emitting
    source's own registered `name` — `run_graph_fetch` sets it
    unconditionally (see below), so a source's own `data` callable never
    needs to (and can't spoof) a different value."""

    source: str
    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()
    layout_hint: str | None = None


@dataclass(frozen=True, slots=True)
class GraphDetail:
    """A single node's detail view. `sections` REUSES the closed
    `PanelStatSection`/`PanelTableSection`/`PanelTextSection` shapes from
    `hivepilot/plugins.py` — see module docstring. Content
    (`title`/`tags`/section text) is source-authored and UNTRUSTED, exactly
    like `PanelData` — a future web renderer must escape it, never inject
    raw markup."""

    title: str
    tags: tuple[str, ...] = ()
    sections: tuple[PanelStatSection | PanelTableSection | PanelTextSection, ...] = ()


@dataclass(frozen=True, slots=True)
class GraphContext:
    """Per-request context passed into a source's `data`/`node_detail`
    callables. `tenant`/`role` are ALWAYS derived from the caller's own
    resolved token (`api_service.require_role`'s `TokenEntry`) — never
    client-supplied — so a source can never be tricked into computing
    another tenant's view via a spoofed context. `params` is the raw
    (already-caller-scoped) query-string dict; a source declaring `params`
    in its `GraphSourceSpec` is documenting which keys it reads, not
    enforcing that only those are present."""

    tenant: str
    role: str
    params: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GraphSourceSpec:
    """A single graph source contribution — the graph-native analogue of
    `PanelSpec`. `data` and `node_detail` must NEVER be called directly by
    a consumer; always go through `run_graph_fetch` / `run_graph_node_detail`
    below so a raising/malformed source can never crash a caller. `min_role`
    defaults to `"read"`, the floor every valid token satisfies (mirrors
    `PanelSpec.min_role`'s default)."""

    name: str
    data: Callable[["GraphContext"], "GraphData"]
    node_detail: Callable[["GraphContext", str], "GraphDetail | None"] | None = None
    title: str | None = None
    min_role: str = "read"
    params: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Registry — module-level, mirrors PluginManager.list_panels()/get_panel()'s
# lookup shape, but as free functions over a single process-global dict
# (graph sources are not per-PluginManager-instance state; they're a small,
# closed set of built-ins today — see hivepilot/graph_sources/ — with room
# for future plugin-contributed sources in a later sprint).
# ---------------------------------------------------------------------------

_GRAPH_SOURCES: dict[str, GraphSourceSpec] = {}


class GraphSourceNameCollisionError(RuntimeError):
    """Raised when two graph sources declare the same `name`. Mirrors
    `PanelNameCollisionError`/`RunnerKindCollisionError` — a hard stop, not a
    silent last-wins overwrite, so a source can never shadow another
    source's registration unnoticed."""


def register_graph_source(spec: GraphSourceSpec) -> None:
    """Register *spec*. Idempotent for the exact same object (re-importing a
    module that registers its own built-in source at import time must never
    raise on a second import in the same process — Python caches modules,
    but tests/tools that re-exec source directly could otherwise trip this).
    A DIFFERENT spec under an already-taken name is a hard collision."""
    current = _GRAPH_SOURCES.get(spec.name)
    if current is not None and current is not spec:
        raise GraphSourceNameCollisionError(
            f"Graph source '{spec.name}' is already registered; refusing to silently replace it"
        )
    _GRAPH_SOURCES[spec.name] = spec


def list_graph_sources() -> list[GraphSourceSpec]:
    """Every registered graph source, sorted by name — safe to call
    unconditionally from the `/v1/graph/sources` endpoint."""
    return [_GRAPH_SOURCES[name] for name in sorted(_GRAPH_SOURCES)]


def get_graph_source(name: str) -> GraphSourceSpec | None:
    """Look up a single registered graph source by name, or `None` if
    unknown — callers (the API layer) must treat `None` as 404."""
    return _GRAPH_SOURCES.get(name)


# ---------------------------------------------------------------------------
# Never-raise discipline — mirrors normalize_panel_data / run_panel_fetch
# (hivepilot/plugins.py) faithfully: a malformed/raising source degrades to
# a normalized error result, NEVER an exception that reaches the API layer.
# ---------------------------------------------------------------------------


class GraphDataError(ValueError):
    """Raised by `normalize_graph_data`/the internal detail-normalizer when
    a source's returned value does not match the closed `GraphData`/
    `GraphDetail` shape. Structural problems are rejected outright — callers
    (`run_graph_fetch`/`run_graph_node_detail`) must catch it and fall back
    to a normalized error result; this function itself never silently drops
    malformed data."""


def _json_safe_meta(meta: Mapping[Any, Any]) -> dict[str, Any]:
    """Coerce `GraphNode.meta` into a mapping GUARANTEED to survive the JSON
    encoder the API layer runs it through (`_graph_data_to_dict` ->
    FastAPI's response encoding), not just this module's own shape checks.

    Without this, `_coerce_node` only confirmed `meta` was A Mapping, not
    that its keys/values were JSON-serializable — a source returning e.g.
    `GraphNode(meta={"x": object()})` passed normalization cleanly, then
    blew up as an UNCAUGHT 500 at the JSON-encoding step, entirely OUTSIDE
    `run_graph_fetch`'s never-raise `try`/`except`. Any non-string key or
    non-JSON-serializable value is stringified rather than dropped — keeps
    the key/value informative for debugging while guaranteeing the
    never-raise contract holds all the way through the response, not just
    through this module's own validation.
    """
    safe: dict[str, Any] = {}
    for key, value in meta.items():
        safe_key = key if isinstance(key, str) else str(key)
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            safe[safe_key] = str(value)
        else:
            safe[safe_key] = value
    return safe


def _coerce_node(raw: Any) -> GraphNode:
    if not isinstance(raw, GraphNode):
        raise GraphDataError(f"graph node must be a GraphNode, got {type(raw).__name__}")
    if not isinstance(raw.id, str) or not raw.id:
        raise GraphDataError("GraphNode.id must be a non-empty string")
    if not isinstance(raw.label, str):
        raise GraphDataError("GraphNode.label must be a string")
    if not isinstance(raw.kind, str) or not raw.kind:
        raise GraphDataError("GraphNode.kind must be a non-empty string")
    if raw.status is not None and not isinstance(raw.status, str):
        raise GraphDataError("GraphNode.status must be a string or None")
    if raw.group is not None and not isinstance(raw.group, str):
        raise GraphDataError("GraphNode.group must be a string or None")
    if not isinstance(raw.badges, (tuple, list)) or not all(isinstance(b, str) for b in raw.badges):
        raise GraphDataError("GraphNode.badges must be a tuple of strings")
    if not isinstance(raw.meta, Mapping):
        raise GraphDataError("GraphNode.meta must be a mapping")
    safe_meta = _json_safe_meta(raw.meta)
    if safe_meta == dict(raw.meta):
        return raw
    return GraphNode(
        id=raw.id,
        label=raw.label,
        kind=raw.kind,
        status=raw.status,
        group=raw.group,
        badges=tuple(raw.badges),
        meta=safe_meta,
    )


def _coerce_edge(raw: Any) -> GraphEdge:
    if not isinstance(raw, GraphEdge):
        raise GraphDataError(f"graph edge must be a GraphEdge, got {type(raw).__name__}")
    if not isinstance(raw.source, str) or not raw.source:
        raise GraphDataError("GraphEdge.source must be a non-empty string")
    if not isinstance(raw.target, str) or not raw.target:
        raise GraphDataError("GraphEdge.target must be a non-empty string")
    if raw.kind is not None and not isinstance(raw.kind, str):
        raise GraphDataError("GraphEdge.kind must be a string or None")
    if raw.label is not None and not isinstance(raw.label, str):
        raise GraphDataError("GraphEdge.label must be a string or None")
    return raw


def normalize_graph_data(raw: Any) -> GraphData:
    """Coerce/validate a source's returned value into the closed `GraphData`
    shape. Structurally malformed input raises `GraphDataError` — callers
    (namely `run_graph_fetch`) must catch it and fall back to an error
    graph; this function itself never silently drops data."""
    if not isinstance(raw, GraphData):
        raise GraphDataError(f"graph source must return GraphData, got {type(raw).__name__}")
    if not isinstance(raw.source, str) or not raw.source:
        raise GraphDataError("GraphData.source must be a non-empty string")
    if not isinstance(raw.nodes, (tuple, list)):
        raise GraphDataError("GraphData.nodes must be a tuple/list of GraphNode")
    if not isinstance(raw.edges, (tuple, list)):
        raise GraphDataError("GraphData.edges must be a tuple/list of GraphEdge")
    nodes = tuple(_coerce_node(n) for n in raw.nodes)
    edges = tuple(_coerce_edge(e) for e in raw.edges)
    if raw.layout_hint is not None and raw.layout_hint not in GRAPH_LAYOUT_HINTS:
        raise GraphDataError(f"invalid layout_hint: {raw.layout_hint!r}")
    return GraphData(source=raw.source, nodes=nodes, edges=edges, layout_hint=raw.layout_hint)


def run_graph_fetch(spec: GraphSourceSpec, ctx: GraphContext) -> GraphData:
    """Run a single graph source's `data(ctx)`. Never raises: an exception
    raised by the callable itself, or a malformed return value (rejected by
    `normalize_graph_data`), is caught here and reported as a single
    `kind="error"`/`status="error"` node — the exception TYPE name only,
    never the exception message, mirroring `PluginManager.run_panel_fetch`'s
    no-secret-leak discipline. `GraphData.source` is always rewritten to the
    SOURCE'S OWN registered `spec.name` before returning (both on the happy
    path and the error path), so a caller can always trust the response's
    `source` field regardless of what the source's own callable returned.
    """
    try:
        result = spec.data(ctx)
        normalized = normalize_graph_data(result)
        return GraphData(
            source=spec.name,
            nodes=normalized.nodes,
            edges=normalized.edges,
            layout_hint=normalized.layout_hint,
        )
    except Exception as exc:  # noqa: BLE001 — a graph source fetch must never crash
        logger.warning("graph.fetch_failed", source=spec.name, error=str(exc))
        return GraphData(
            source=spec.name,
            nodes=(
                GraphNode(
                    id="error",
                    label=type(exc).__name__,
                    kind="error",
                    status="error",
                ),
            ),
            edges=(),
            layout_hint=None,
        )


def _normalize_graph_detail(raw: Any) -> GraphDetail:
    if not isinstance(raw, GraphDetail):
        raise GraphDataError(f"node_detail must return GraphDetail, got {type(raw).__name__}")
    if not isinstance(raw.title, str):
        raise GraphDataError("GraphDetail.title must be a string")
    if not isinstance(raw.tags, (tuple, list)) or not all(isinstance(t, str) for t in raw.tags):
        raise GraphDataError("GraphDetail.tags must be a tuple of strings")
    if not isinstance(raw.sections, (tuple, list)):
        raise GraphDataError("GraphDetail.sections must be a tuple/list of panel sections")
    # Reuse hivepilot.plugins' own PanelData validator rather than
    # reimplementing section-shape checking — see module docstring.
    panel_data = normalize_panel_data({"sections": list(raw.sections)})
    return GraphDetail(
        title=raw.title, tags=tuple(raw.tags), sections=tuple(panel_data["sections"])
    )


def run_graph_node_detail(
    spec: GraphSourceSpec, ctx: GraphContext, node_id: str
) -> GraphDetail | None:
    """Run a single graph source's `node_detail(ctx, node_id)`. Never
    raises: an exception raised by the callable itself, or a malformed
    return value, is caught and normalized into a `GraphDetail` whose only
    content is the exception TYPE name (never the exception message) — same
    no-secret-leak discipline as `run_graph_fetch`.

    Returns `None` in exactly two cases the API layer must treat as 404:
    the source declares no `node_detail` callable at all, or the callable
    itself genuinely returns `None` (node id not recognized by this
    source). Both are distinct from the exception/malformed-shape case
    above, which returns a normal (200-worthy) error `GraphDetail` instead
    of `None` — a raising `node_detail` is a source BUG, not "unknown
    node", and must never be indistinguishable from a clean 404.
    """
    if spec.node_detail is None:
        return None
    try:
        result = spec.node_detail(ctx, node_id)
        if result is None:
            return None
        return _normalize_graph_detail(result)
    except Exception as exc:  # noqa: BLE001 — a node_detail call must never crash
        logger.warning(
            "graph.node_detail_failed", source=spec.name, node_id=node_id, error=str(exc)
        )
        return GraphDetail(
            title="error",
            tags=("error",),
            sections=(PanelTextSection(kind="text", content=type(exc).__name__),),
        )
