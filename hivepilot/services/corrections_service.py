"""Standing operator corrections, per role, read verbatim into the prompt.

Making an agent stop doing something currently costs a config-repo PR, a
merge, a `config sync` and a restart. This costs editing a file:

    prompts/corrections/<role>.md    # beside prompts/agents/<role>.md
    prompts/corrections/_all.md      # optional, applies to every role

The lessons table already exists and is deliberately NOT this. Lessons are
distilled by an LLM, *scored*, and injected top-N by rank -- the right shape
for durable cross-run learning, the wrong shape for "stop doing X, I am
telling you". A correction is authored, curated and unranked, and it applies
on the next tick.

|              | lessons table          | corrections file        |
|--------------|------------------------|-------------------------|
| author       | the distiller          | the operator            |
| curated      | no -- ranked           | yes                     |
| latency      | next run, if top-N     | next tick               |

**Per role, not one global file.** A correction aimed at the developer is
noise in the CISO's prompt, and it is paid for on every single call.

**The engine stays generic.** Its entire contribution is "for role R, read
`corrections/R.md`" -- no role name appears in this module. Content lives in
the config repo, where tenant policy belongs. Resolution reuses the same
XDG -> config_repo -> base_dir chain as `prompts/agents/`, so a deployment
that keeps its prompts in the config repo gets its corrections from there
too, with no extra setting.

No restart is needed: roles reload on demand (`hivepilot reload`,
`POST /v1/admin/reload`, `SIGHUP`) and per tick when `config_hot_reload` is
on. This module re-reads from disk on every call, so a correction is in force
as soon as it is saved.
"""

from __future__ import annotations

import re
from pathlib import Path

import structlog

from hivepilot.config import settings

logger = structlog.get_logger(__name__)

# The role key becomes a filename. It comes from config today, but a
# path-traversing key would turn this into an arbitrary file read whose
# contents are handed to an agent as instructions -- so it is validated, not
# trusted. Same guard, and same reasoning, as `herdr_board._ROLE_RE`.
_ROLE_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Corrections are read WHOLE and unranked -- that is the point. But the file
# is operator-editable and unbounded, and one pasted stack trace is how a
# prompt reached 148 390 bytes against Linux's 131 072-byte limit for a single
# argv element. Bounded per file, and the cut is declared in the text the
# agent sees: a silent truncation makes it act confidently on half an
# instruction with nothing anywhere recording that the other half existed.
_MAX_CORRECTION_CHARS = 20_000

_ALL_ROLES_FILE = "_all"
_HEADER = (
    "## Standing corrections\n"
    "Operator instructions that override your defaults. They are not "
    "suggestions and they are not ranked -- apply all of them."
)


def _corrections_path(stem: str) -> Path:
    """Resolve `prompts/corrections/<stem>.md` through the config chain.

    Mirrors `roles._resolve_prompt_path`'s tiers (XDG -> config_repo ->
    base_dir) via `resolve_config_path`, minus the packaged-copy fallback:
    corrections are tenant content and the engine ships none.
    """
    return settings.resolve_config_path(Path("prompts") / "corrections" / f"{stem}.md")


def _read_one(stem: str) -> str:
    """Return the bounded contents of one corrections file, or ``""``.

    Absence is silent -- it is the overwhelmingly common case, and emitting
    anything for it would cost tokens on every call forever. A read that
    FAILS is logged: it must never break a run, but it must not pass for "no
    corrections" either, because the operator believes their instruction is
    in force.
    """
    path = _corrections_path(stem)
    if not path.exists():
        return ""

    try:
        text = path.read_text(encoding="utf-8").strip()
    except Exception as exc:  # noqa: BLE001 - a correction must never break a run
        logger.warning(
            "corrections.read_failed",
            stem=stem,
            path=str(path),
            error=str(exc),
            detail="the file exists and could not be read; its instructions are "
            "NOT in force for this step",
        )
        return ""

    if not text:
        return ""

    if len(text) > _MAX_CORRECTION_CHARS:
        kept = text[:_MAX_CORRECTION_CHARS]
        logger.warning(
            "corrections.truncated",
            stem=stem,
            path=str(path),
            original_chars=len(text),
            kept_chars=_MAX_CORRECTION_CHARS,
        )
        return (
            f"{kept}\n\n[truncated: {len(text)} characters in {path.name}, "
            f"{_MAX_CORRECTION_CHARS} kept. The rest was NOT delivered.]"
        )

    return text


def load_corrections(role_key: str | None) -> str:
    """Return the corrections text for *role_key*, or ``""`` if there is none.

    `_all.md` comes first and the role's own file second, so the specific
    instruction sits nearest the task and wins a conflict by recency.

    Re-read from disk on every call. These are small files and the whole
    value of the feature is that an edit takes effect immediately -- caching
    would reintroduce exactly the latency it exists to remove.
    """
    if not role_key or not _ROLE_RE.match(role_key):
        if role_key:
            logger.warning(
                "corrections.invalid_role_key",
                role_key=role_key,
                detail="not a plain identifier; refusing to build a path from it",
            )
        return ""

    blocks = [b for b in (_read_one(_ALL_ROLES_FILE), _read_one(role_key)) if b]
    if not blocks:
        return ""

    body = "\n\n".join(blocks)
    logger.info("corrections.applied", role=role_key, chars=len(body))
    return f"{_HEADER}\n\n{body}"
