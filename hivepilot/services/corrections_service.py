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
from datetime import date
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
#
# 4 000, not 20 000: this file rides in the STABLE PREFIX of every call the
# role makes, so it is billed on each one -- 20 000 characters is roughly
# 5 000 tokens on every single call, forever. A cap that only guards against
# the argv limit is set for the wrong failure; the binding constraint is that
# corrections are a recurring cost, not a one-off payload.
#
# The bound is also what forces the promote-or-delete rule to be used: a file
# that can grow without limit never gets pruned.
_MAX_CORRECTION_CHARS = 4_000

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


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

#: Header written into a corrections file the first time one is created.
#: The promote-or-delete rule lives HERE, not only in a README, because this
#: is the file someone actually opens when it has grown too long.
_FILE_PREAMBLE = (
    "# Standing corrections\n\n"
    "One rule per entry, newest last. These are injected verbatim into every\n"
    "call this role makes, so they are billed on each one.\n\n"
    "**Promote or delete.** A correction that has held for a while belongs in\n"
    "the role's prompt, not here; one that stopped being true should be\n"
    "deleted. Nothing in this file is meant to live forever -- the size cap\n"
    "exists so that pruning happens on a schedule rather than never.\n"
)


class CorrectionTooLarge(RuntimeError):
    """Appending would push the file past `_MAX_CORRECTION_CHARS`.

    Raised instead of truncating. On READ, truncation is a last-resort safety
    net and it declares the cut in the text the agent sees. On WRITE there is
    a caller who can be told no, and half a rule stored forever is worse than
    a refused write.
    """


def _write_path(role_key: str) -> Path:
    """Where a correction is WRITTEN.

    With a config repo configured this is the clone, so the entry can be
    committed and reaches every host. Without one it falls back to wherever
    the read chain resolves, so a local-only deployment can still record a
    correction instead of writing it somewhere nothing will ever read.

    A write has exactly one destination either way -- picking the read chain
    unconditionally would let an entry land on an XDG copy that is never
    committed and silently diverges.
    """
    if settings.config_repo:
        from hivepilot.services import config_service

        return config_service._config_dir() / "prompts" / "corrections" / f"{role_key}.md"
    return _corrections_path(role_key)


def append_correction(
    role_key: str,
    text: str,
    *,
    commit: bool = True,
    author: str = "agent",
) -> Path:
    """Append one dated correction for *role_key* and (by default) commit it.

    The operator chose to let the agent write these directly rather than
    propose them for review, so the safeguards replace that review:

    - the entry is DATED, so promote-or-delete is mechanical;
    - the file is bounded and this REFUSES rather than truncates;
    - the commit touches exactly one path (the config-repo clone routinely
      has unrelated dirt in it, and `config_service.push` would sweep it up
      with `git add -A`);
    - the text is redacted, because a correction is often written straight
      out of an error message.

    Returns the written path. Raises `ValueError` for an unusable role or
    empty text, and `CorrectionTooLarge` when the bound would be exceeded.
    """
    role_key = (role_key or "").strip()
    if not role_key or not _ROLE_RE.match(role_key):
        raise ValueError(f"unusable role key for a corrections file: {role_key!r}")

    from hivepilot.services.config_provenance import redact_text

    body = redact_text((text or "").strip())
    if not body:
        raise ValueError("refusing to record an empty correction")

    path = _write_path(role_key)
    existing = ""
    if path.exists():
        try:
            existing = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError(f"cannot read {path}: {exc}") from exc
    else:
        existing = _FILE_PREAMBLE

    entry = f"\n- {date.today().isoformat()} ({author}) {body}\n"
    updated = existing.rstrip("\n") + "\n" + entry

    if len(updated) > _MAX_CORRECTION_CHARS:
        raise CorrectionTooLarge(
            f"{path.name} would reach {len(updated)} chars, over the "
            f"{_MAX_CORRECTION_CHARS} cap. Promote a standing rule into the role "
            f"prompt or delete one that no longer holds, then retry."
        )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    logger.info(
        "corrections.appended",
        role=role_key,
        author=author,
        chars=len(updated),
        path=str(path),
    )

    if commit and settings.config_repo:
        _commit_one(path, role_key, body)
    return path


def _commit_one(path: Path, role_key: str, body: str) -> None:
    """Commit and push exactly *path*.

    Deliberately narrow. An agent-initiated commit that nobody reviews must
    not carry anything the author did not intend, and the clone is routinely
    dirty with untracked run output.
    """
    from git import GitCommandError, Repo

    from hivepilot.services import config_service

    try:
        repo = Repo(config_service._config_dir())
    except Exception as exc:  # noqa: BLE001 - git layout problems are not fatal here
        logger.warning("corrections.commit_skipped", error=str(exc))
        return

    summary = body.splitlines()[0][:60]
    message = (
        f"chore(corrections): {role_key} -- {summary}\n\n"
        "Written by an agent, unreviewed by design (operator decision).\n"
        "Revert this commit to drop the rule."
    )
    try:
        repo.git.add("--", str(path))
        repo.git.commit("-m", message)
        repo.git.push("origin", settings.config_branch)
        logger.info("corrections.committed", role=role_key)
    except GitCommandError as exc:
        # A correction that is on disk but unpushed still applies locally; the
        # loud failure belongs to whoever runs `config status`, not to the run
        # that produced the correction.
        logger.warning("corrections.push_failed", role=role_key, error=str(exc))
