"""Onboarding wizard logic for `hivepilot init`.

Two modes, both reusing existing machinery rather than reimplementing it:

- SCAFFOLD (no existing config repo): write minimal valid placeholder config
  files locally. Reuses the content already authored in
  ``hivepilot.scaffold.templates`` for the six core files, and adds three
  more minimal templates (model_profiles.yaml, schedules.yaml,
  api_tokens.yaml) that scaffold_config's existing set didn't cover.
- CLONE (existing config repo): seeds HIVEPILOT_CONFIG_REPO into the target
  ``.env`` and delegates the actual git clone/pull to
  ``hivepilot.services.config_service.sync`` — the ONLY git interaction this
  module performs.

Interactive prompting (questionary) intentionally lives in the CLI layer
(``hivepilot.cli.init_config``), not here — every function below is a plain,
TTY-independent unit that can be exercised directly in tests.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import yaml

from hivepilot.config import settings
from hivepilot.scaffold.templates import _FILES as _BASE_SCAFFOLD_FILES
from hivepilot.services.agent_checks import MandatoryAgentReport, check_mandatory_agents

# ---------------------------------------------------------------------------
# Extra minimal-valid templates for the config surfaces the existing
# scaffold_config() generator doesn't cover. Each loader below tolerates a
# missing file or an empty/near-empty document (see profile_service,
# schedule_service, token_service — all use `.get(key, default)`).
# ---------------------------------------------------------------------------

_MODEL_PROFILES_YAML = """\
# HivePilot model profile bindings — maps a profile name to a concrete model.
# Referenced by role definitions in roles.yaml (role.model_profile).
claude_profiles:
  coding:
    model: sonnet
  architecture:
    model: opus
  automation:
    model: haiku
