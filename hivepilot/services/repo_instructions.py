"""Resolve + inline a project's declared repository instruction/context
files into the agent prompt.

Root-cause bug this module fixes (inline-repo-instructions PRD): the claude
runner used to emit ``f"Repository instructions file: {claude_md}"`` -- a
bare filename the agent was expected to go fetch itself. In headless
``--print`` mode that is unreliable, and it directly contradicts prompt
contracts that tell agents "the context is ALREADY PROVIDED inline above --
do NOT defer to reading external files". Worse, if the declared file didn't
exist, nothing happened at all: no warning, no error, governance silently
absent.

This module:

1. Resolves every declared file (``ProjectConfig.claude_md`` first, then
   ``ProjectConfig.instruction_files``, in that order, de-duplicated) and
   reads its CONTENT.
2. Never silences a missing/unreadable file -- it logs a structured warning
   (``repo_instructions.missing_file`` / ``.read_failed``) AND injects a
   clearly-marked "NOT FOUND" notice in the prompt itself, so the gap is
   visible in the run transcript an operator actually reads, not only in a
   log line that might be filtered out. This mirrors the codebase's existing
   posture for a missing role prompt file (see
   ``orchestrator._resolve_or_synthesize_role_prompt_file``): warn loudly and
   degrade gracefully rather than hard-failing every run over one optional
   file. Hard-failing was judged too aggressive here -- a project's
   instructions files are advisory context, not a required credential/secret
   whose absence should block execution outright -- but silence (the
   pre-fix behavior) is exactly the bug, so the gap is made impossible to
   miss instead.
3. Bounds size per-file and in total (``Settings.instruction_file_max_bytes``
   / ``.instruction_files_max_total_bytes``), always noting explicitly in the
   injected text when a truncation happened.
4. Redacts resolved content through the SAME choke point every other runner
   output is masked through (``hivepilot.services.config_provenance
   .redact_text``) before it is ever appended to a section list that later
   flows into logs/notifications.
"""

from __future__ import annotations

from pathlib import Path

from hivepilot.config import Settings
from hivepilot.models import ProjectConfig
from hivepilot.services.config_provenance import redact_text
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_SECTION_INTRO = (
    "Repository instructions files (content inlined verbatim below -- this "
    "IS the file content, not a path to go fetch):"
)


def declared_instruction_files(
    claude_md: str | None, instruction_files: list[str] | None
) -> list[str]:
    """Ordered, de-duplicated list of declared instruction file paths:
    ``claude_md`` first (preserves today's single-file behavior/ordering),
    then ``instruction_files``, skipping any exact-string duplicate so a
    config that lists the same file in both fields doesn't inject it twice.

    Public (primitives-only signature, not ``ProjectConfig``) so a caller
    that only has raw/untrusted YAML values -- e.g.
    ``hivepilot.services.config_doctor``'s dangling-instruction-file check,
    which must keep working even for a project entry that fails
    ``ProjectConfig`` validation entirely -- can reuse this EXACT ordering/
    dedup rule instead of reimplementing it and risking disagreement with
    what actually gets inlined into the prompt at run time.
    """
    declared: list[str] = []
    if claude_md:
        declared.append(claude_md)
    for name in instruction_files or []:
        if name not in declared:
            declared.append(name)
    return declared


def _declared_files(project: ProjectConfig) -> list[str]:
    """Thin ``ProjectConfig``-typed wrapper around
    :func:`declared_instruction_files` -- kept so every existing call site in
    this module stays unchanged."""
    return declared_instruction_files(project.claude_md, project.instruction_files)


def resolve_instruction_file_path(project_path: Path, declared: str) -> Path:
    """Resolve *declared* against *project_path*.

    Resolution rule (deliberately simple/explicit, no implicit search):
    - An absolute path is used as-is.
    - A relative path (including one that starts with ``..``) is resolved
      relative to the project's ``path`` -- the same root every other
      project-relative reference (``modules`` subpaths, knowledge files)
      resolves against. This supports the common monorepo/umbrella pattern
      where instruction files live in the PARENT of the product repo (e.g.
      ``claude_md: "../CLAUDE.md"``) without HivePilot ever walking parent
      directories on its own -- the operator states the relationship
      explicitly in config, HivePilot never guesses it.

    Public (not ``_resolve_path``) so `config_doctor`'s dangling-instruction-
    file check calls this SAME function rather than reimplementing
    resolution -- a check that disagrees with the runtime is worse than no
    check.
    """
    candidate = Path(declared).expanduser()
    if candidate.is_absolute():
        return candidate
    return (project_path / candidate).resolve()


