"""Tests for git_service.merge_pr (Jules' autonomous final PR approval/merge)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hivepilot.models import GitActions, ProjectConfig
from hivepilot.services import git_service


def test_merge_pr_builds_gh_command(tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    git = GitActions(merge_pr=True)  # default method = merge
    with patch("hivepilot.services.git_service.subprocess.run") as m:
        git_service.merge_pr(project=project, branch="hivepilot/x", git=git)
    cmd = m.call_args.args[0]
    assert cmd[0] == "gh"
    assert cmd[1:3] == ["pr", "merge"]
    assert "hivepilot/x" in cmd
    assert "--merge" in cmd
    assert m.call_args.kwargs["cwd"] == str(tmp_path)


def test_merge_pr_respects_method(tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    git = GitActions(merge_pr=True, merge_method="squash")
    with patch("hivepilot.services.git_service.subprocess.run") as m:
        git_service.merge_pr(project=project, branch="hivepilot/x", git=git)
    assert "--squash" in m.call_args.args[0]


def test_merge_pr_raises_on_gh_failure(tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    git = GitActions(merge_pr=True)
    with patch("hivepilot.services.git_service.subprocess.run", side_effect=OSError("boom")):
        with pytest.raises(RuntimeError, match="Failed to merge PR"):
            git_service.merge_pr(project=project, branch="hivepilot/z", git=git)


def test_commit_vault_commits_and_pushes(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import MagicMock

    fake = MagicMock()
    fake.git.diff.return_value = "Notes/plan.md"  # staged changes present
    fake.head.is_detached = False
    fake.active_branch.name = "main"
    monkeypatch.setattr(git_service, "Repo", lambda *a, **k: fake)
    assert git_service.commit_vault(tmp_path, "msg", push=True) is True
    # add/commit scoped to the vault pathspec; push explicit remote+branch.
    # `--sparse` staged: a vault on a cone checkout excludes the directories
    # HivePilot writes to, and without it `git add` refuses them and raises.
    # See tests/test_vault_sparse_checkout.py, which drives a real repository --
    # a MagicMock answers `add` the same whatever the cone says, which is why
    # this assertion could pin the broken shape for three days.
    fake.git.add.assert_called_with("-A", "--sparse", "--", str(tmp_path))
    fake.git.commit.assert_called_with("-m", "msg", "--", str(tmp_path))
    fake.git.push.assert_called_once_with("origin", "main")


def test_commit_vault_no_changes_returns_false(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import MagicMock

    fake = MagicMock()
    fake.git.diff.return_value = ""  # nothing staged
    monkeypatch.setattr(git_service, "Repo", lambda *a, **k: fake)
    assert git_service.commit_vault(tmp_path, "m") is False
    fake.git.commit.assert_not_called()


def test_commit_vault_not_a_repo_returns_false(tmp_path: Path, monkeypatch) -> None:
    def boom(*a, **k):
        raise Exception("not a repo")

    monkeypatch.setattr(git_service, "Repo", boom)
    assert git_service.commit_vault(tmp_path, "m") is False


def test_perform_git_actions_merges_when_flag_set(tmp_path: Path) -> None:
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    project = ProjectConfig(path=tmp_path)
    ga = GitActions(merge_pr=True)
    with patch("hivepilot.services.git_service.merge_pr") as mock_merge:
        git_service.perform_git_actions(project_name="p", project=project, git=ga)
    mock_merge.assert_called_once()


def test_promote_pr_builds_gh_ready_command(tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    git = GitActions(promote_pr=True)
    with patch("hivepilot.services.git_service.subprocess.run") as m:
        git_service.promote_pr(project=project, branch="hivepilot/x", git=git)
    cmd = m.call_args.args[0]
    assert cmd[0] == "gh"
    assert cmd[1:3] == ["pr", "ready"]
    assert "hivepilot/x" in cmd
    assert m.call_args.kwargs["cwd"] == str(tmp_path)


def test_promote_pr_raises_on_gh_failure(tmp_path: Path) -> None:
    project = ProjectConfig(path=tmp_path)
    git = GitActions(promote_pr=True)
    with patch("hivepilot.services.git_service.subprocess.run", side_effect=OSError("boom")):
        with pytest.raises(RuntimeError, match="Failed to promote PR"):
            git_service.promote_pr(project=project, branch="hivepilot/z", git=git)


def test_perform_git_actions_promotes_when_flag_set(tmp_path: Path) -> None:
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    project = ProjectConfig(path=tmp_path)
    ga = GitActions(promote_pr=True)
    with patch("hivepilot.services.git_service.promote_pr") as mock_promote:
        git_service.perform_git_actions(project_name="p", project=project, git=ga)
    mock_promote.assert_called_once()


def test_perform_git_actions_promotes_before_merge(tmp_path: Path) -> None:
    """promote_pr must run before merge_pr when both flags are set (draft-then-promote,
    then merge, in one gate stage)."""
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    project = ProjectConfig(path=tmp_path)
    ga = GitActions(promote_pr=True, merge_pr=True)
    calls: list[str] = []
    with (
        patch(
            "hivepilot.services.git_service.promote_pr",
            side_effect=lambda **_: calls.append("promote"),
        ),
        patch(
            "hivepilot.services.git_service.merge_pr", side_effect=lambda **_: calls.append("merge")
        ),
    ):
        git_service.perform_git_actions(project_name="p", project=project, git=ga)
    assert calls == ["promote", "merge"]


def test_perform_git_actions_skips_promote_when_verdict_blocked(tmp_path: Path) -> None:
    """CORRECTNESS: a gate stage whose own report parses to an explicit blocking
    verdict (BLOCK / BLOCKED / REQUEST_CHANGES / NEEDS_HUMAN / ...) must not
    promote the draft PR."""
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    project = ProjectConfig(path=tmp_path)
    ga = GitActions(promote_pr=True)
    blocked_report = "status: BLOCKED\nsummary:\n- found a critical issue\n"
    with patch("hivepilot.services.git_service.promote_pr") as mock_promote:
        git_service.perform_git_actions(
            project_name="p", project=project, git=ga, task_result=blocked_report
        )
    mock_promote.assert_not_called()


def test_perform_git_actions_skips_merge_when_verdict_blocked(tmp_path: Path) -> None:
    """Same gate, applied to merge_pr for safety (a merge is even more final than
    promoting a draft)."""
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    project = ProjectConfig(path=tmp_path)
    ga = GitActions(merge_pr=True)
    blocked_report = "status: REQUEST_CHANGES\nsummary:\n- needs fixes\n"
    with patch("hivepilot.services.git_service.merge_pr") as mock_merge:
        git_service.perform_git_actions(
            project_name="p", project=project, git=ga, task_result=blocked_report
        )
    mock_merge.assert_not_called()


@pytest.mark.parametrize(
    "verdict",
    ["BLOCK", "BLOCKED", "REQUEST_CHANGES", "NEEDS_HUMAN", "REJECTED", "FAILED", "DENIED"],
)
def test_perform_git_actions_skips_promote_on_each_blocking_verdict(
    tmp_path: Path, verdict: str
) -> None:
    """Every known blocking verdict (incl. NEEDS_HUMAN, which defers to a human)
    must skip promote — the PR stays a draft."""
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    project = ProjectConfig(path=tmp_path)
    ga = GitActions(promote_pr=True)
    report = f"status: {verdict}\nsummary:\n- see report\n"
    with patch("hivepilot.services.git_service.promote_pr") as mock_promote:
        git_service.perform_git_actions(
            project_name="p", project=project, git=ga, task_result=report
        )
    mock_promote.assert_not_called()


@pytest.mark.parametrize("verdict", ["PASS", "APPROVE", "APPROVED", "CLEARED", "ADVISORY", "OK"])
def test_perform_git_actions_promotes_on_each_proceed_verdict(tmp_path: Path, verdict: str) -> None:
    """Heterogeneous approval vocabulary: the release gate approves with APPROVE,
    code roles with PASS, security with CLEARED, etc. — all must promote (a
    PASS-only whitelist would wrongly block the release gate on its own approval)."""
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    project = ProjectConfig(path=tmp_path)
    ga = GitActions(promote_pr=True)
    report = f"status: {verdict}\nsummary:\n- looks good\n"
    with patch("hivepilot.services.git_service.promote_pr") as mock_promote:
        git_service.perform_git_actions(
            project_name="p", project=project, git=ga, task_result=report
        )
    mock_promote.assert_called_once()


def test_perform_git_actions_merges_when_verdict_approve(tmp_path: Path) -> None:
    """The release-gate approval verdict (APPROVE) must not be treated as blocked
    for merge either."""
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    project = ProjectConfig(path=tmp_path)
    ga = GitActions(merge_pr=True)
    approve_report = "status: APPROVE\nsummary:\n- ship it\n"
    with patch("hivepilot.services.git_service.merge_pr") as mock_merge:
        git_service.perform_git_actions(
            project_name="p", project=project, git=ga, task_result=approve_report
        )
    mock_merge.assert_called_once()


def test_perform_git_actions_promotes_when_no_task_result(tmp_path: Path) -> None:
    """Legacy behaviour: no task_result (or unstructured text with no status:
    field) must NOT be treated as blocked, since most tasks aren't can_block
    roles and never emitted a structured report before this feature existed."""
    import git as gitlib

    gitlib.Repo.init(tmp_path)
    project = ProjectConfig(path=tmp_path)
    ga = GitActions(promote_pr=True)
    with patch("hivepilot.services.git_service.promote_pr") as mock_promote:
        git_service.perform_git_actions(project_name="p", project=project, git=ga, task_result=None)
    mock_promote.assert_called_once()

    with patch("hivepilot.services.git_service.promote_pr") as mock_promote2:
        git_service.perform_git_actions(
            project_name="p", project=project, git=ga, task_result="plain unstructured output"
        )
    mock_promote2.assert_called_once()


class _CapturingForge:
    """Minimal ForgeProvider double that records the `git` kwarg `open_pr`
    was actually called with (and eagerly reads its resolved `pr_body_file`
    content, since `create_pr` deletes a FALLBACK tempfile as soon as
    `open_pr` returns -- exactly like a real forge would have already
    consumed it by then)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.received_git: GitActions | None = None
        self.received_body: str | None = None

    def open_pr(self, *, project, branch, git) -> None:  # noqa: ANN001
        self.received_git = git
        self.received_body = Path(git.pr_body_file).read_text(encoding="utf-8")


