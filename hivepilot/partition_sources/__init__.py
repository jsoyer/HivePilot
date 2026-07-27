"""hivepilot.partition_sources -- the `PartitionSource` abstraction (propose
-> ratify -> dispatch PRD, Sprint 1, spec section 4).

Mirrors `hivepilot.forges.provider.resolve_forge` exactly: a process-global
name -> instance map (`SOURCE_MAP`) plus a fail-closed `resolve_source(name)`
lookup that NEVER silently falls back to a default source.

Ships four built-in sources, all reading HivePilot's own state: `run` (a
failed/completed run + its steps), `verdict` (a `verdicts` row), `drift` (a
`drift_scans` row), `text` (a literal string or file -- the universal escape
hatch). A different org's tracker (GitHub issues, Linear, Jira, Notion) is a
PLUGIN, not engine code -- see spec section 4's "generic test": would a
different company get value from this with config/plugin changes only?

Registration shape, and why it differs slightly from `hivepilot.forges`
------------------------------------------------------------------------
`hivepilot.forges.provider` (the Protocol/registry) and `hivepilot.forges.
github` (a concrete provider that self-registers via `ForgeRegistry.
register(...)` at its own module's import time) live in separate files, so
`github.py` can import `provider.py` with no cycle. This sprint's file
boundary is exactly `{__init__,run_source,verdict_source,drift_source,
text_source}.py` -- there is no separate "provider.py" sibling module to
hold the Protocol/registry, so it lives here in `__init__.py` instead. To
avoid an `__init__.py` <-> `run_source.py` (etc.) import cycle, the concrete
source classes do NOT self-register at their own module's import time;
instead `_register_builtin_sources()` below imports each concrete class
(deferred, function-scoped) and registers it explicitly, after the
registry itself is fully defined. The *resolve* half of the contract
(`resolve_source`, fail-closed `UnknownSourceError`, never a silent
fallback) is identical to `resolve_forge`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class SourceDocument:
    """The fetched content of a partition source, handed to the proposer
    role as prompt material. `digest` is a `sha256:<hex>` string -- the
    same shape as `PartitionPlan.source.digest` (`hivepilot/partition.py`),
    so a proposer pipeline can round-trip it verbatim into the partition it
    emits."""

    kind: str
    ref: str
    digest: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)


def compute_digest(content: str) -> str:
    """The one canonical `sha256:<hex>` digest computation every built-in
    source uses for its `SourceDocument.digest` -- a single definition so
    no source can drift to a different hash shape."""
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


class PartitionSource(Protocol):
    """Structural interface every partition source implements (spec
    section 4). `name` is the `SOURCE_MAP` registration key."""

    name: str

    def fetch(self, ref: str) -> SourceDocument: ...


class UnknownSourceError(RuntimeError):
    """Raised by `resolve_source` when *name* isn't a registered source.

    Fail-closed -- mirrors `hivepilot.forges.provider.UnknownForgeError`
    exactly: never a silent fallback to a default source."""


class SourceCollisionError(RuntimeError):
    """Raised by `PartitionSourceRegistry.register` when a DIFFERENT
    source instance tries to silently replace an already-registered name
    (mirrors `ForgeCollisionError` / `RunnerKindCollisionError`)."""


SOURCE_MAP: dict[str, PartitionSource] = {}


class PartitionSourceRegistry:
    """Process-global partition-source registry -- mirrors `ForgeRegistry`."""

    @staticmethod
    def register(name: str, source: PartitionSource, *, override: bool = False) -> None:
        if name in SOURCE_MAP and SOURCE_MAP[name] is not source and not override:
            raise SourceCollisionError(
                f"Partition source '{name}' is already registered to "
                f"{type(SOURCE_MAP[name]).__name__}; refusing to silently replace it "
                f"with {type(source).__name__}"
            )
        SOURCE_MAP[name] = source

    @staticmethod
    def known_kinds() -> frozenset[str]:
        return frozenset(SOURCE_MAP)


def resolve_source(name: str) -> PartitionSource:
    """Resolve *name*'s registered `PartitionSource` from `SOURCE_MAP`.

    Fail-closed: raises `UnknownSourceError` (never a silent fallback to a
    default source) if *name* isn't registered -- mirrors
    `hivepilot.forges.provider.resolve_forge` exactly:
    `hivepilot/forges/provider.py:136`'s `FORGE_MAP.get(project.forge)` ->
    `None` -> raise, vs. this function's `SOURCE_MAP.get(name)` -> `None`
    -> raise. Neither ever falls through to a default.
    """
    source = SOURCE_MAP.get(name)
    if source is None:
        raise UnknownSourceError(
            f"Unknown partition source {name!r}; available: {sorted(SOURCE_MAP)}"
        )
    return source


def _register_builtin_sources() -> None:
    """Register the four built-in sources. Deferred, function-scoped
    imports (rather than module-level ones) avoid an `__init__.py` <->
    `run_source.py` (etc.) import cycle -- see this module's docstring."""
    from hivepilot.partition_sources.drift_source import DriftSource
    from hivepilot.partition_sources.run_source import RunSource
    from hivepilot.partition_sources.text_source import TextSource
    from hivepilot.partition_sources.verdict_source import VerdictSource

    PartitionSourceRegistry.register("run", RunSource())
    PartitionSourceRegistry.register("verdict", VerdictSource())
    PartitionSourceRegistry.register("drift", DriftSource())
    PartitionSourceRegistry.register("text", TextSource())


_register_builtin_sources()

__all__ = [
    "SOURCE_MAP",
    "PartitionSource",
    "PartitionSourceRegistry",
    "SourceCollisionError",
    "SourceDocument",
    "UnknownSourceError",
    "compute_digest",
    "resolve_source",
]