"""

_SCHEDULES_YAML = """\
# HivePilot scheduled task definitions. Empty by default.
# schedules:
#   nightly-docs:
#     task: docs
#     projects: ["your-project"]
#     interval_minutes: 1440
#     enabled: true
schedules: {}
"""

_API_TOKENS_YAML = """\
# HivePilot API tokens. Managed via `hivepilot tokens add/list/rotate/remove`
# — do not hand-edit token_hash values below.
tokens: []
"""

SCAFFOLD_FILES: dict[str, str] = {
    **_BASE_SCAFFOLD_FILES,
    "model_profiles.yaml": _MODEL_PROFILES_YAML,
    "schedules.yaml": _SCHEDULES_YAML,
    "api_tokens.yaml": _API_TOKENS_YAML,
}

# Files validated by parsing only (no strict pydantic model at runtime —
# these loaders all use tolerant `.get(key, default)` reads).
_PLAIN_YAML_FILES: tuple[str, ...] = (
    "roles.yaml",
    "policies.yaml",
    "model_profiles.yaml",
    "schedules.yaml",
    "api_tokens.yaml",
)


@dataclass(frozen=True)
class FileResult:
    """Outcome of writing (or skipping) a single scaffolded/seeded file."""

    path: Path
    action: str  # "created" | "skipped" | "overwritten" | "updated"


@dataclass(frozen=True)
class ValidationResult:
    """Outcome of attempting to load one config file through its model."""

    name: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True)
class InitOutcome:
    """Aggregate result of a single `run_init()` call, for the CLI to report."""

    mode: str  # "clone" | "scaffold"
    target: Path
    scaffold_results: list[FileResult]
    env_result: FileResult | None
    synced_files: list[str]
    validation: list[ValidationResult]
    # Directory `validation` was actually run against. Equal to `target` in
    # SCAFFOLD mode; in CLONE mode this is the XDG config dir (where
    # config_service.sync() actually writes), which may differ from `target`
    # when a custom --path was given (see `run_init`).
    validated_target: Path
    # Presence verdict for the mandatory agent CLIs (claude/codex/vibe). The
    # CLI layer (`hivepilot.cli.init_config`) decides what to do with this --
    # warn (never hard-fail) regardless of which/how-many are present, since
    # `init` scaffolds the config you need before you can install an agent
    # CLI in the first place -- keeping this module's functions plain and
    # exit-free.
    mandatory_agents: MandatoryAgentReport


def resolve_target_dir(path: Path | None) -> Path:
    """Resolve the wizard's target config directory.

    Defaults to the XDG config dir (``settings.xdg_config_home``, e.g.
    ``~/.config/hivepilot``) when *path* is not given — the same directory
    the rest of the app resolves config files from first.
    """
    base = path if path is not None else settings.xdg_config_home
    return Path(base).expanduser().resolve()


def is_interactive_tty() -> bool:
    """Return True when stdin is an interactive TTY (safe to prompt)."""
    return sys.stdin.isatty()


def minimal_document_for(name: str) -> str:
    """Return the minimal-valid scaffold template content for *name*.

    Raises ValueError if there is no known template for that filename.
    """
    try:
        return SCAFFOLD_FILES[name]
    except KeyError as exc:
        raise ValueError(f"No scaffold template for {name!r}") from exc


def scaffold_local(target: Path, *, force: bool = False) -> list[FileResult]:
    """Write every scaffold file into *target*, skipping files that already
    exist unless *force* is set. Never raises on a pre-existing file — the
    wizard needs per-file skip/overwrite reporting, unlike the lower-level
    ``scaffold_config`` (which is all-or-nothing and raises on any conflict).
    """
    target.mkdir(parents=True, exist_ok=True)
    results: list[FileResult] = []
    for rel, content in SCAFFOLD_FILES.items():
        dest = target / rel
        existed = dest.exists()
        if existed and not force:
            results.append(FileResult(dest, "skipped"))
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        results.append(FileResult(dest, "overwritten" if existed else "created"))
    return results


def ensure_env_from_example(target: Path, *, auto_copy: bool) -> FileResult | None:
    """Copy .env.example -> .env in *target* if .env is missing, .env.example
    exists, and *auto_copy* is True. Never overwrites an existing .env.
    Returns None when there is nothing to do.
    """
    env_path = target / ".env"
    example_path = target / ".env.example"
    if env_path.exists() or not example_path.exists() or not auto_copy:
        return None
    env_path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")
    return FileResult(env_path, "created")


def seed_env_with_config_repo(target: Path, repo: str) -> FileResult:
    """Ensure *target*/.env exists (seeded from .env.example when missing,
    otherwise created empty) and carries ``HIVEPILOT_CONFIG_REPO=<repo>``.

    Only that single line is added/updated — the rest of an existing .env's
    content is preserved untouched (the file itself is never wiped).

    Raises ValueError if *repo* contains a newline/carriage-return: a git URL
    or local path never legitimately needs one, and writing it unsanitized
    would inject extra ``KEY=value`` lines into ``.env``.
    """
    if "\n" in repo or "\r" in repo:
        raise ValueError(
            "config repo value must not contain newlines/carriage returns "
            f"(got {repo!r}) -- refusing to write a malformed .env entry"
        )
    target.mkdir(parents=True, exist_ok=True)
    env_path = target / ".env"
    example_path = target / ".env.example"

    existed = env_path.exists()
    if not existed:
        base = example_path.read_text(encoding="utf-8") if example_path.exists() else ""
        env_path.write_text(base, encoding="utf-8")

    lines = env_path.read_text(encoding="utf-8").splitlines()
    out_lines: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith("HIVEPILOT_CONFIG_REPO="):
            out_lines.append(f"HIVEPILOT_CONFIG_REPO={repo}")
            replaced = True
        else:
            out_lines.append(line)
    if not replaced:
        out_lines.append(f"HIVEPILOT_CONFIG_REPO={repo}")

    env_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return FileResult(env_path, "updated" if existed else "created")


def run_clone_sync(repo: str) -> list[str]:
    """Delegate to the existing config-sync logic.

    This is the ONLY git interaction the wizard performs: it points the
    process-wide ``settings.config_repo`` at *repo* for the duration of this
    call and defers to ``config_service.sync()`` (clone-or-pull + copy into
    the XDG config dir), never reimplementing that logic here.
    """
    from hivepilot.services import config_service

    settings.config_repo = repo
    return config_service.sync()


def _model_loaders() -> dict[str, Callable[[Path], object]]:
    """Lazily import the project_service loaders (avoids import cost for
    callers that only need the plain-YAML validation path)."""
    from hivepilot.services.project_service import (
        load_groups,
        load_pipelines,
        load_projects,
        load_tasks,
    )

    return {
        "projects.yaml": load_projects,
        "tasks.yaml": load_tasks,
        "pipelines.yaml": load_pipelines,
        "groups.yaml": load_groups,
    }


def _plain_yaml_loaders() -> dict[str, Callable[[Path], object]]:
    """Lazily import the cheap, side-effect-free runtime loaders that exist
    for a subset of ``_PLAIN_YAML_FILES``.

    Each of these accepts a *path* override and (per pathlib's ``/`` operator
    treating an absolute right-hand side as an absolute result) resolves to
    exactly that path regardless of the live XDG/config-repo chain, so they
    validate the actual scaffolded/synced file rather than whatever the
    process's real settings happen to point at.

    ``roles.yaml`` has no such override-able loader --
    ``hivepilot.roles.load_roles()`` takes no path argument and always
    resolves through the live settings chain, so it cannot be pointed at an
    arbitrary target directory here — it stays on ``yaml.safe_load`` below.
    """
    from hivepilot.services.policy_service import load_policies
    from hivepilot.services.profile_service import load_claude_profiles
    from hivepilot.services.schedule_service import load_schedules
    from hivepilot.services.token_service import load_tokens

    return {
        "policies.yaml": load_policies,
        "model_profiles.yaml": load_claude_profiles,
        "schedules.yaml": load_schedules,
        "api_tokens.yaml": load_tokens,
    }


def validate_target(target: Path) -> list[ValidationResult]:
    """Attempt to load every config file in *target* through its existing
    loader/model. Never crashes on a single bad file — every failure is
    collected as a ValidationResult instead of propagating.
    """
    results: list[ValidationResult] = []

    for name, loader in _model_loaders().items():
        file_path = target / name
        if not file_path.exists():
            results.append(ValidationResult(name, False, "file not found"))
            continue
        try:
            loader(file_path)
            results.append(ValidationResult(name, True))
        except Exception as exc:  # noqa: BLE001 — collect, never crash the wizard
            results.append(ValidationResult(name, False, str(exc)))

    plain_yaml_loaders = _plain_yaml_loaders()
    for name in _PLAIN_YAML_FILES:
        file_path = target / name
        if not file_path.exists():
            results.append(ValidationResult(name, False, "file not found"))
            continue
        plain_loader = plain_yaml_loaders.get(name)
        try:
            if plain_loader is not None:
                plain_loader(file_path)
            else:
                yaml.safe_load(file_path.read_text(encoding="utf-8"))
            results.append(ValidationResult(name, True))
        except Exception as exc:  # noqa: BLE001 — collect, never crash the wizard
            results.append(ValidationResult(name, False, str(exc)))

    try:
        from hivepilot.services.config_validation import validate_config

        problems = validate_config(base_dir=target)
        if problems:
            results.append(ValidationResult("cross-reference", False, "; ".join(problems)))
        else:
            results.append(ValidationResult("cross-reference", True))
    except Exception as exc:  # noqa: BLE001 — collect, never crash the wizard
        results.append(ValidationResult("cross-reference", False, str(exc)))

    return results


def run_init(
    *,
    config_repo: str | None,
    path: Path | None,
    force: bool = False,
    auto_copy_env: bool = True,
) -> InitOutcome:
    """Core wizard logic — no prompting, no TTY interaction.

    The CLI layer resolves interactive choices (whether to clone, the repo
    URL, whether to copy .env.example) before calling this function.
    """
    target = resolve_target_dir(path)
    mode = "clone" if config_repo else "scaffold"

    scaffold_results: list[FileResult] = []
    synced_files: list[str] = []
    env_result: FileResult | None

    if mode == "clone":
        assert config_repo is not None  # narrowed by `mode` above
        env_result = seed_env_with_config_repo(target, config_repo)
        synced_files = run_clone_sync(config_repo)
        # config_service.sync() always copies into the XDG config dir
        # (settings.xdg_config_home), never into an arbitrary `--path` --
        # validate the directory sync ACTUALLY wrote to, not `target` (which,
        # in clone mode, only controls where .env is seeded).
        validation_target = resolve_target_dir(None)
    else:
        scaffold_results = scaffold_local(target, force=force)
        env_result = ensure_env_from_example(target, auto_copy=auto_copy_env)
        validation_target = target

    validation = validate_target(validation_target)
    mandatory_agents = check_mandatory_agents()

    return InitOutcome(
        mode=mode,
        target=target,
        scaffold_results=scaffold_results,
        env_result=env_result,
        synced_files=synced_files,
        validation=validation,
        validated_target=validation_target,
        mandatory_agents=mandatory_agents,
    )