def _register_capturing_forge(name: str) -> _CapturingForge:
    from hivepilot.forges.provider import ForgeRegistry

    forge = _CapturingForge(name)
    ForgeRegistry.register(name, forge, override=True)  # type: ignore[arg-type]
    return forge


def test_create_pr_falls_back_when_pr_body_file_missing(tmp_path: Path) -> None:
    """CORRECTNESS (the headline bug): a declared-but-never-written
    pr_body_file must not crash create_pr and lose the whole stage's output
    -- it must open the PR with a fallback body built from task_result."""
    from hivepilot.forges.provider import FORGE_MAP

    forge = _register_capturing_forge("capturing-missing")
    try:
        project = ProjectConfig(path=tmp_path, forge="capturing-missing")
        ga = GitActions(create_pr=True, pr_body_file="PR_BODY.md")  # never written
        git_service.create_pr(
            project=project, branch="hivepilot/x", git=ga, task_result="Stage did real work."
        )
        assert forge.received_git is not None
        assert "Stage did real work." in forge.received_body
    finally:
        FORGE_MAP.pop("capturing-missing", None)


def test_create_pr_uses_declared_file_unchanged_when_present(tmp_path: Path) -> None:
    """Regression guard: a present, non-blank declared file's content
    reaches the forge exactly as before this fix."""
    from hivepilot.forges.provider import FORGE_MAP

    forge = _register_capturing_forge("capturing-present")
    try:
        (tmp_path / "PR_BODY.md").write_text("## Real agent-written body\n", encoding="utf-8")
        project = ProjectConfig(path=tmp_path, forge="capturing-present")
        ga = GitActions(create_pr=True, pr_body_file="PR_BODY.md")
        git_service.create_pr(project=project, branch="hivepilot/x", git=ga, task_result="ignored")
        assert forge.received_git is not None
        assert forge.received_body == "## Real agent-written body\n"
    finally:
        FORGE_MAP.pop("capturing-present", None)


