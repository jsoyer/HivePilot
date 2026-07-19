"""`secrets_trust_graph_source` -- plugin-contributed `secrets-trust`
`graph_sources` capability contribution (Mirador GraphSources plugins
sprint).

Renders a trust graph over the secrets subsystem: one `provider` node per
registered secrets backend (`hivepilot.registry.SECRETS_MAP` --
env/file/vault/sops, plus whatever plugin-contributed backends a live
`PluginManager` has registered, e.g. infisical/onepassword), and one
`secret` node per declared secret NAME found in `ProjectConfig.secrets`
(`hivepilot/models.py` -- `NAME -> {source, ...}` spec dict, the SAME
config shape `hivepilot.services.secrets_service.SecretResolver.resolve`
consumes), with a `secret -> provider` edge showing which backend resolves
it. Mirrors `hivepilot.graph_sources.plugins_source`'s own `secret` node
discipline (see that module's docstring) and
`plugins/example_graph_source.py`'s overall plugin-contract structure.

ABSOLUTE SECURITY RULE -- no secret VALUE, ever
--------------------------------------------------
Every field this module ever puts into a `GraphNode`/`GraphDetail` is a
NAME, a PROVIDER/backend identifier, or a project name -- never a resolved
secret value. This module NEVER calls `SecretsBackend.resolve(...)` (the
method every `EnvSecretsBackend`/`FileSecretsBackend`/`VaultSecretsBackend`/
`SopsSecretsBackend`/plugin-contributed backend implements) and NEVER calls
`hivepilot.services.secrets_service.secret_resolver.resolve(...)`. It only
ever reads:
  - `hivepilot.registry.SECRETS_MAP` -- just the registered backend NAMES
    (dict keys), never invoking anything on the backend instances.
  - `ProjectConfig.secrets` -- a NAME -> spec dict already declared in
    config (`projects.yaml`), itself never a value: the spec is a
    *reference* (`source`, plus backend-specific location fields like
    `key`/`path`), consumed only for its `source` string. `node_detail`
    deliberately does NOT surface the rest of the spec (no `key`/`path`/
    `file` fields) at all -- even though those are themselves just
    references, not secret material, keeping this source's output surface
    minimal is simpler to reason about and audit than re-deriving a
    per-backend "is this spec key safe" allowlist.
`tests/test_secrets_trust_graph_source.py::TestNeverLeaksASecretValue`
seeds a secret whose declared env var actually holds a known marker value
and proves it never reaches any `GraphData`/`GraphDetail` this module
produces, and that no resolver is ever invoked.

TENANT: unlike `plugins/drift_graph_source.py` (tenant data), `projects.yaml`
is pure configuration with no tenant concept -- same as
`hivepilot.graph_sources.plugins_source`'s own tenant-free pattern (see that
module's docstring). `ctx` is accepted (for `GraphSourceSpec` shape
consistency) but unused by `_build_graph`.

Opt-in / read-only: gated on `settings.secrets_trust_graph_source_enabled`
(default False -- `register()` early-returns `{}` when unset, required by
`tests/test_gating_conformance.py::TestAllPluginStemsHaveEnabledFlag`). This
module never calls a writer of any kind and has no other side effect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hivepilot.graph import GraphData, GraphDetail, GraphEdge, GraphNode, GraphSourceSpec
from hivepilot.plugins import PanelTextSection

if TYPE_CHECKING:
    from hivepilot.graph import GraphContext

_PROVIDER_PREFIX = "provider:"
_SECRET_PREFIX = "secret:"


def _provider_node_id(name: str) -> str:
    return f"{_PROVIDER_PREFIX}{name}"


def _secret_node_id(project: str, name: str) -> str:
    return f"{_SECRET_PREFIX}{project}:{name}"


def _build_graph(ctx: "GraphContext") -> GraphData:  # noqa: ARG001 - ctx unused, see docstring
    # Importing secrets_service (rather than just hivepilot.registry) ensures
    # the four builtin backends are registered in SECRETS_MAP even in a
    # process that hasn't otherwise imported it yet -- mirrors
    # hivepilot/graph_sources/__init__.py's own "import for registration
    # side effect" idiom.
    import hivepilot.services.secrets_service  # noqa: F401
    from hivepilot.registry import SECRETS_MAP
    from hivepilot.services.project_service import load_projects

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    provider_ids: set[str] = set()

    def _ensure_provider(name: str, *, status: str) -> str:
        provider_id = _provider_node_id(name)
        if provider_id not in provider_ids:
            provider_ids.add(provider_id)
            nodes.append(
                GraphNode(
                    id=provider_id, label=name, kind="provider", status=status, group="provider"
                )
            )
        return provider_id

    for backend_name in sorted(SECRETS_MAP):
        _ensure_provider(backend_name, status="enabled")

    projects_file = load_projects()
    for project_name, project in sorted(projects_file.projects.items()):
        for secret_name, spec in sorted(project.secrets.items()):
            source = str(spec.get("source") or "env")
            provider_id = _ensure_provider(
                source, status="enabled" if source in SECRETS_MAP else "unknown"
            )
            secret_node_id = _secret_node_id(project_name, secret_name)
            nodes.append(
                GraphNode(
                    id=secret_node_id,
                    label=secret_name,
                    kind="secret",
                    status=None,
                    group=project_name,
                    meta={"project": project_name, "source": source},
                )
            )
            edges.append(GraphEdge(source=secret_node_id, target=provider_id, kind="resolves_via"))

    return GraphData(
        source="secrets-trust", nodes=tuple(nodes), edges=tuple(edges), layout_hint="dag"
    )


def _provider_detail(name: str) -> GraphDetail:
    import hivepilot.services.secrets_service  # noqa: F401
    from hivepilot.registry import SECRETS_MAP

    status = "enabled" if name in SECRETS_MAP else "unknown"
    return GraphDetail(
        title=name,
        tags=("provider", status),
        sections=(
            PanelTextSection(kind="text", content=f"secrets provider '{name}' — status: {status}"),
        ),
    )


def _secret_detail(project_name: str, secret_name: str) -> GraphDetail | None:
    from hivepilot.services.project_service import load_projects

    projects_file = load_projects()
    project = projects_file.projects.get(project_name)
    if project is None:
        return None
    spec = project.secrets.get(secret_name)
    if spec is None:
        return None
    source = str(spec.get("source") or "env")
    return GraphDetail(
        title=f"{project_name}/{secret_name}",
        tags=("secret", source),
        sections=(
            PanelTextSection(
                kind="text",
                content=(
                    f"secret '{secret_name}' declared in project '{project_name}', "
                    f"resolves via provider '{source}'"
                ),
            ),
        ),
    )


def _node_detail(ctx: "GraphContext", node_id: str) -> GraphDetail | None:  # noqa: ARG001
    if node_id.startswith(_PROVIDER_PREFIX):
        return _provider_detail(node_id[len(_PROVIDER_PREFIX) :])
    if node_id.startswith(_SECRET_PREFIX):
        rest = node_id[len(_SECRET_PREFIX) :]
        project_name, sep, secret_name = rest.partition(":")
        if not sep:
            return None
        return _secret_detail(project_name, secret_name)
    return None


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.secrets_trust_graph_source_enabled:
        return {}

    return {
        "graph_sources": [
            GraphSourceSpec(
                name="secrets-trust",
                data=_build_graph,
                node_detail=_node_detail,
                title="Secrets Trust",
                min_role="read",
                params=(),
            )
        ]
    }
