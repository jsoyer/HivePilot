"""`TextSource` -- the `text` built-in partition source (propose -> ratify
-> dispatch PRD, Sprint 1, spec section 4): the universal escape hatch. A
*ref* that resolves to an existing local file is read as that file's
content; otherwise *ref* itself is treated as the literal text content
(e.g. a PRD pasted inline). No config, no network, no HivePilot state."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hivepilot.partition_sources import SourceDocument


class TextSourceError(RuntimeError):
    """Raised when *ref* is blank/`None`, or resolves to blank content."""


class TextSource:
    """`PartitionSource` reading a literal string or a local file path."""

    name = "text"

    def fetch(self, ref: str) -> "SourceDocument":
        from hivepilot.partition_sources import SourceDocument, compute_digest

        if ref is None or not ref.strip():
            raise TextSourceError(
                "text source requires a non-blank ref (a literal string or a file path)"
            )

        path = Path(ref)
        from_file = path.is_file()
        content = path.read_text(encoding="utf-8") if from_file else ref

        if not content.strip():
            raise TextSourceError(f"text source ref {ref!r} resolved to blank content")

        return SourceDocument(
            kind="text",
            ref=ref,
            digest=compute_digest(content),
            content=content,
            metadata={"from_file": from_file},
        )
