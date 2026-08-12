"""Round-trip YAML config writer with prospective cross-reference validation.

Provides the primitives the interactive (`config edit`, Sprint 2) and
non-interactive (`config set`, Sprint 3) commands build on:

  - ``load_roundtrip`` / ``dump_roundtrip``: ruamel.yaml round-trip load/dump
    that preserves comments, key order, and quoting style.
  - ``apply_and_validate``: apply a mutation to a config file, validate the
    *prospective* result via ``validate_config``, and only persist it to disk
    when the mutation keeps the config consistent (and ``dry_run`` is False).
  - ``resolve_reference``: read-only membership check used to validate a
    user-supplied value (role/project/task/prompt_file) before it is written.
  - ``prompt_or_refuse``: TTY-aware interactive picker; returns ``None``
    headlessly instead of blocking on a prompt nobody can answer.

No plain, comment-dropping YAML dump helper is used here — every write goes
through a fresh round-trip ``YAML()`` instance so hand-authored comments and
formatting survive edits made through this module.
"""

from __future__ import annotations

import copy
import difflib
import shutil
import sys
import tempfile
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Callable, Literal

from ruamel.yaml import YAML, YAMLError
from ruamel.yaml.comments import CommentedMap

from hivepilot.config import settings
from hivepilot.services.config_validation import validate_config

# Files validate_config() reads when computing cross-reference problems.
# Mirrors config_validation.validate_config's `required_files`.
_REQUIRED_CONFIG_FILES = (
    "projects.yaml",
    "roles.yaml",
    "policies.yaml",
    "groups.yaml",
    "pipelines.yaml",
    "tasks.yaml",
)

ReferenceKind = Literal["role", "project", "task", "prompt_file"]


@dataclass(frozen=True)
class WriteResult:
    """Outcome of an `apply_and_validate` call."""

    diff: str
    errors: list[str]
    written: bool


def _yaml() -> YAML:
    """Return a fresh round-trip YAML() instance (never shared/mutated)."""
    y = YAML()  # default typ="rt" — round-trip: preserves comments + key order
    y.preserve_quotes = True
    y.width = 4096  # avoid re-wrapping long lines on dump (keeps diffs minimal)
    # Match this project's YAML convention (see roles.yaml, projects.yaml, …):
    # sequence items indented 2 under their parent key, with a further 2-space
    # offset before the dash — e.g. "roles:\n  - name: ceo\n    title: CEO".
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def load_roundtrip(path: Path) -> CommentedMap:
    """Load a YAML file preserving comments, key order, and quote style.

    Raises FileNotFoundError if *path* does not exist (mirrors `open()`).
    """
    with path.open("r", encoding="utf-8") as handle:
        data = _yaml().load(handle)
    return data if data is not None else CommentedMap()


def dump_roundtrip(data: object, path: Path) -> None:
    """Write *data* to *path* via the round-trip YAML dumper."""
    with path.open("w", encoding="utf-8") as handle:
        _yaml().dump(data, handle)


def _dump_roundtrip_to_string(data: object) -> str:
    buf = StringIO()
    _yaml().dump(data, buf)
    return buf.getvalue()


def _resolve_real_path(file: str, base_dir: Path | None) -> tuple[Path, Path]:
    """Resolve (real_path, containing_dir) for *file*.

    Mirrors `validate_config`'s own base_dir semantics: an explicit base_dir
    is joined directly; omitting it falls back to the XDG-aware
    `settings.resolve_config_path` chain (the same file the rest of the app
    reads and writes).
    """
    if base_dir is not None:
        return base_dir / file, base_dir
    real_path = settings.resolve_config_path(file)
    return real_path, real_path.parent


def _scratch_asset_sources(containing_dir: Path) -> list[Path]:
    """Roots to copy `plugins/` and `skills/` from, in priority order.

    `containing_dir` first, because a self-contained config directory is the
    common case and must keep behaving exactly as before.

    Then the RESOLVED search roots. Populating only from siblings assumes a
    single-directory layout; production is split -- config in XDG_CONFIG,
    plugins in XDG_DATA, skills in the config-repo clone -- so the siblings do
    not exist, the scratch copy is empty, and every `skills:` reference
    false-positives. `hivepilot project add` refused every write on that box.

    The existing tests all build a single-directory layout, so they validated
    the assumption instead of testing it.
    """
    roots: list[Path] = [containing_dir]
    try:
        from hivepilot.config import settings

        for candidate in (
            settings.xdg_data_home,
            settings.xdg_config_home,
            settings._config_repo_local_path(),
            settings.base_dir,
        ):
            if candidate and Path(candidate) not in roots:
                roots.append(Path(candidate))
    except Exception:  # noqa: BLE001 - a wider search must never break the write gate
        pass
    return roots


