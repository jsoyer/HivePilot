"""Shared PR/MR body resolution for every `ForgeProvider` (github/forgejo/
gitlab) -- the single choke point `git_service.create_pr` calls through
before delegating to `resolve_forge(project).open_pr`.

`GitActions.pr_body_file` is a task-config-DECLARED filename (see
`docs/CONFIGURATION.md`) that the engine itself never writes -- it exists so
an agent stage can hand the forge a richer PR description as a side effect
of its own work. Before this module existed, nothing verified the declared
file actually existed before it reached a forge:

- `GitHubForge.open_pr` passed it straight to `gh pr create --body-file
  <path>`, resolved against the subprocess `cwd` (the project's working
  directory) -- a missing file makes `gh` fail, `subprocess.run(check=True)`
  raises, and that propagates all the way up through `create_pr` ->
  `perform_git_actions` -> the pipeline stage loop, LOSING the stage's
  entire (already fully-paid-for) output.
- `ForgejoForge`/`GitLabForge.open_pr` called `Path(git.pr_body_file).
  read_text()` directly -- an unhandled `FileNotFoundError` for a missing
  file, AND (a separate, sibling bug) a *relative* `pr_body_file` resolves
  against the ORCHESTRATOR PROCESS's cwd rather than the project's own
  working directory, since there is no `cwd=` argument the way there is for
  the `gh` subprocess call.

`resolve_pr_body_file` (a context manager) closes every one of those holes
in ONE place, so no forge implementation needs to duplicate this logic:

- A present, non-blank file (resolved against `base_dir` -- the project's
  own working directory, never the process cwd) is used byte-for-byte
  unchanged -- this is the `pr_body_file` feature working exactly as
  documented, and behaviour here is a strict no-op.
- A declared-but-missing/blank file degrades GRACEFULLY: a redacted,
  size-capped fallback body is materialized from whatever meaningful
  content the engine already has for this stage (the stage's own output,
  then the configured PR title / commit message, then a generic message) --
  the PR still opens, it just doesn't get the intended custom body.
- That degradation is never silent: a WARNING names the declared file, the
  directory searched, and what was substituted instead -- proceeding
  without a promised body must be visible, unlike a security/authz gate
  where an absent value should instead deny (see the empty-value fail-open
  lesson this codebase applies elsewhere -- this is the deliberate inverse:
  proceed, but say so loudly).
- The caller ALWAYS receives a valid, existing, absolute `Path` -- so every
  forge's `--body-file`/`Path(...).read_text()` call site keeps working
  completely unchanged; this module normalizes the input, not the forges.
"""

from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from hivepilot.models import GitActions
from hivepilot.services.config_provenance import redact_text
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

DEFAULT_PR_BODY = "Automated pull request opened by HivePilot."

# GitHub/GitLab/Forgejo all cap PR/MR body sizes well above this (GitHub's
# limit is 65536 characters) -- capping well under every known forge limit
# means the fallback body can NEVER itself be the reason `open_pr` fails,
# and leaves headroom for the in-band truncation notice appended below.
_MAX_FALLBACK_CHARS = 60_000

_TEMP_FILE_PREFIX = "hivepilot-pr-body-fallback-"


def _resolve_declared_path(base_dir: Path, declared: str) -> Path:
    """Resolve *declared* against *base_dir* -- the project's OWN working
    directory -- regardless of the calling process's cwd. An already-
    absolute *declared* path is returned unchanged."""
    candidate = Path(declared)
    return candidate if candidate.is_absolute() else base_dir / candidate


def _read_if_nonblank(path: Path) -> str | None:
    """Return *path*'s text content if it exists, is a regular file, is
    readable, AND is non-blank once stripped -- `None` for every other case
    (missing, unreadable, or whitespace-only), which the caller treats
    identically: "no usable declared body"."""
    if not path.is_file():
        return None
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning(
            "git.pr_body_file_unreadable",
            resolved_path=str(path),
            error=str(exc),
        )
        return None
    return content if content.strip() else None


def _build_fallback_body(git: GitActions, fallback_text: str | None) -> tuple[str, str]:
    """Build the fallback PR/MR body text plus a short label describing what
    was substituted (used in the warning log), in priority order:

    1. The stage's own output (`fallback_text` -- the richest, most specific
       content the engine has for THIS run).
    2. The task's own configured `pr_title` / `commit_message` (still more
       meaningful than a generic placeholder).
    3. A generic default message.

    Whatever is chosen may contain agent-produced text, so it is always
    routed through `redact_text` (the established sink discipline for
    exactly this class of content -- see `orchestrator.py`'s `RunResult`
    choke point) and capped to `_MAX_FALLBACK_CHARS`, with the truncation
    stated in-band rather than silently dropped.
    """
    stage_output = (fallback_text or "").strip()
    if stage_output:
        text, substituted = stage_output, "stage output"
    else:
        metadata_parts = [p.strip() for p in (git.pr_title, git.commit_message) if p and p.strip()]
        metadata_text = " — ".join(metadata_parts)
        if metadata_text:
            text, substituted = metadata_text, "task git metadata (pr_title/commit_message)"
        else:
            return DEFAULT_PR_BODY, "default message"

    text = redact_text(text)
    if len(text) > _MAX_FALLBACK_CHARS:
        omitted = len(text) - _MAX_FALLBACK_CHARS
        text = (
            text[:_MAX_FALLBACK_CHARS].rstrip()
            + f"\n\n...[truncated by HivePilot: {omitted} more characters omitted]"
        )
    return text, substituted


@contextmanager
def resolve_pr_body_file(
    *, base_dir: Path, git: GitActions, fallback_text: str | None
) -> Iterator[Path | None]:
    """Yield a valid, existing, absolute `Path` to use as a forge's
    `--body-file`/`Path(...).read_text()` PR-body argument, or `None` when
    `git.pr_body_file` was never declared -- in that case every forge
    already has its own correct, tested "no custom body" default (a literal
    `--body <generic message>` for github, an inline default string for
    forgejo/gitlab), and this function deliberately does NOT touch that path
    at all, so undeclared-`pr_body_file` behaviour stays byte-identical.

    This function only activates once a file WAS declared:

    - Resolves (against `base_dir` -- the project's own working directory,
      never the calling process's cwd) to an existing, non-blank file:
      yields that file's own path unchanged -- it is NEVER deleted on exit
      (it belongs to the agent's own working tree, not to this function).
    - Otherwise (missing or blank): materializes a redacted, size-capped
      fallback body (see `_build_fallback_body`) into a fresh temp file,
      logs a WARNING naming the missing file, the directory searched, and
      what was substituted (silence is never acceptable once a file WAS
      promised), yields that temp file's path, and deletes it on exit.
    """
    declared = git.pr_body_file
    if not declared:
        yield None
        return

    candidate = _resolve_declared_path(base_dir, declared)
    content = _read_if_nonblank(candidate)
    if content is not None:
        yield candidate
        return

    fallback_body, substituted = _build_fallback_body(git, fallback_text)
    # A file WAS promised (via task/pipeline config) but couldn't be used --
    # this must be visible, never a silent proceed.
    logger.warning(
        "git.pr_body_file_missing_or_empty",
        declared_file=declared,
        searched_path=str(candidate),
        searched_dir=str(base_dir),
        substituted=substituted,
    )

    fd, tmp_name = tempfile.mkstemp(prefix=_TEMP_FILE_PREFIX, suffix=".md")
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "w", encoding="utf-8") as fh:
            fh.write(fallback_body)
        yield tmp_path
    finally:
        tmp_path.unlink(missing_ok=True)
