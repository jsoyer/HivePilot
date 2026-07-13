"""Tests for the `hivepilot init` onboarding wizard.

Named to mirror `hivepilot/services/init_service.py` (matches this repo's
`test_<service>.py` convention, e.g. test_config_service.py/config_service.py)
and to satisfy the TDD pre-write hook, which resolves the expected test path
from the production module name.

Covers the service layer (`hivepilot.services.init_service`) directly for
fast, TTY-independent unit coverage, plus end-to-end CLI invocations via
Typer's CliRunner. No test hits the network: clone mode always monkeypatches
`hivepilot.services.config_service.sync`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

# ---------------------------------------------------------------------------
# Stub out optional heavy dependencies before importing hivepilot.cli — same
# approach as tests/test_cli.py, needed because hivepilot.cli transitively
# imports hivepilot.orchestrator which imports several optional extras.
# ---------------------------------------------------------------------------

_STUBS = [
    "langchain",
    "langchain.text_splitter",
    "langchain_community",
    "langchain_community.embeddings",
    "langchain_community.vectorstores",
    "langchain_openai",
    "openai",
    "boto3",
    "docker",
    "telegram",
    "telegram.ext",
    "fastapi",
    "fastapi.responses",
    "fastapi.security",
    "uvicorn",
    "textual",
    "slack_bolt",
    "slack_bolt.adapter",
    "slack_bolt.adapter.fastapi",
    "slack_bolt.adapter.socket_mode",
    "discord",
    "PyNaCl",
    "nacl",
    "nacl.exceptions",
    "nacl.signing",
]

import importlib  # noqa: E402

for _mod in _STUBS:
    if _mod in sys.modules:
        continue
    try:
        importlib.import_module(_mod)
    except Exception:
        sys.modules[_mod] = MagicMock()

from typer.testing import CliRunner  # noqa: E402

from hivepilot.cli import app  # noqa: E402
from hivepilot.services import init_service  # noqa: E402

# ---------------------------------------------------------------------------
# scaffold_local
# ---------------------------------------------------------------------------


def test_scaffold_writes_valid_yaml_for_every_config_model(tmp_path: Path) -> None:
    """scaffold_local() writes a file for every config surface, each of which
    loads through its existing model/loader without error."""
    from hivepilot.services.project_service import (
        load_groups,
        load_pipelines,
        load_projects,
        load_tasks,
    )

    results = init_service.scaffold_local(tmp_path, force=False)
    assert results, "expected at least one file to be written"
    assert all(r.action == "created" for r in results)

    # Strict pydantic models
    assert load_projects(tmp_path / "projects.yaml").projects
    assert load_tasks(tmp_path / "tasks.yaml").tasks
    assert load_pipelines(tmp_path / "pipelines.yaml").pipelines
    assert load_groups(tmp_path / "groups.yaml").groups

    # Tolerant plain-YAML files (no strict pydantic model at runtime) —
    # parse-only check mirrors how the app itself loads them.
    for name in (
        "roles.yaml",
        "policies.yaml",
        "model_profiles.yaml",
        "schedules.yaml",
        "api_tokens.yaml",
    ):
        path = tmp_path / name
        assert path.exists(), f"{name} was not scaffolded"
        assert yaml.safe_load(path.read_text(encoding="utf-8")) is not None


def test_scaffold_plain_yaml_surfaces_pass_their_real_loaders(tmp_path: Path) -> None:
    """The 4 plain-YAML surfaces that have a cheap, override-able runtime
    loader must parse through that REAL loader, not just yaml.safe_load —
    catching structurally-wrong-but-parseable YAML that safe_load can't.
    `roles.yaml` has no such loader (see `_plain_yaml_loaders` docstring) so
    it is intentionally excluded here."""
    from hivepilot.services.policy_service import load_policies
    from hivepilot.services.profile_service import load_claude_profiles
    from hivepilot.services.schedule_service import load_schedules
    from hivepilot.services.token_service import load_tokens

    init_service.scaffold_local(tmp_path, force=False)

    assert load_policies(tmp_path / "policies.yaml") is not None
    assert load_claude_profiles(tmp_path / "model_profiles.yaml")["coding"]["model"] == "sonnet"
    assert load_schedules(tmp_path / "schedules.yaml") == {}
    assert load_tokens(tmp_path / "api_tokens.yaml") == []


def test_scaffold_produces_cross_reference_clean_config(tmp_path: Path) -> None:
    """The scaffolded config should have zero cross-reference problems under
    the existing `validate_config` engine (the same one `hivepilot validate`
    uses)."""
    from hivepilot.services.config_validation import validate_config

    init_service.scaffold_local(tmp_path, force=False)
    problems = validate_config(base_dir=tmp_path)
    assert problems == [], f"Unexpected problems: {problems}"


def test_scaffold_idempotent_skips_existing_files(tmp_path: Path) -> None:
    """Running scaffold_local twice must not overwrite; the second run
    reports every file as skipped."""
    first = init_service.scaffold_local(tmp_path, force=False)
    assert all(r.action == "created" for r in first)

    second = init_service.scaffold_local(tmp_path, force=False)
    assert len(second) == len(first)
    assert all(r.action == "skipped" for r in second)


def test_scaffold_force_overwrites_existing_files(tmp_path: Path) -> None:
    """--force overwrites a customized file instead of skipping it."""
    init_service.scaffold_local(tmp_path, force=False)
    target_file = tmp_path / "projects.yaml"
    target_file.write_text("# customized by user\n", encoding="utf-8")

    results = init_service.scaffold_local(tmp_path, force=True)
    by_name = {r.path.name: r.action for r in results}
    assert by_name["projects.yaml"] == "overwritten"
    assert target_file.read_text(encoding="utf-8") != "# customized by user\n"


# ---------------------------------------------------------------------------
# .env handling
# ---------------------------------------------------------------------------


def test_env_created_from_example_when_missing(tmp_path: Path) -> None:
    init_service.scaffold_local(tmp_path, force=False)  # writes .env.example
    assert not (tmp_path / ".env").exists()

    result = init_service.ensure_env_from_example(tmp_path, auto_copy=True)
    assert result is not None
    assert result.action == "created"
    assert (tmp_path / ".env").exists()


def test_env_not_overwritten_when_present(tmp_path: Path) -> None:
    init_service.scaffold_local(tmp_path, force=False)
    env_path = tmp_path / ".env"
    env_path.write_text("# user customized, do not touch\n", encoding="utf-8")

    result = init_service.ensure_env_from_example(tmp_path, auto_copy=True)
    assert result is None
    assert env_path.read_text(encoding="utf-8") == "# user customized, do not touch\n"


def test_env_not_created_without_auto_copy(tmp_path: Path) -> None:
    init_service.scaffold_local(tmp_path, force=False)
    result = init_service.ensure_env_from_example(tmp_path, auto_copy=False)
    assert result is None
    assert not (tmp_path / ".env").exists()


def test_seed_env_with_config_repo_creates_and_updates(tmp_path: Path) -> None:
    """Clone mode always writes/updates HIVEPILOT_CONFIG_REPO in .env, seeding
    from .env.example when missing, without wiping unrelated content."""
    (tmp_path / ".env.example").write_text("HIVEPILOT_OTHER=1\n", encoding="utf-8")

    result = init_service.seed_env_with_config_repo(tmp_path, "git@example.com:org/config.git")
    assert result.action == "created"
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "HIVEPILOT_OTHER=1" in text
    assert "HIVEPILOT_CONFIG_REPO=git@example.com:org/config.git" in text

    # Second call updates the existing .env's line in place, preserving the
    # rest of the file (never wipes it wholesale).
    (tmp_path / ".env").write_text(
        "HIVEPILOT_OTHER=1\nHIVEPILOT_CONFIG_REPO=old\n", encoding="utf-8"
    )
    result2 = init_service.seed_env_with_config_repo(tmp_path, "new-repo-url")
    assert result2.action == "updated"
    text2 = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "HIVEPILOT_CONFIG_REPO=new-repo-url" in text2
    assert "HIVEPILOT_OTHER=1" in text2
    assert "HIVEPILOT_CONFIG_REPO=old" not in text2


def test_seed_env_with_config_repo_rejects_newline(tmp_path: Path) -> None:
    """MEDIUM-1 regression: a repo value containing a newline must be
    rejected outright -- writing it unsanitized would inject a second
    `KEY=value` line into `.env` (env-injection)."""
    with pytest.raises(ValueError):
        init_service.seed_env_with_config_repo(
            tmp_path, "git@example.com:org/config.git\nHIVEPILOT_EVIL=1"
        )
    # No side effects: rejected before any file/dir was touched.
    assert not (tmp_path / ".env").exists()


def test_seed_env_with_config_repo_rejects_carriage_return(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        init_service.seed_env_with_config_repo(tmp_path, "git@example.com:org/config.git\r\n")
    assert not (tmp_path / ".env").exists()


# ---------------------------------------------------------------------------
# Clone mode — never hits the network
# ---------------------------------------------------------------------------


def test_clone_mode_delegates_to_existing_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hivepilot.services import config_service

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-home"))
    calls: dict[str, bool] = {}

    def fake_sync() -> list[str]:
        calls["invoked"] = True
        return ["projects.yaml", "tasks.yaml"]

    monkeypatch.setattr(config_service, "sync", fake_sync)

    outcome = init_service.run_init(
        config_repo="git@example.com:org/config.git", path=tmp_path, force=False
    )

    assert calls.get("invoked") is True
    assert outcome.mode == "clone"
    assert outcome.synced_files == ["projects.yaml", "tasks.yaml"]
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "HIVEPILOT_CONFIG_REPO=git@example.com:org/config.git" in env_text


def test_clone_mode_reports_up_to_date_when_sync_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hivepilot.services import config_service

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-home"))
    monkeypatch.setattr(config_service, "sync", lambda: [])

    outcome = init_service.run_init(config_repo="/local/config-repo", path=tmp_path, force=False)
    assert outcome.synced_files == []


def test_clone_mode_validates_xdg_dir_sync_actually_writes_to_not_custom_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HIGH-1 regression: config_service.sync() only ever writes into the XDG
    config dir, never into an arbitrary `--path`. `run_init` must validate
    (and report) that XDG dir, not the unrelated custom `path` -- otherwise a
    successful clone is reported as "file not found" for every surface."""
    from hivepilot.services import config_service

    custom_path = tmp_path / "custom-path"
    fake_xdg_home = tmp_path / "fake-xdg-config"
    fake_xdg = fake_xdg_home / "hivepilot"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(fake_xdg_home))

    def fake_sync() -> list[str]:
        # Simulate config_service.sync() copying the cloned config into the
        # XDG config dir (the real sync()'s actual behavior).
        init_service.scaffold_local(fake_xdg, force=False)
        return ["projects.yaml"]

    monkeypatch.setattr(config_service, "sync", fake_sync)

    outcome = init_service.run_init(
        config_repo="git@example.com:org/config.git", path=custom_path, force=False
    )

    assert outcome.mode == "clone"
    # `target`/.env is still the custom --path -- clone mode only seeds .env
    # there, it never claims config files live there.
    assert outcome.target == custom_path.resolve()
    # Validation must have run against the XDG dir sync() actually wrote to.
    assert outcome.validated_target == fake_xdg.resolve()
    assert all(r.ok for r in outcome.validation), outcome.validation
    env_text = (custom_path / ".env").read_text(encoding="utf-8")
    assert "HIVEPILOT_CONFIG_REPO=git@example.com:org/config.git" in env_text


