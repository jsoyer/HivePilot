"""Runner-agnostic ``apply_skill`` contract (Sprint 2) + the Claude runner's
materialisation of skills into an ephemeral scratch directory.

Covers:
  * ``apply_skill`` is optional/structural (getattr-discovered) — a runner
    without it is a safe no-op via ``hivepilot.runners.base.apply_skill_if_supported``.
  * ClaudeRunner.apply_skill materialises ``files`` into an ephemeral scratch
    and stashes an appended system prompt; ``_build_invocation`` reflects it.
  * The scratch is cleaned up after the step, on success AND on exception.
  * ``applies_to`` mismatch skips a skill non-fatally.
  * A pre-existing real ``.claude/skills/<name>/`` in the project working dir
    is never touched.
  * ``${secret:NAME}`` refs inside skill content are resolved via the
    existing choke point (``hivepilot.services.secret_refs.resolve_secret_refs``)
    and registered for masking — never leaked raw.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hivepilot.config import settings
from hivepilot.models import ProjectConfig, RunnerDefinition, TaskStep
from hivepilot.plugins import SkillSpec
from hivepilot.runners.base import RunnerPayload, apply_skill_if_supported
from hivepilot.runners.claude_runner import ClaudeRunner
from hivepilot.services import config_provenance


@pytest.fixture(autouse=True)
def _clear_masks() -> Iterator[None]:
    config_provenance.clear_secret_values()
    yield
    config_provenance.clear_secret_values()


def _runner() -> ClaudeRunner:
    return ClaudeRunner(RunnerDefinition(name="claude", kind="claude", command="claude"), settings)


def _payload(tmp_path: Path, *, secrets_catalog: dict | None = None) -> RunnerPayload:
    pf = tmp_path / "p.md"
    pf.write_text("do it", encoding="utf-8")
    return RunnerPayload(
        project_name="p",
        project=ProjectConfig(path=tmp_path, secrets=secrets_catalog or {}),
        task_name="t",
        step=TaskStep(name="s", runner="claude", prompt_file=str(pf)),
        metadata={},
        secrets={},
    )


def _skill(**overrides) -> SkillSpec:
    base: SkillSpec = {
        "name": "demo",
        "description": "demo skill",
        "provider": "sample",
        "files": {"SKILL.md": "# Demo\nDo the thing."},
    }
    base.update(overrides)  # type: ignore[typeddict-item]
    return base


# --- apply_skill_if_supported: optional/structural discovery ----------------


def test_apply_skill_if_supported_noop_when_runner_lacks_method(tmp_path: Path) -> None:
    class _NoSkillRunner:
        pass

    payload = _payload(tmp_path)
    out = apply_skill_if_supported(_NoSkillRunner(), payload, [_skill()])
    assert out is payload


def test_apply_skill_if_supported_delegates_when_present(tmp_path: Path) -> None:
    runner = _runner()
    payload = _payload(tmp_path)
    out = apply_skill_if_supported(runner, payload, [_skill()])
    assert out is not payload
    assert "skill_scratch_dir" in out.metadata
    Path(out.metadata["skill_scratch_dir"])  # exists as a valid path string
    import shutil

    shutil.rmtree(out.metadata["skill_scratch_dir"], ignore_errors=True)


# --- ClaudeRunner.apply_skill: materialisation -------------------------------


def test_apply_skill_materialises_files_to_scratch(tmp_path: Path) -> None:
    runner = _runner()
    payload = _payload(tmp_path)
    skill = _skill(system_prompt="Follow the demo skill.")

    new_payload = runner.apply_skill(payload, [skill])

    scratch = Path(new_payload.metadata["skill_scratch_dir"])
    skill_file = scratch / ".claude" / "skills" / "demo" / "SKILL.md"
    assert skill_file.read_text(encoding="utf-8") == "# Demo\nDo the thing."
    assert new_payload.metadata["skill_system_prompt"] == "Follow the demo skill."

    import shutil

    shutil.rmtree(scratch, ignore_errors=True)


def test_apply_skill_does_not_mutate_original_payload(tmp_path: Path) -> None:
    runner = _runner()
    payload = _payload(tmp_path)
    skill = _skill()

    new_payload = runner.apply_skill(payload, [skill])

    assert payload.metadata == {}
    assert new_payload is not payload

    import shutil

    shutil.rmtree(new_payload.metadata["skill_scratch_dir"], ignore_errors=True)


def test_build_invocation_reflects_materialised_skill(tmp_path: Path) -> None:
    runner = _runner()
    payload = _payload(tmp_path)
    skill = _skill(system_prompt="Follow the demo skill.")

    new_payload = runner.apply_skill(payload, [skill])
    args, _ = runner._build_invocation(new_payload)

    scratch = new_payload.metadata["skill_scratch_dir"]
    assert "--add-dir" in args
    assert args[args.index("--add-dir") + 1] == scratch
    assert "--append-system-prompt" in args
    assert args[args.index("--append-system-prompt") + 1] == "Follow the demo skill."

    import shutil

    shutil.rmtree(scratch, ignore_errors=True)


def test_build_invocation_unaffected_when_no_skills_applied(tmp_path: Path) -> None:
    runner = _runner()
    payload = _payload(tmp_path)
    args, _ = runner._build_invocation(payload)
    assert "--add-dir" not in args
    assert "--append-system-prompt" not in args


# --- applies_to mismatch ------------------------------------------------------


def test_applies_to_mismatch_skips_skill_non_fatally(tmp_path: Path) -> None:
    runner = _runner()
    payload = _payload(tmp_path)
    skill = _skill(applies_to=["codex"])

    new_payload = runner.apply_skill(payload, [skill])

    # No scratch created at all since the only skill offered was skipped.
    assert "skill_scratch_dir" not in new_payload.metadata


def test_applies_to_match_is_applied(tmp_path: Path) -> None:
    runner = _runner()
    payload = _payload(tmp_path)
    skill = _skill(applies_to=["claude", "codex"])

    new_payload = runner.apply_skill(payload, [skill])

    assert "skill_scratch_dir" in new_payload.metadata
    import shutil

    shutil.rmtree(new_payload.metadata["skill_scratch_dir"], ignore_errors=True)


# --- never overwrite a pre-existing real .claude/skills/<name>/ -------------


def test_never_overwrites_real_claude_skills_dir(tmp_path: Path) -> None:
    real_skill_dir = tmp_path / ".claude" / "skills" / "demo"
    real_skill_dir.mkdir(parents=True)
    sentinel = "REAL REPO CONTENT — DO NOT TOUCH"
    (real_skill_dir / "SKILL.md").write_text(sentinel, encoding="utf-8")

    runner = _runner()
    payload = _payload(tmp_path)
    skill = _skill(files={"SKILL.md": "malicious overwrite attempt"})

    new_payload = runner.apply_skill(payload, [skill])

    # The real repo file is untouched.
    assert (real_skill_dir / "SKILL.md").read_text(encoding="utf-8") == sentinel
    # The materialised copy lives elsewhere (the scratch), not under tmp_path.
    scratch = Path(new_payload.metadata["skill_scratch_dir"])
    assert not str(scratch).startswith(str(tmp_path))

    import shutil

    shutil.rmtree(scratch, ignore_errors=True)


# --- cleanup: success + exception --------------------------------------------


def test_scratch_cleaned_up_after_successful_run(tmp_path: Path) -> None:
    runner = _runner()
    payload = _payload(tmp_path)
    skill = _skill()
    payload = runner.apply_skill(payload, [skill])
    scratch = Path(payload.metadata["skill_scratch_dir"])
    assert scratch.exists()

    with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
        m.return_value = MagicMock(returncode=0)
        runner.run(payload)

    assert not scratch.exists()


def test_scratch_cleaned_up_after_run_raises(tmp_path: Path) -> None:
    runner = _runner()
    payload = _payload(tmp_path)
    skill = _skill()
    payload = runner.apply_skill(payload, [skill])
    scratch = Path(payload.metadata["skill_scratch_dir"])
    assert scratch.exists()

    with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
        m.side_effect = RuntimeError("boom")
        with pytest.raises(RuntimeError):
            runner.run(payload)

    assert not scratch.exists()


def test_scratch_cleaned_up_after_capture_success(tmp_path: Path) -> None:
    runner = _runner()
    payload = _payload(tmp_path)
    skill = _skill()
    payload = runner.apply_skill(payload, [skill])
    scratch = Path(payload.metadata["skill_scratch_dir"])
    assert scratch.exists()

    with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
        m.return_value = MagicMock(stdout="OK", returncode=0)
        out = runner.capture(payload)

    assert out == "OK"
    assert not scratch.exists()


def test_scratch_cleaned_up_after_capture_failure(tmp_path: Path) -> None:
    runner = _runner()
    payload = _payload(tmp_path)
    skill = _skill()
    payload = runner.apply_skill(payload, [skill])
    scratch = Path(payload.metadata["skill_scratch_dir"])
    assert scratch.exists()

    with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
        m.return_value = MagicMock(stdout="", stderr="fail", returncode=1)
        with pytest.raises(RuntimeError):
            runner.capture(payload)

    assert not scratch.exists()


def test_no_skills_applied_means_no_scratch_to_clean(tmp_path: Path) -> None:
    """A step that never called apply_skill must not choke on cleanup
    (no skill_scratch_dir key present at all)."""
    runner = _runner()
    payload = _payload(tmp_path)
    with patch("hivepilot.runners.claude_runner.subprocess.run") as m:
        m.return_value = MagicMock(returncode=0)
        runner.run(payload)  # must not raise


# --- secret resolution / masking ---------------------------------------------


def test_skill_secret_refs_resolved_and_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HP_SKILL_SECRET_STORE", "super-secret-value")
    catalog = {"mytoken": {"source": "env", "key": "HP_SKILL_SECRET_STORE"}}
    runner = _runner()
    payload = _payload(tmp_path, secrets_catalog=catalog)
    skill = _skill(
        files={"SKILL.md": "token=${secret:mytoken}"},
        system_prompt="Use token ${secret:mytoken} to authenticate.",
    )

    new_payload = runner.apply_skill(payload, [skill])

    scratch = Path(new_payload.metadata["skill_scratch_dir"])
    written = (scratch / ".claude" / "skills" / "demo" / "SKILL.md").read_text(encoding="utf-8")
    assert written == "token=super-secret-value"
    assert new_payload.metadata["skill_system_prompt"] == (
        "Use token super-secret-value to authenticate."
    )
    # The resolved secret value is registered for masking (existing choke point).
    assert "super-secret-value" in config_provenance.registered_secret_values()
    assert config_provenance.redact_text(written) == "token=REDACTED"

    import shutil

    shutil.rmtree(scratch, ignore_errors=True)