class TestOneBranchPerRun:
    """A perpetual branch makes every review cumulative.

    `hivepilot/noxys` was reused by every run, so a run pushed into the PR
    opened by an earlier one (`git.pr_already_open`) and the body was never
    refreshed. PR #428 carried three runs' commits from three separate days,
    and the reviewer of run 455 was implicitly reviewing runs 425 and 412 too.

    The branch now carries the RUN ID and nothing else. A slug minted here
    would describe an intention -- the branch is created before any code
    exists, and the objective is not even persisted -- and a descriptive name
    that is wrong is worse than a neutral one. The run id is the key to the
    verdicts, lessons, costs and gate report, so a branch resolves to the
    whole record in one step. Description belongs in the PR title, which is
    written after the work exists.
    """

    def test_branch_carries_the_run_id(self):
        from hivepilot.services.git_service import build_branch_name

        assert build_branch_name(prefix="hivepilot", project_name="noxys", run_id=455) == (
            "hivepilot/noxys/455"
        )

    def test_two_runs_never_share_a_branch(self):
        from hivepilot.services.git_service import build_branch_name

        first = build_branch_name(prefix="hivepilot", project_name="noxys", run_id=455)
        second = build_branch_name(prefix="hivepilot", project_name="noxys", run_id=456)

        assert first != second

    def test_falls_back_to_the_old_name_without_a_run_id(self):
        """Callers with no run in scope must keep working, not crash or invent
        an id that resolves to nothing."""
        from hivepilot.services.git_service import build_branch_name

        assert build_branch_name(prefix="hivepilot", project_name="noxys", run_id=None) == (
            "hivepilot/noxys"
        )

    def test_project_name_is_sanitised_into_a_ref(self):
        """A project name is operator-supplied and becomes a git ref."""
        from hivepilot.services.git_service import build_branch_name

        name = build_branch_name(prefix="hivepilot", project_name="my proj/../x", run_id=7)

        assert ".." not in name
        assert " " not in name
        assert name.count("/") == 2