# ---------------------------------------------------------------------------
# validate_target
# ---------------------------------------------------------------------------


def test_validate_target_reports_missing_files(tmp_path: Path) -> None:
    results = init_service.validate_target(tmp_path)
    assert results, "expected at least one validation result"
    assert all(not r.ok for r in results if r.name != "cross-reference")


def test_validate_target_reports_ok_after_scaffold(tmp_path: Path) -> None:
    init_service.scaffold_local(tmp_path, force=False)
    results = init_service.validate_target(tmp_path)
    failed = [r for r in results if not r.ok]
    assert failed == [], f"Unexpected validation failures: {failed}"


def test_validate_target_catches_structural_error_via_real_loader(tmp_path: Path) -> None:
    """schedules.yaml missing the required `task` key is valid YAML (would
    pass a plain `yaml.safe_load`) but must FAIL through the real
    `load_schedules()` loader, which requires `task` on every entry."""
    init_service.scaffold_local(tmp_path, force=False)
    (tmp_path / "schedules.yaml").write_text(
        'schedules:\n  broken:\n    projects: ["acme"]\n', encoding="utf-8"
    )

    # Confirms the premise: plain YAML parsing alone sees nothing wrong.
    assert yaml.safe_load((tmp_path / "schedules.yaml").read_text(encoding="utf-8")) is not None

    results = init_service.validate_target(tmp_path)
    schedules_result = next(r for r in results if r.name == "schedules.yaml")
    assert schedules_result.ok is False


