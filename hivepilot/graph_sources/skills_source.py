"""Built-in `skills` graph source (Mirador Graph View PRD, Sprint 2).

Unlike every other graph source, this one does NOT render HivePilot's own
tenant/config state -- it scans a LOCAL FILESYSTEM path (configured via
`hivepilot.config.settings.graph_skills_scan_path`) for `SKILL.md` files
plus sibling `hooks/`/`commands/`/`agents/` directories, mirroring the
`skill`/`hook`/`command`/`agent` plugin-contribution shapes `plugins.py`
already knows about (see `hivepilot/graph_sources/plugins_source.py`).

D3 (Sprint 2 spec decision): **`min_role="admin"`** -- a local host
filesystem scan is qualitatively different from every tenant-scoped or
config-level graph source; it must never be reachable by a read/run/approve
token, exactly like `GET /v1/memories`'s own admin gate. The S1 framework
(`hivepilot/graph.py`'s `_enforce_graph_min_role` caller in
`hivepilot/services/api_service.py`) enforces this automatically once
`SKILLS_GRAPH_SOURCE.min_role == "admin"` is registered -- nothing in this
module re-implements the gate.

Path-traversal guard: every filesystem path this module reads is verified,
via `Path.resolve()` + `Path.relative_to()`, to still live inside the
configured scan root AFTER resolving symlinks, before it is ever opened.
Any file read is additionally capped at `_MAX_READ_BYTES` -- a malicious or
huge `SKILL.md`/front-matter can never exhaust memory or leak content past
the cap. No secret VALUE is ever read or rendered -- this module only reads
markdown/YAML front matter authored to be displayed.
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

import yaml

from hivepilot.config import settings
from hivepilot.graph import (
    GraphContext,
    GraphData,
    GraphDetail,
    GraphEdge,
    GraphNode,
    GraphSourceSpec,
)
from hivepilot.plugins import PanelTableSection, PanelTextSection

_SKILL_PREFIX = "skill:"
_HOOK_PREFIX = "hook:"
_COMMAND_PREFIX = "command:"
_AGENT_PREFIX = "agent:"

# Defense-in-depth caps (path-traversal guard + no-unbounded-read discipline
# -- Sprint 2 spec acceptance criteria 4).
_MAX_READ_BYTES = 65536
_MAX_DESCRIPTION_LEN = 500


def _is_within_root(path: Path, root_resolved: Path) -> Path | None:
    """Resolve *path* (following symlinks) and return it ONLY if it still
    lives inside *root_resolved* -- else `None`. This is the single
    path-traversal choke point every filesystem access in this module goes
    through before opening/globbing a path."""
    try:
        resolved = path.resolve(strict=False)
    except (OSError, ValueError):
        return None
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        return None
    return resolved


def _read_capped(path: Path) -> str:
    """Read at most `_MAX_READ_BYTES` from *path* -- never the whole file,
    however large. Any I/O error degrades to an empty string (never
    raises)."""
    try:
        with path.open("rb") as handle:
            data = handle.read(_MAX_READ_BYTES)
        return data.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _parse_front_matter(text: str) -> dict[str, Any]:
    """Parse a `SKILL.md`'s leading `---\\n...\\n---` YAML front matter block.
    Malformed/absent front matter degrades to `{}` -- never raises."""
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        loaded = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _status_for(rel_path: str) -> str:
    """ "vendored" if any path component of *rel_path* is literally
    "vendored" (case-insensitive) -- e.g. `vendored/some-skill/SKILL.md` --
    else "active". A local-scan-only convention this module owns; there is
    no external vendored/active flag to read."""
    if any(part.lower() == "vendored" for part in Path(rel_path).parts):
        return "vendored"
    return "active"


def _truncate_escape(text: str, limit: int = _MAX_DESCRIPTION_LEN) -> str:
    """Truncate *text* to *limit* chars then HTML-escape it -- front-matter
    `description` content is source-authored/untrusted, same discipline as
    every other `GraphDetail`/`PanelData` text section (see `hivepilot/
    graph.py`'s module docstring)."""
    truncated = text[:limit]
    if len(text) > limit:
        truncated += "..."
    return html.escape(truncated)


def _nearest_skill_dir(
    file_path: Path, root_resolved: Path, skill_dirs: dict[Path, str]
) -> str | None:
    """Walk *file_path*'s ancestor directories (stopping at *root_resolved*)
    looking for the nearest one that is a known skill directory. Returns
    that skill's node id, or `None` when *file_path* is not nested under any
    scanned skill (an "orphan" hook/command/agent -- still emitted as a
    node, just with no membership edge)."""
    current = file_path.parent
    while True:
        if current in skill_dirs:
            return skill_dirs[current]
        if current == root_resolved or current == current.parent:
            return None
        current = current.parent


def _scan_root(root: Path) -> tuple[list[GraphNode], list[GraphEdge]]:
    root_resolved = root.resolve(strict=False)
    if not root_resolved.is_dir():
        return [], []

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    skill_dirs: dict[Path, str] = {}

    for skill_md in sorted(root_resolved.rglob("SKILL.md")):
        resolved = _is_within_root(skill_md, root_resolved)
        if resolved is None or not resolved.is_file():
            continue
        rel = resolved.relative_to(root_resolved).as_posix()
        front_matter = _parse_front_matter(_read_capped(resolved))
        name = front_matter.get("name")
        label = name if isinstance(name, str) and name else resolved.parent.name
        tags = front_matter.get("tags")
        badges = tuple(str(t) for t in tags) if isinstance(tags, list) else ()
        status = _status_for(rel)
        node_id = f"{_SKILL_PREFIX}{rel}"
        nodes.append(
            GraphNode(
                id=node_id,
                label=label,
                kind="skill",
                status=status,
                group="skill",
                badges=badges,
                meta={"path": rel},
            )
        )
        skill_dirs[resolved.parent] = node_id

    for kind, dirname, prefix in (
        ("hook", "hooks", _HOOK_PREFIX),
        ("command", "commands", _COMMAND_PREFIX),
        ("agent", "agents", _AGENT_PREFIX),
    ):
        for candidate_dir in sorted(root_resolved.rglob(dirname)):
            if not candidate_dir.is_dir():
                continue
            resolved_dir = _is_within_root(candidate_dir, root_resolved)
            if resolved_dir is None:
                continue
            for item in sorted(resolved_dir.iterdir()):
                if not item.is_file():
                    continue
                resolved_item = _is_within_root(item, root_resolved)
                if resolved_item is None:
                    continue
                rel_item = resolved_item.relative_to(root_resolved).as_posix()
                node_id = f"{prefix}{rel_item}"
                status = _status_for(rel_item)
                nodes.append(
                    GraphNode(
                        id=node_id,
                        label=resolved_item.name,
                        kind=kind,
                        status=status,
                        group=kind,
                        meta={"path": rel_item},
                    )
                )
                owner = _nearest_skill_dir(resolved_item, root_resolved, skill_dirs)
                if owner is not None:
                    edges.append(GraphEdge(source=owner, target=node_id, kind="member"))

    return nodes, edges


def _build_graph(ctx: GraphContext) -> GraphData:  # noqa: ARG001 - local host scan, no tenant/role scoping
    """Local host filesystem scan -- deliberately IGNORES `ctx` entirely
    (no tenant concept applies to a host-local skills directory, unlike
    `pipeline_source.py`). `graph_skills_scan_path` unset/not-a-directory
    degrades to an empty graph, never a crash."""
    root = settings.graph_skills_scan_path
    if root is None:
        return GraphData(source="skills", nodes=(), edges=())
    root_path = Path(root)
    if not root_path.is_dir():
        return GraphData(source="skills", nodes=(), edges=())
    nodes, edges = _scan_root(root_path)
    return GraphData(source="skills", nodes=tuple(nodes), edges=tuple(edges), layout_hint="grid")


def _parse_node_id(node_id: str) -> tuple[str, str] | None:
    for prefix, kind in (
        (_SKILL_PREFIX, "skill"),
        (_HOOK_PREFIX, "hook"),
        (_COMMAND_PREFIX, "command"),
        (_AGENT_PREFIX, "agent"),
    ):
        if node_id.startswith(prefix):
            rel = node_id[len(prefix) :]
            if not rel:
                return None
            return rel, kind
    return None


def _node_detail(ctx: GraphContext, node_id: str) -> GraphDetail | None:  # noqa: ARG001
    parsed = _parse_node_id(node_id)
    if parsed is None:
        return None
    rel, kind = parsed

    root = settings.graph_skills_scan_path
    if root is None:
        return None
    root_path = Path(root)
    if not root_path.is_dir():
        return None
    root_resolved = root_path.resolve(strict=False)

    candidate = root_resolved / rel
    resolved = _is_within_root(candidate, root_resolved)
    if resolved is None or not resolved.is_file():
        return None

    status = _status_for(rel)
    sections: list[Any] = []
    if kind == "skill":
        front_matter = _parse_front_matter(_read_capped(resolved))
        description = front_matter.get("description")
        description = description if isinstance(description, str) else ""
        sections.append(
            PanelTextSection(
                kind="text", content=_truncate_escape(description or "(no description)")
            )
        )
    sections.append(PanelTableSection(kind="table", columns=["path"], rows=[[rel]]))

    title = resolved.parent.name if kind == "skill" else resolved.name
    return GraphDetail(title=title, tags=(kind, status), sections=tuple(sections))


SKILLS_GRAPH_SOURCE = GraphSourceSpec(
    name="skills",
    data=_build_graph,
    node_detail=_node_detail,
    title="Skills (local host scan)",
    min_role="admin",
    params=(),
)
