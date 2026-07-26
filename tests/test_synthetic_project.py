"""Focused unit tests for ``orchestrator._synthetic_project`` -- the fix for
a recurring crash class in ``human_challenge()`` / ``_handle_agent_requests()``:
those two out-of-band, repo-less call sites used to build a ``RunnerPayload``
with ``project=None``, and every runner (Claude, prompt-cli/vibe, the IaC/
k8s/config-mgmt runners, ...) unconditionally dereferences
``payload.project.env`` / ``.secrets`` / ``.path`` / ``.description`` /
``.claude_md`` in multiple places. Three separate crashes were fixed one
attribute at a time (a missing prompt_file, then ``payload.project.path``,
then ``payload.project.env``) before this: the payload now always carries a
*valid* ``ProjectConfig``, so every ``payload.project.*`` access just works.

See ``tests/test_human_challenge.py::test_human_challenge_end_to_end_real_claude_runner_no_crash``
and ``tests/test_agent_requests.py::TestHandleAgentRequestsEndToEnd`` for the
full end-to-end drive through a real ``ClaudeRunner``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from hivepilot.orchestrator import _synthetic_project


@pytest.mark.parametrize("kind", ["challenge", "agent-request"])
class TestSyntheticProject:
    def test_path_is_a_real_existing_directory(self, kind: str) -> None:
        project = _synthetic_project(kind)
        assert project.path.exists()
        assert project.path.is_dir()

    def test_path_is_never_a_real_project_repository(self, kind: str) -> None:
        """The synthetic path must always be the bare system temp dir --
        never a path that could coincide with (or accidentally write into)
        a real configured project's repository."""
        project = _synthetic_project(kind)
        assert project.path == Path(tempfile.gettempdir())

    def test_description_names_the_kind_and_is_obviously_synthetic(self, kind: str) -> None:
        project = _synthetic_project(kind)
        description = project.description or ""
        assert kind in description
        assert "synthetic" in description.lower()

    def test_env_and_secrets_are_empty_dicts_not_none(self, kind: str) -> None:
        project = _synthetic_project(kind)
        assert project.env == {}
        assert project.secrets == {}

    def test_claude_md_is_unset(self, kind: str) -> None:
        project = _synthetic_project(kind)
        assert project.claude_md is None

    def test_two_calls_are_distinguishable_by_kind(self, kind: str) -> None:
        """A "challenge" project and an "agent-request" project must never
        be confused for one another in logs/prompts."""
        other_kind = "agent-request" if kind == "challenge" else "challenge"
        project = _synthetic_project(kind)
        other = _synthetic_project(other_kind)
        assert project.description != other.description


# Every `payload.project.*` attribute the Claude runner dereferences (see
# hivepilot/runners/claude_runner.py lines 369, 526, 563, 569, 577, 638, 643,
# 651, 915-920, 936, 970) -- accessing each one on the synthetic project must
# never raise AttributeError/TypeError, which is exactly what `project=None`
# used to do.
_CLAUDE_RUNNER_ACCESSED_ATTRS = ("env", "secrets", "path", "description", "claude_md")


@pytest.mark.parametrize("attr", _CLAUDE_RUNNER_ACCESSED_ATTRS)
def test_every_claude_runner_accessed_attribute_is_safe(attr: str) -> None:
    project = _synthetic_project("challenge")

    value = getattr(project, attr)  # must not raise

    if attr in ("env", "secrets"):
        assert value == {}
    elif attr == "path":
        assert isinstance(value, Path)
        assert value.exists()
    else:
        # `description`/`claude_md` are used behind truthiness checks
        # (`if payload.project.description:`) by every runner -- str or
        # None are both safe; anything else would be a contract change.
        assert value is None or isinstance(value, str)


def test_default_branch_and_forge_still_have_sane_defaults() -> None:
    """The remaining ProjectConfig fields (never dereferenced by any runner
    for a repo-less payload, but still part of the contract) must resolve to
    the model's own safe defaults -- never crash construction."""
    project = _synthetic_project("challenge")
    assert project.default_branch == "main"
    assert project.forge == "github"
    assert project.modules == {}
    assert project.ui_surfaces == []