# ---------------------------------------------------------------------------
# run_init — full outcome assembly
# ---------------------------------------------------------------------------


def test_run_init_scaffold_mode_full_outcome(tmp_path: Path) -> None:
    outcome = init_service.run_init(config_repo=None, path=tmp_path, force=False)
    assert outcome.mode == "scaffold"
    assert outcome.target == tmp_path.resolve()
    assert outcome.scaffold_results
    assert outcome.env_result is not None
    assert outcome.env_result.action == "created"
    assert all(r.ok for r in outcome.validation)


def test_resolve_target_dir_defaults_to_xdg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    resolved = init_service.resolve_target_dir(None)
    assert resolved == (tmp_path / "hivepilot").resolve()


def test_resolve_target_dir_honors_explicit_path(tmp_path: Path) -> None:
    custom = tmp_path / "custom-dir"
    resolved = init_service.resolve_target_dir(custom)
    assert resolved == custom.resolve()


# ---------------------------------------------------------------------------
# CLI end-to-end
# ---------------------------------------------------------------------------


def test_cli_init_yes_scaffolds_without_prompting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`hivepilot init --yes` must never touch questionary, even if TTY
    detection were somehow wrong."""
    import questionary

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("questionary should not be invoked when --yes is passed")

    monkeypatch.setattr(questionary, "confirm", _boom)
    monkeypatch.setattr(questionary, "text", _boom)

    runner = CliRunner()
    result = runner.invoke(app, ["init", "--path", str(tmp_path), "--yes"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "projects.yaml").exists()
    assert (tmp_path / ".env").exists()


def test_cli_init_clone_mode_via_flag_no_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hivepilot.services import config_service

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-home"))
    monkeypatch.setattr(config_service, "sync", lambda: ["projects.yaml"])

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["init", "--path", str(tmp_path), "--config-repo", "git@example.com:org/config.git"],
    )

    assert result.exit_code == 0, result.output
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "HIVEPILOT_CONFIG_REPO=git@example.com:org/config.git" in env_text


def test_cli_init_bare_non_tty_never_prompts_and_scaffolds_with_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HIGH-2 regression: a bare `hivepilot init` (no --yes, no
    --config-repo) in a non-TTY shell must deterministically take the
    documented SCAFFOLD-with-defaults path -- never reach a questionary
    prompt, which would hang forever with nobody able to answer it.
    Typer's CliRunner wires a non-interactive stdin, so this exercises the
    real non-TTY code path with zero prior coverage."""
    import questionary

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("questionary must not be invoked in a non-TTY shell")

    monkeypatch.setattr(questionary, "confirm", _boom)
    monkeypatch.setattr(questionary, "text", _boom)
    # Belt-and-suspenders: guarantee the non-TTY branch is taken regardless
    # of how this environment's CliRunner happens to wire stdin.
    monkeypatch.setattr(init_service, "is_interactive_tty", lambda: False)

    runner = CliRunner()
    result = runner.invoke(app, ["init", "--path", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "projects.yaml").exists()
    assert (tmp_path / ".env").exists()


def test_is_interactive_tty_false_for_cli_runners_non_tty_stdin() -> None:
    """Sanity check for the belt-and-suspenders monkeypatch above: confirm
    CliRunner's stdin really is non-TTY on its own, i.e. the guard in
    `init_config` is not merely coincidentally satisfied by a monkeypatch."""
    runner = CliRunner()
    with runner.isolation():
        assert init_service.is_interactive_tty() is False