def _validate_prospective(file: str, mutated_text: str, containing_dir: Path) -> list[str]:
    """Run validate_config() against a scratch copy of containing_dir with
    *file* replaced by *mutated_text*, so a bad mutation is caught before it
    ever touches the real files."""
    if file not in _REQUIRED_CONFIG_FILES:
        # validate_config() only reads the 6 core config files, so a file
        # outside that set (e.g. model_profiles.yaml) would otherwise never
        # be parse-checked. At minimum, confirm the mutated text still
        # parses as valid YAML before it is allowed through the write gate.
        try:
            _yaml().load(StringIO(mutated_text))
        except YAMLError as exc:
            return [f"Failed to parse mutated {file}: {exc}"]
        return []

    with tempfile.TemporaryDirectory(prefix="hivepilot-config-writer-") as tmp:
        tmp_dir = Path(tmp)
        for name in _REQUIRED_CONFIG_FILES:
            src = containing_dir / name
            if src.exists():
                shutil.copy2(src, tmp_dir / name)
        prompts_src = containing_dir / "prompts"
        if prompts_src.exists():
            shutil.copytree(prompts_src, tmp_dir / "prompts")
        # `validate_config`'s `skills:` cross-reference check now scans
        # plugins/skills from the SAME isolated `base_dir` it validates the
        # config files from (the `hivepilot validate --dir` cwd bug fix —
        # plugin/skill discovery is no longer merged with the process-global
        # `settings.base_dir`). Without copying `plugins/` into the scratch
        # copy too, a `skills:` reference that's genuinely registered in the
        # real, active config would false-positive as "unknown skill" here,
        # purely because the scratch dir doesn't have its own plugins/ --
        # mirrors the `prompts/` copy immediately above for the same reason.
        # First root that actually has the directory wins -- see
        # `_scratch_asset_sources` for why siblings alone are not enough.
        for root in _scratch_asset_sources(containing_dir):
            plugins_src = root / "plugins"
            if plugins_src.exists():
                shutil.copytree(plugins_src, tmp_dir / "plugins")
                break
        # Same reason, for the OTHER skill source: `skills/<name>/SKILL.md`
        # directories (`hivepilot/skill_dirs.py`). A config dir that ships its
        # skills as directories (the config-repo case) would otherwise have
        # every one of its `skills:` references false-positive as "unknown
        # skill" in the scratch copy, blocking every `config set`/`config
        # edit` write against that directory. `symlinks=True`: copying links
        # AS links keeps a directory-escaping symlink escaping, so
        # `skill_dirs`' containment check still rejects it here instead of
        # `copytree` dereferencing it into a real in-scratch file and
        # silently validating something the live tree would refuse.
        for root in _scratch_asset_sources(containing_dir):
            skills_src = root / "skills"
            if skills_src.exists():
                shutil.copytree(skills_src, tmp_dir / "skills", symlinks=True)
                break

        (tmp_dir / file).write_text(mutated_text, encoding="utf-8")

        try:
            return validate_config(base_dir=tmp_dir)
        except ValueError as exc:
            # _load() in config_validation raises ValueError on a YAML parse
            # error — surface it as a problem entry instead of crashing.
            return [str(exc)]


def apply_and_validate(
    file: str,
    mutate: Callable[[CommentedMap], CommentedMap],
    *,
    dry_run: bool,
    base_dir: Path | None,
) -> WriteResult:
    """Apply *mutate* to *file*'s parsed content and validate the prospective
    result before writing.

    The mutation NEVER touches the on-disk file (or the caller's loaded map)
    directly: it runs against a deep copy, gets validated against a scratch
    copy of the whole config directory, and is only persisted to the real
    path when `validate_config` reports zero problems and `dry_run` is
    False. A failed validation (or `dry_run=True`) leaves the real file
    byte-identical to how it started.
    """
    real_path, containing_dir = _resolve_real_path(file, base_dir)

    if real_path.exists():
        original_text = real_path.read_text(encoding="utf-8")
        try:
            original_map = load_roundtrip(real_path)
        except YAMLError as exc:
            # The file already on disk is corrupt — surface it as an error
            # entry instead of letting the parse error crash the command.
            return WriteResult(
                diff="", errors=[f"Failed to parse {real_path}: {exc}"], written=False
            )
    else:
        original_text = ""
        original_map = CommentedMap()

    mutated_map = mutate(copy.deepcopy(original_map))
    mutated_text = _dump_roundtrip_to_string(mutated_map)

    diff = "".join(
        difflib.unified_diff(
            original_text.splitlines(keepends=True),
            mutated_text.splitlines(keepends=True),
            fromfile=str(real_path),
            tofile=f"{real_path} (proposed)",
        )
    )

    errors = _validate_prospective(file, mutated_text, containing_dir)

    written = False
    if not errors and not dry_run:
        dump_roundtrip(mutated_map, real_path)
        written = True

    return WriteResult(diff=diff, errors=errors, written=written)


def resolve_reference(kind: ReferenceKind, value: str) -> bool:
    """Read-only membership check for a user-supplied reference value."""
    if kind == "role":
        from hivepilot.roles import load_roles

        return value in load_roles()
    if kind == "project":
        from hivepilot.services.project_service import load_projects

        return value in load_projects().projects
    if kind == "task":
        from hivepilot.services.project_service import load_tasks

        return value in load_tasks().tasks
    if kind == "prompt_file":
        prompts_dir = settings.resolve_config_path("prompts") / "agents"
        return (prompts_dir / value).exists()
    raise ValueError(f"Unknown reference kind: {kind!r}")


def prompt_or_refuse(valid: list[str], label: str) -> str | None:
    """Interactively prompt for one of *valid* choices when attached to a
    TTY; otherwise refuse (return None) rather than block on a prompt no one
    can answer. `questionary` is only imported inside the TTY branch."""
    if not sys.stdin.isatty():
        return None

    import questionary

    return questionary.select(label, choices=valid).ask()