# Backward-compatible private alias -- every pre-existing call site inside
# this module keeps working unchanged.
_resolve_path = resolve_instruction_file_path


def _missing_file_block(declared: str, resolved: Path, project_path: Path) -> str:
    logger.warning(
        "repo_instructions.missing_file",
        declared=declared,
        resolved=str(resolved),
        searched_dir=str(resolved.parent),
        project_path=str(project_path),
    )
    return (
        f"--- {declared} (NOT FOUND) ---\n"
        f"WARNING: this repository instructions file was declared but could "
        f"not be found. Resolved path: {resolved}. Searched directory: "
        f"{resolved.parent}. Proceeding WITHOUT this file's content -- do "
        f"NOT assume its governance/instructions were applied.\n"
        f"--- end {declared} ---"
    )


def _unreadable_file_block(declared: str, resolved: Path, error: OSError) -> str:
    logger.warning(
        "repo_instructions.read_failed",
        declared=declared,
        resolved=str(resolved),
        error=str(error),
    )
    return (
        f"--- {declared} (UNREADABLE) ---\n"
        f"WARNING: this repository instructions file exists at {resolved} "
        f"but could not be read ({error}). Proceeding WITHOUT its content.\n"
        f"--- end {declared} ---"
    )


def _skipped_total_cap_block(declared: str, resolved: Path, total_cap: int) -> str:
    logger.warning(
        "repo_instructions.total_cap_reached",
        declared=declared,
        resolved=str(resolved),
        total_cap_bytes=total_cap,
    )
    return (
        f"--- {declared} (SKIPPED) ---\n"
        f"WARNING: the combined repository-instructions size cap "
        f"({total_cap} bytes total) was already reached before this file; "
        f"its content was NOT injected.\n"
        f"--- end {declared} ---"
    )


def build_repo_instructions_section(project: ProjectConfig, settings: Settings) -> str | None:
    """Return the fully-assembled prompt section for *project*'s declared
    instruction/context files, or ``None`` when none are declared.

    Never raises on a missing/unreadable file (see module docstring) --
    those degrade to a clearly-marked in-band warning block instead.
    """
    declared_files = _declared_files(project)
    if not declared_files:
        return None

    per_file_cap = max(0, settings.instruction_file_max_bytes)
    total_cap = max(0, settings.instruction_files_max_total_bytes)
    remaining_total = total_cap

    blocks: list[str] = []
    for declared in declared_files:
        resolved = _resolve_path(project.path, declared)

        if not resolved.is_file():
            blocks.append(_missing_file_block(declared, resolved, project.path))
            continue

        try:
            content = resolved.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            blocks.append(_unreadable_file_block(declared, resolved, exc))
            continue

        if total_cap and remaining_total <= 0:
            blocks.append(_skipped_total_cap_block(declared, resolved, total_cap))
            continue

        truncated = False
        if per_file_cap and len(content) > per_file_cap:
            content = content[:per_file_cap]
            truncated = True
        if total_cap and len(content) > remaining_total:
            content = content[:remaining_total]
            truncated = True
        if total_cap:
            remaining_total -= len(content)

        # Redact through the SAME sink discipline choke point every other
        # runner output is masked through -- a secret pasted into an
        # instructions file must never reach the prompt/logs/notifications
        # verbatim (config_provenance.redact_text only masks values that
        # were resolved as secrets elsewhere in this process, e.g. project
        # env/secrets -- consistent with the rest of the codebase's
        # redaction posture, see claude_runner._call_anthropic_api).
        content = redact_text(content)

        notice = ""
        if truncated:
            notice = (
                f"\n[TRUNCATED: cut short to respect the instruction-file size "
                f"cap ({per_file_cap} bytes/file, {total_cap} bytes total "
                f"across all declared files). The content above this notice "
                f"is a PREFIX of the real file, not the whole file.]"
            )
        blocks.append(f"--- {declared} ---\n{content}{notice}\n--- end {declared} ---")

    if not blocks:
        return None
    return _SECTION_INTRO + "\n\n" + "\n\n".join(blocks)