class TestPrTitleDescribesTheWork:
    """`HivePilot: pipeline implementation` told a reviewer nothing.

    The branch deliberately carries no description (see build_branch_name), so
    the title is the only place a human learns what a PR is for -- and unlike
    the branch, it is built AFTER the commits exist, so it can be accurate
    instead of aspirational.
    """

    def test_uses_the_first_commit_subject(self):
        from hivepilot.services.git_service import build_pr_title

        title = build_pr_title(
            branch="hivepilot/noxys/455",
            commit_subjects=[
                "fix(console): gate desktop AI allowlist behind isAdmin",
                "test(console): cover the revoke path",
            ],
        )

        # The count is part of it: a two-commit PR titled after one of them
        # still hides the other.
        assert title == "fix(console): gate desktop AI allowlist behind isAdmin (+1 more)"

    def test_says_how_many_more_commits_there_are(self):
        """A three-commit PR titled after one of them hides the other two."""
        from hivepilot.services.git_service import build_pr_title

        title = build_pr_title(
            branch="hivepilot/noxys/455",
            commit_subjects=["feat: a", "fix: b", "docs: c"],
        )

        assert "a" in title and "+2" in title

    def test_falls_back_to_the_branch_when_there_are_no_commits(self):
        from hivepilot.services.git_service import build_pr_title

        assert build_pr_title(branch="hivepilot/noxys/455", commit_subjects=[]) == (
            "HivePilot: hivepilot/noxys/455"
        )

    def test_bounded(self):
        """Forges reject or truncate long titles; do it deliberately."""
        from hivepilot.services.git_service import build_pr_title

        title = build_pr_title(branch="b", commit_subjects=["x" * 500])

        assert len(title) <= 120


