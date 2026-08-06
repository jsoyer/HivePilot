"""Directory-sourced skills: `<root>/skills/<name>/SKILL.md`.

Why this module exists
----------------------
Before it, a skill could ONLY reach the engine as a plugin contribution
(`register()["skills"] = [SkillSpec, ...]`, with every file's content inlined
in the spec's `files` dict -- see `hivepilot/plugins.py`). There was no
filesystem skill discovery of any kind, so a config repo that ships its
skills the way the wider agent ecosystem writes them -- one directory per
skill, each with a `SKILL.md` -- was invisible to the engine: every
`skills:` reference to them failed the fail-closed cross-reference check in
`config_validation` as `references unknown skill '<name>'`, and the search
path printed in that error named only the PLUGIN roots, sending the operator
down a false trail.

This module is the missing SOURCE, deliberately built as a search TIER (the
config repo stays the single source of truth; nothing is ever copied, so no
copy can go stale) rather than as a new step in `config_service.sync()`.
That mirrors how role prompts already resolve (`hivepilot/roles.py`:
XDG -> config_repo -> base_dir) and how config-repo PLUGINS already resolve
(`plugins._config_repo_plugins_dir`) -- no third mechanism is invented here.

Trust model (identical to the sibling `plugins/` path, never weaker)
--------------------------------------------------------------------
`skill_scan_dirs` mirrors `plugins.plugin_scan_dirs` root for root and gate
for gate:

* `settings.plugins_enabled` (master switch) -- off means NO roots at all,
  exactly as it means no plugin roots at all.
* The config-repo root is gated behind BOTH `settings.config_repo` (set) AND
  `settings.config_repo_load_plugins` (opt-out, default True) -- the very
  same pair that decides whether the config repo's `plugins/` directory is
  scanned. An operator who has never configured a config repo, or who turned
  that flag off, sees zero behavior change.
* A directory skill can never SHADOW a plugin-contributed skill: the staging
  in `plugins.PluginManager._load_into` registers plugin skills first and
  skips (loudly) any directory skill whose name is already taken. So a config
  repo cannot replace, and cannot collide-crash, an installed plugin's skill.

What a config repo can therefore now cause to load that it could not before:
UTF-8 TEXT files (a `SKILL.md` plus whatever it references), which a runner
materialises into an ephemeral scratch dir and may append to a system prompt.
It cannot cause new Python to be imported or executed -- that remains the
exclusive privilege of the `plugins/` path, which was already gated by these
same two switches.

Fail-closed rules (a rejected skill stays UNREGISTERED, so the existing
`dangling_reference` error still fires loudly -- the check is never loosened):

* No `SKILL.md` -> not a skill directory.
* Unsafe name (path separators, leading dot, ...) -> rejected.
* A file that escapes the skill directory via symlink -> whole skill rejected.
* Non-UTF-8 content, a file over `MAX_SKILL_FILE_BYTES`, or more than
  `MAX_SKILL_FILES` files -> whole skill rejected.
* Malformed frontmatter, a `name:` disagreeing with the directory name, or a
  `min_role` that is not a recognised `token_service.ROLE_RANKS` value ->
  whole skill rejected (never stored with an unenforceable gate, which a
  `-1` rank would silently invert to fail-open).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger
from hivepilot.utils.terminal import strip_control_chars

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hivepilot.plugins import SkillSpec

logger = get_logger(__name__)

# The manifest every skill directory must contain to be recognised as one --
# the de-facto convention across the agent-skill ecosystem, and the guard that
# keeps an unrelated directory that merely happens to sit under `skills/` from
# being slurped in as a skill.
SKILL_MANIFEST = "SKILL.md"

# Bounded reads: a skill's files are held in memory and later materialised
# into the runner's scratch dir, so an accidentally-committed artifact must
# not become an unbounded read. Exceeding either limit rejects the SKILL
# (loudly) rather than silently truncating it.
MAX_SKILL_FILE_BYTES = 1_048_576
MAX_SKILL_FILES = 200

# Directory name == skill name, so it must be a plain, single-segment,
# reference-able identifier.
_SKILL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\r?\n(?P<body>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)

# Optional frontmatter keys copied onto the SkillSpec. `provider` is
# deliberately NOT accepted from frontmatter: it is attribution ("where did
# this come from"), so it is always derived from the real on-disk path.
_OPTIONAL_TEXT_KEYS = ("system_prompt",)


# `description` is free-form text read off disk and rendered straight into a
# terminal table by `hivepilot skills list`. Bound it and strip anything that
# could move the cursor, emit ANSI, or break the table across lines (via the
# shared `hivepilot.utils.terminal.strip_control_chars` helper).
_MAX_DESCRIPTION_CHARS = 300


class SkillDirectoryError(RuntimeError):
    """A skill directory exists but is not usable. Caught by the discovery
    loop, logged, and the skill is left UNREGISTERED (so the config
    cross-reference check still reports it as dangling)."""


def _config_repo_skill_roots() -> list[Path]:
    """Every `skills/` directory belonging to the operator's config repo, in
    priority order -- or an empty list when config-repo auto-loading is off.

    Gated behind BOTH `settings.config_repo` AND
    `settings.config_repo_load_plugins`, exactly like
    `plugins._config_repo_plugins_dir` (see this module's docstring).

    Two roots because this codebase already has two established notions of
    "the config repo", and both are honoured elsewhere:

    1. the CLONE at `config_service._config_dir()`
       (`xdg_data_home/config-repo`) -- what `config sync` maintains and what
       `plugins._config_repo_plugins_dir` reads;
    2. `config_repo` given as a plain LOCAL DIRECTORY -- the form
       `Settings.resolve_config_path`/`config_path_search_dirs` honour as
       their tier 2 (the documented `/srv/config` deployment).

    Lazy, function-local import of `config_service._config_dir` (the single
    source of truth for where the clone lives) to avoid a module-load-time
    dependency, mirroring the lazy-import pattern in `plugins.py`.
    """
    if not settings.config_repo or not settings.config_repo_load_plugins:
        return []

    from hivepilot.services.config_service import _config_dir

    roots: list[Path] = []
    seen: set[Path] = set()
    for candidate in (_config_dir(), settings._config_repo_local_path()):
        if candidate is None:
            continue
        skills_root = candidate / "skills"
        if not skills_root.is_dir():
            continue
        resolved = skills_root.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        roots.append(skills_root)
    return roots


def skill_scan_dirs(base_dir: Path | None = None) -> list[Path]:
    """Return, in scan order, every directory `discover_directory_skills`
    looks in for `<name>/SKILL.md` skill directories.

    Pure path arithmetic apart from the existence checks -- no file is read
    here, so `hivepilot validate`'s header and the "searched: ..." list in an
    unknown-skill error can name these roots without doing any discovery.
    Callers MUST use this to build that list: an error message that names
    fewer roots than were really consulted is how the original bug stayed
    invisible for so long.

    Two modes, matching `plugins.plugin_scan_dirs` exactly:

    - `base_dir` omitted (`None`, the default): `settings.base_dir/skills`
      first (the cwd-derived tier), then the config repo's `skills/` (when
      auto-loading is enabled), then `xdg_data_home/skills` (the managed
      tier, sibling of the `plugins install` destination). Existence-gated
      for every tier except the first, which is always listed so the operator
      can see it was considered.
    - `base_dir` given explicitly: ISOLATED-directory mode
      (`hivepilot validate --dir`, `config_writer`'s scratch copy) -- ONLY
      `base_dir/skills`, never the process's ambient config repo or managed
      dir, whose state is unrelated to the directory being checked and would
      silently change the verdict.
    """
    if not settings.plugins_enabled:
        return []

    if base_dir is not None:
        return [base_dir / "skills"]

    first = settings.base_dir / "skills"
    dirs: list[Path] = [first]
    seen = {first.resolve()}

    for root in [*_config_repo_skill_roots(), settings.xdg_data_home / "skills"]:
        if not root.is_dir():
            continue
        resolved = root.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        dirs.append(root)
    return dirs


def _parse_frontmatter(text: str) -> dict[str, Any]:
    """Parse the optional leading `---` YAML frontmatter block of a SKILL.md.

    Returns `{}` when there is no frontmatter at all. Raises
    `SkillDirectoryError` when a block IS present but is malformed or is not
    a mapping -- a skill whose metadata cannot be trusted is refused rather
    than registered with silently-dropped metadata (a dropped `min_role`
    would be a fail-open gate).
    """
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        return {}
    try:
        parsed = yaml.safe_load(match.group("body"))
    except yaml.YAMLError as exc:
        raise SkillDirectoryError(f"malformed {SKILL_MANIFEST} frontmatter: {exc}") from exc
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise SkillDirectoryError(
            f"{SKILL_MANIFEST} frontmatter must be a mapping, got {type(parsed).__name__}"
        )
    return parsed


def _sanitize_description(value: object, name: str) -> str:
    """Return a single-line, control-character-free, bounded description.

    Falls back to a generated one when the frontmatter has none (or an
    unusable one). Sanitised rather than rejected: a cosmetic field must not
    be able to make an otherwise-valid skill vanish, but it also must not be
    able to emit ANSI/cursor escapes into the operator's terminal via
    `hivepilot skills list`.
    """
    if not isinstance(value, str):
        return f"Skill directory {name}"
    cleaned = strip_control_chars(value, replacement=" ")
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return f"Skill directory {name}"
    if len(cleaned) > _MAX_DESCRIPTION_CHARS:
        cleaned = cleaned[: _MAX_DESCRIPTION_CHARS - 1].rstrip() + "…"
    return cleaned


def _read_skill_files(skill_dir: Path) -> dict[str, str]:
    """Read every file under *skill_dir* into a `relpath -> content` dict.

    Skips dot-prefixed path segments (`.git/`, editor droppings). Rejects the
    whole skill -- never just the offending file -- on a symlink that escapes
    the directory, a non-UTF-8 file, an oversized file, or too many files.
    """
    root = skill_dir.resolve()
    files: dict[str, str] = {}
    for path in sorted(skill_dir.rglob("*")):
        rel = path.relative_to(skill_dir)
        if any(part.startswith(".") for part in rel.parts):
            continue
        if not path.is_file():
            continue
        # Symlink containment: the same rule `ClaudeRunner.apply_skill`
        # applies when materialising, enforced here at READ time so a config
        # repo can never use a symlink to pull an arbitrary host file (an SSH
        # key, a .env) into a skill and thus into an agent's prompt context.
        if not path.resolve().is_relative_to(root):
            raise SkillDirectoryError(f"file escapes the skill directory: {rel.as_posix()}")
        size = path.stat().st_size
        if size > MAX_SKILL_FILE_BYTES:
            raise SkillDirectoryError(
                f"file {rel.as_posix()} is {size} bytes, over the "
                f"{MAX_SKILL_FILE_BYTES}-byte per-file limit"
            )
        try:
            files[rel.as_posix()] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise SkillDirectoryError(f"file {rel.as_posix()} is not valid UTF-8: {exc}") from exc
        if len(files) > MAX_SKILL_FILES:
            raise SkillDirectoryError(f"more than {MAX_SKILL_FILES} files")
    return files


def _load_skill_dir(skill_dir: Path) -> SkillSpec:
    """Build one `SkillSpec` from a skill directory, or raise
    `SkillDirectoryError` (never return a partially-valid spec)."""
    name = skill_dir.name
    if not _SKILL_NAME_RE.match(name):
        raise SkillDirectoryError(f"invalid skill directory name {name!r}")

    if not (skill_dir / SKILL_MANIFEST).is_file():
        raise SkillDirectoryError(f"no {SKILL_MANIFEST}")

    files = _read_skill_files(skill_dir)
    manifest_text = files.get(SKILL_MANIFEST)
    if manifest_text is None:  # pragma: no cover - defensive; is_file() checked above
        raise SkillDirectoryError(f"no readable {SKILL_MANIFEST}")

    meta = _parse_frontmatter(manifest_text)

    declared_name = meta.get("name")
    if declared_name is not None and declared_name != name:
        # The directory name is authoritative (it is what config references).
        # A disagreeing `name:` is an authoring mistake that would otherwise
        # register the skill under a name nobody expects -- refuse it.
        raise SkillDirectoryError(
            f"{SKILL_MANIFEST} declares name {declared_name!r} but lives in directory {name!r}"
        )

    skill: SkillSpec = {
        "name": name,
        "description": _sanitize_description(meta.get("description"), name),
        "provider": f"directory:{skill_dir.resolve()}",
        "files": files,
    }

    for key in _OPTIONAL_TEXT_KEYS:
        value = meta.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise SkillDirectoryError(f"{key!r} must be a string, got {type(value).__name__}")
        skill[key] = value  # type: ignore[literal-required]

    applies_to = meta.get("applies_to")
    if applies_to is not None:
        if not isinstance(applies_to, list) or not all(isinstance(k, str) for k in applies_to):
            raise SkillDirectoryError("'applies_to' must be a list of runner-kind strings")
        skill["applies_to"] = list(applies_to)

    min_role = meta.get("min_role")
    if min_role is not None:
        from hivepilot.services import token_service

        # isinstance() FIRST so a non-string/non-hashable value can never
        # raise a raw TypeError on the membership check. Fail-closed exactly
        # like `plugins.SkillInvalidMinRoleError`: an unrecognised role is
        # never stored, because `role_rank()` would return -1 and invert the
        # comparison that is supposed to gate the skill.
        if not isinstance(min_role, str) or min_role not in token_service.ROLE_RANKS:
            raise SkillDirectoryError(
                f"invalid min_role {min_role!r}; must be one of {sorted(token_service.ROLE_RANKS)}"
            )
        skill["min_role"] = min_role

    return skill


def discover_directory_skills(base_dir: Path | None = None) -> list[SkillSpec]:
    """Every skill discovered under `skill_scan_dirs(base_dir=base_dir)`.

    First root wins on a duplicate name -- the same first-wins precedence
    `plugins._scan_plugin_dir` applies to duplicate plugin stems, never a
    hard error, so an operator can legitimately override a config-repo skill
    from an earlier tier. A directory that cannot be loaded is logged at
    WARNING and skipped, leaving the name unregistered so the config
    cross-reference check still reports it as dangling.
    """
    found: dict[str, SkillSpec] = {}
    for root in skill_scan_dirs(base_dir=base_dir):
        if not root.is_dir():
            continue
        for skill_dir in sorted(root.iterdir()):
            if not skill_dir.is_dir() or skill_dir.name.startswith("."):
                continue
            if skill_dir.name in found:
                logger.debug(
                    "skills.directory.shadowed",
                    skill=skill_dir.name,
                    ignored=str(skill_dir),
                )
                continue
            try:
                found[skill_dir.name] = _load_skill_dir(skill_dir)
            except SkillDirectoryError as exc:
                logger.warning(
                    "skills.directory.rejected",
                    skill=skill_dir.name,
                    path=str(skill_dir),
                    reason=str(exc),
                )
            except OSError as exc:  # unreadable file/dir -- never kill the load
                logger.warning(
                    "skills.directory.unreadable",
                    skill=skill_dir.name,
                    path=str(skill_dir),
                    error=str(exc),
                )
    return [found[name] for name in sorted(found)]
