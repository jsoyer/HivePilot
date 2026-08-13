"""The deterministic check has to be able to overrule a unanimous approval.

That is the whole case, and it is not hypothetical. On the memory A/B both arms
produced a tool that emitted a raw ESC byte to stdout, and in BOTH arms the
reviewer, the CISO and QA all passed it -- zero mentions of ANSI, escape
sequence or control character across six review outputs. A gate wired only to
agent prose would have promoted both.

So a check that fails must block a release every agent approved, and the pull
request must say which check failed and what it printed -- otherwise this
repeats the PR #428 failure in a new place: a refusal with no reason attached.
"""

from __future__ import annotations

import sys

import pytest

from hivepilot.models import GitActions
from hivepilot.services import git_service
from hivepilot.services.verification_service import Check


def _py(code: str) -> str:
    return f'{sys.executable} -c "{code}"'


class _Forge:
    def __init__(self) -> None:
        self.comments: list[str] = []
        self.promoted = False
        self.merged = False

    def open_pr(self, **_kw):
        return "https://example.invalid/pr/1"

    def promote_pr(self, **_kw):
        self.promoted = True

    def merge_pr(self, **_kw):
        self.merged = True

    def comment_pr(self, *, project, branch, body):
        self.comments.append(body)


@pytest.fixture
def gate(monkeypatch, tmp_path):
    """A gate with the git plumbing stubbed out, so only the decision is under
    test."""
    forge = _Forge()
    monkeypatch.setattr(git_service, "ensure_repo", lambda _p: object(), raising=False)
    monkeypatch.setattr(git_service, "checkout_branch", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(git_service, "resolve_forge", lambda _p: forge, raising=False)
    monkeypatch.setattr(git_service, "create_pr", lambda **k: None, raising=False)
    monkeypatch.setattr(git_service, "promote_pr", lambda **k: forge.promote_pr(), raising=False)
    monkeypatch.setattr(git_service, "merge_pr", lambda **k: forge.merge_pr(), raising=False)
    return forge


def _project(tmp_path):
    from hivepilot.models import ProjectConfig

    return ProjectConfig(path=tmp_path)


class TestAFailingCheckOverrulesTheAgents:
    def test_a_unanimous_approval_is_still_blocked(self, gate, tmp_path):
        """The A/B case, exactly: every agent said proceed, the probe says no."""
        git_service.perform_git_actions(
            project_name="p",
            project=_project(tmp_path),
            git=GitActions(create_pr=True, promote_pr=True, merge_pr=True),
            task_result="status: APPROVE\nLooks good to me.",
            checks=[Check(name="hostile-input", command=_py("raise SystemExit(1)"))],
        )

        assert gate.promoted is False
        assert gate.merged is False

    def test_the_pull_request_says_which_check_failed(self, gate, tmp_path):
        git_service.perform_git_actions(
            project_name="p",
            project=_project(tmp_path),
            git=GitActions(create_pr=True, promote_pr=True),
            task_result="status: APPROVE",
            checks=[
                Check(
                    name="hostile-input",
                    command=_py(
                        "import sys; sys.stderr.write('raw ESC reached stdout'); sys.exit(1)"
                    ),
                )
            ],
        )

        body = "\n".join(gate.comments)
        assert "hostile-input" in body
        assert "raw ESC reached stdout" in body

    def test_a_check_that_could_not_run_also_blocks(self, gate, tmp_path):
        """Fail closed: nobody verified anything, so nothing is verified."""
        git_service.perform_git_actions(
            project_name="p",
            project=_project(tmp_path),
            git=GitActions(create_pr=True, promote_pr=True),
            task_result="status: APPROVE",
            checks=[Check(name="absent", command="hivepilot-no-such-binary-9f3a")],
        )

        assert gate.promoted is False


class TestAPassingCheckDoesNotRescueABlockingAgent:
    def test_the_agent_verdict_still_blocks(self, gate, tmp_path):
        """The check is an ADDITIONAL veto, never a permission. A green probe
        must not promote something the CISO refused."""
        git_service.perform_git_actions(
            project_name="p",
            project=_project(tmp_path),
            git=GitActions(create_pr=True, promote_pr=True),
            task_result="status: BLOCKED\nThe allowlist accepts an unsigned path.",
            checks=[Check(name="tests", command=_py("pass"))],
        )

        assert gate.promoted is False


class TestNothingChangesWhenNoChecksAreDeclared:
    def test_an_approved_run_still_promotes(self, gate, tmp_path):
        """Backward compatibility, stated as a test: every existing pipeline
        declares no checks and must behave exactly as before."""
        git_service.perform_git_actions(
            project_name="p",
            project=_project(tmp_path),
            git=GitActions(create_pr=True, promote_pr=True),
            task_result="status: APPROVE",
        )

        assert gate.promoted is True

    def test_and_nothing_is_posted(self, gate, tmp_path):
        git_service.perform_git_actions(
            project_name="p",
            project=_project(tmp_path),
            git=GitActions(create_pr=True, promote_pr=True),
            task_result="status: APPROVE",
        )

        assert gate.comments == []


class TestAPassingCheckPromotesAndSaysSo:
    def test_it_promotes(self, gate, tmp_path):
        git_service.perform_git_actions(
            project_name="p",
            project=_project(tmp_path),
            git=GitActions(create_pr=True, promote_pr=True),
            task_result="status: APPROVE",
            checks=[Check(name="tests", command=_py("pass"))],
        )

        assert gate.promoted is True