class TestBranchIsStableAcrossAPipeline:
    """#500 wired the branch to the wrong run id and broke the first pipeline
    that used it.

    `record_run_start` mints a run id per TASK, so `<prefix>/<project>/<run_id>`
    produced one branch per STAGE. Measured on the greenfield run: the
    developer committed to `.../473` and the documentation stage to `.../477`.
    The reviewer — the stage carrying `create_pr: true` — ran on a branch with
    no commits, so no PR was opened, no adversarial review ran, no verdict was
    recorded, and the release manager had nothing to promote.

    Ten stages reported success and the chain between them was severed. The
    branch must be stable for the whole pipeline run, which was the intent all
    along.
    """

    def test_opening_a_pipeline_run_remembers_its_id(self, monkeypatch):
        """The bug #504 shipped was an ORDER, not a value.

        `self._pipeline_run_id = run_id` sat BEFORE the row was created, so it
        stored None, the branch fell back to the per-task id, and a resolver
        unit test passed while the wiring was broken. Recording the row and
        remembering its id are now the same call, so they cannot drift.
        """
        from hivepilot import orchestrator as orch_mod
        from hivepilot.orchestrator import Orchestrator

        monkeypatch.setattr(orch_mod.state_service, "record_run_start", lambda *a, **k: 479)
        orch = Orchestrator.__new__(Orchestrator)
        orch._pipeline_run_id = None

        returned = orch._open_pipeline_run(None, "greenfield")

        assert returned == 479
        assert orch._pipeline_run_id == 479
        # And the branch then follows the pipeline, not the task.
        assert orch._branch_run_id(484) == 479

    def test_an_adopted_run_id_is_remembered_too(self):
        from hivepilot.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch._pipeline_run_id = None

        assert orch._open_pipeline_run(1234, "p") == 1234
        assert orch._pipeline_run_id == 1234

    def test_pipeline_run_id_wins_over_the_task_run_id(self):
        from hivepilot.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch._pipeline_run_id = 468

        assert orch._branch_run_id(473) == 468

    def test_falls_back_to_the_task_run_outside_a_pipeline(self):
        """A standalone task run has no pipeline; its own id is the right one."""
        from hivepilot.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch._pipeline_run_id = None

        assert orch._branch_run_id(473) == 473

    def test_no_id_at_all_is_none_not_a_crash(self):
        from hivepilot.orchestrator import Orchestrator

        orch = Orchestrator.__new__(Orchestrator)
        orch._pipeline_run_id = None

        assert orch._branch_run_id(None) is None


class TestUnattendedCommitsCarryAnIdentity:
    """A freshly cloned repo has no `user.name`, and systemd units have no HOME.

    Measured: the developer stage of a real greenfield run died on

        *** Please tell me who you are

    The `noxys` checkout happens to carry a LOCAL git identity, which is why
    every earlier run committed fine and this defect stayed invisible until a
    repo was cloned fresh.

    #491 fixed exactly this for the corrections path and did not generalise it.
    An unattended commit cannot depend on ambient git configuration, wherever
    it runs.
    """

    def test_the_commit_passes_name_and_email(self, monkeypatch, tmp_path):
        from hivepilot.models import GitActions, ProjectConfig
        from hivepilot.services import git_service

        calls: list[tuple] = []

        class Git:
            def add(self, *a, **k):
                calls.append(("add", a))

            def commit(self, *a, **k):
                calls.append(("commit", a))

            def update_environment(self, **k):
                calls.append(("env", tuple(sorted(k.items()))))

            def checkout(self, *a, **k):
                pass

        class Repo:
            def __init__(self, *a, **k):
                self.git = Git()

            def is_dirty(self, **k):
                return True

        monkeypatch.setattr(git_service, "Repo", Repo)
        monkeypatch.setattr(git_service, "checkout_branch", lambda *a, **k: None)
        monkeypatch.setattr(git_service, "push", lambda *a, **k: None)

        git_service.perform_git_actions(
            project_name="p",
            project=ProjectConfig(path=tmp_path),
            git=GitActions(commit=True, push=False, create_pr=False),
            run_id=479,
        )

        # The identity now travels as GIT_AUTHOR_*/GIT_COMMITTER_*, not as `-c`
        # flags. Two earlier shapes were wrong: `-c` after the subcommand is
        # commit's --reedit-message (git refused outright), and `-c` before it
        # is silently outranked by a GIT_AUTHOR_EMAIL already in the
        # environment -- measured, the commit came out with our name and the
        # ambient email.
        assert next((c for c in calls if c[0] == "commit"), None), "no commit ran"
        env = next((c for c in calls if c[0] == "env"), None)
        assert env is not None, "commit ran without an author identity"
        joined = " ".join(f"{k}={v}" for k, v in env[1])
        assert "GIT_AUTHOR_NAME=" in joined
        assert "GIT_AUTHOR_EMAIL=" in joined
        assert "GIT_COMMITTER_EMAIL=" in joined
