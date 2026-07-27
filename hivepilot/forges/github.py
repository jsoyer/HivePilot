"""GitHubForge — the default `ForgeProvider` (Phase 1 of the forge plugin
type PRD). Every operation here is a direct, unmodified port of the
`gh`/`git` CLI logic that used to live inline in
`hivepilot.services.github_service` / `hivepilot.services.git_service` --
moved behind the `ForgeProvider` interface so a later Forgejo/GitLab
provider can implement the same surface. Because `forge: "github"` is the
default (every pre-existing project), and this class shells out to the
exact same commands those two modules used to build inline, behaviour is
byte-identical to before this module existed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tenacity import retry, stop_after_attempt, wait_exponential

from hivepilot.config import Settings
from hivepilot.config import settings as _settings
from hivepilot.forges.provider import ForgeRegistry, extract_pr_url
from hivepilot.models import GitActions, ProjectConfig
from hivepilot.utils.logging import get_logger
from hivepilot.utils.shell import run_command

logger = get_logger(__name__)


class GitHubForge:
    """`ForgeProvider` for github.com, via the `gh` CLI."""

    name = "github"

    # ---- repo lifecycle (ported from hivepilot.services.github_service) ----

    def repo_exists(self, slug: str, settings: Settings, project: ProjectConfig) -> bool:
        result = run_command(
            [settings.gh_command, "repo", "view", slug],
            cwd=project.path,
            check=False,
            capture_output=True,
        )
        return result.returncode == 0

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=5))
    def create_repo(
        self,
        slug: str,
        *,
        settings: Settings,
        project: ProjectConfig,
        visibility: str,
        description: str | None,
    ) -> None:
        args = [settings.gh_command, "repo", "create", slug, "--confirm"]
        if visibility == "private":
            args.append("--private")
        else:
            args.append("--public")
        if description:
            args.extend(["--description", description])
        run_command(args, cwd=project.path)

    def ensure_repository(
        self,
        project: ProjectConfig,
        settings: Settings,
        *,
        push: bool,
        set_remote: bool = True,
        remote_protocol: str = "ssh",
        visibility: str = "private",
    ) -> None:
        remote_protocol = remote_protocol.lower()
        visibility = visibility.lower()
        if remote_protocol not in {"ssh", "https"}:
            raise ValueError("remote_protocol must be 'ssh' or 'https'")
        if visibility not in {"private", "public"}:
            raise ValueError("visibility must be 'private' or 'public'")
        slug = project.owner_repo
        if not slug:
            raise ValueError("owner_repo missing in project configuration.")
        if self.repo_exists(slug, settings, project):
            logger.info("github.repo_exists", repo=slug)
        else:
            logger.info("github.repo_create", repo=slug)
            self.create_repo(
                slug,
                settings=settings,
                project=project,
                visibility=visibility,
                description=project.description,
            )
        if set_remote:
            run_command(
                [
                    settings.git_command,
                    "remote",
                    "set-url",
                    "origin",
                    self.build_repo_url(slug, remote_protocol, project),
                ],
                cwd=project.path,
                check=False,
            )
        if push:
            run_command(
                [settings.git_command, "push", "-u", "origin", project.default_branch],
                cwd=project.path,
                check=False,
            )

    def create_issue(
        self,
        *,
        project: ProjectConfig,
        settings: Settings,
        title: str,
        body: str | None,
        labels: list[str],
    ) -> None:
        slug = project.owner_repo
        if not slug:
            raise ValueError("owner_repo missing for issue creation")
        args = [
            settings.gh_command,
            "issue",
            "create",
            "--repo",
            slug,
            "--title",
            title,
        ]
        if body:
            args.extend(["--body", body])
        for label in labels:
            args.extend(["--label", label])
        run_command(args, cwd=project.path)

    def create_release(
        self,
        *,
        project: ProjectConfig,
        settings: Settings,
        tag: str,
        title: str | None,
        notes_file: Path | None = None,
        generate_notes: bool = True,
    ) -> None:
        slug = project.owner_repo
        if not slug:
            raise ValueError("owner_repo missing for release creation")
        args = [
            settings.gh_command,
            "release",
            "create",
            tag,
            "--repo",
            slug,
        ]
        if generate_notes and not notes_file:
            args.append("--generate-notes")
        if title:
            args.extend(["--title", title])
        if notes_file:
            args.extend(["--notes-file", str(notes_file)])
        run_command(args, cwd=project.path)

    def build_repo_url(self, repo: str, protocol: str, project: ProjectConfig | None = None) -> str:
        # `project` is accepted (Phase 2 interface widening -- see
        # hivepilot.forges.provider.ForgeProvider.build_repo_url) but unused:
        # github.com is a single fixed, always-https host, unlike a
        # self-hosted Forgejo/GitLab instance whose URL depends on the
        # project's own `forge_base_url`. Byte-identical output either way.
        del project
        if protocol == "https":
            return f"https://github.com/{repo}.git"
        return f"git@github.com:{repo}.git"

    # ---- change-request lifecycle (ported from hivepilot.services.git_service) ----

    def open_pr(self, *, project: ProjectConfig, branch: str, git: GitActions) -> str | None:
        """Open a pull request via the gh CLI (run from the project repo).

        Returns the PR's URL, or ``None`` when one could not be read back
        from ``gh``'s output (propose -> ratify -> dispatch PRD, spec §8).
        **Never a fabricated/derived URL** -- an unreadable URL is reported
        honestly as ``None`` and the partition journal then shows "—", the
        same stated position as the ``steps.role`` migration comment in
        ``state_service``. `gh pr create` prints exactly one line, the URL of
        the PR it just created, so this reads it back rather than guessing at
        `https://github.com/<slug>/pull/<n>` (which would require a number
        this call never learns).

        Only ``stdout`` is captured -- ``stderr`` still streams to the
        console, so a failure is exactly as visible as it was before, and
        ``check=True`` still raises on a non-zero exit.
        """
        base = project.default_branch or "main"
        title = git.pr_title or f"HivePilot: {branch}"
        cmd = [
            _settings.gh_command,
            "pr",
            "create",
            "--base",
            base,
            "--head",
            branch,
            "--title",
            title,
        ]
        if git.draft:
            cmd.append("--draft")
        if git.pr_body_file:
            cmd += ["--body-file", git.pr_body_file]
        else:
            cmd += ["--body", "Automated pull request opened by HivePilot."]
        try:
            completed = subprocess.run(
                cmd, cwd=str(project.path), check=True, text=True, stdout=subprocess.PIPE
            )
            url = extract_pr_url(getattr(completed, "stdout", None))
            logger.info(
                "git.pr_created",
                project=project.path.name,
                branch=branch,
                base=base,
                draft=git.draft,
                pr_url_captured=url is not None,
            )
            return url
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Failed to create PR for {project.path.name}: {exc}") from exc

    def promote_pr(self, *, project: ProjectConfig, branch: str, git: GitActions) -> None:
        """Mark *branch*'s draft PR ready for review via gh -- release-gate promotion."""
        cmd = [_settings.gh_command, "pr", "ready", branch]
        try:
            subprocess.run(cmd, cwd=str(project.path), check=True, text=True)
            logger.info("git.pr_promoted", project=project.path.name, branch=branch)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Failed to promote PR for {project.path.name}: {exc}") from exc

    def merge_pr(self, *, project: ProjectConfig, branch: str, git: GitActions) -> None:
        """Merge the open PR for *branch* via gh."""
        method = git.merge_method if git.merge_method in {"merge", "squash", "rebase"} else "merge"
        cmd = [_settings.gh_command, "pr", "merge", branch, f"--{method}"]
        try:
            subprocess.run(cmd, cwd=str(project.path), check=True, text=True)
            logger.info("git.pr_merged", project=project.path.name, branch=branch, method=method)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Failed to merge PR for {project.path.name}: {exc}") from exc

    # ---- NEW in Phase 1: complete the ForgeProvider interface ----

    def comment_pr(self, *, project: ProjectConfig, branch: str, body: str) -> None:
        """Post a comment on *branch*'s PR via `gh pr comment`."""
        cmd = [_settings.gh_command, "pr", "comment", branch, "--body", body]
        try:
            subprocess.run(cmd, cwd=str(project.path), check=True, text=True)
            logger.info("git.pr_commented", project=project.path.name, branch=branch)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Failed to comment on PR for {project.path.name}: {exc}") from exc

    def pr_status(self, *, project: ProjectConfig, branch: str) -> str:
        """Return *branch*'s PR `state` (OPEN/CLOSED/MERGED) via `gh pr view`."""
        cmd = [_settings.gh_command, "pr", "view", branch, "--json", "state", "-q", ".state"]
        try:
            result = subprocess.run(
                cmd, cwd=str(project.path), check=True, text=True, capture_output=True
            )
            return result.stdout.strip()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Failed to fetch PR status for {project.path.name}: {exc}") from exc


ForgeRegistry.register("github", GitHubForge())
