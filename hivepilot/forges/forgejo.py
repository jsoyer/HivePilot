"""ForgejoForge -- a self-hosted Forgejo/Gitea `ForgeProvider` (Forge Phase
2). Talks to the Forgejo/Gitea REST API (`{forge_base_url}/api/v1/...`) via
httpx -- unlike `GitHubForge`, which shells out to the `gh` CLI, there is no
equivalent Forgejo CLI HivePilot can assume is installed, so this provider
speaks the API directly.

Security posture (mirrors `hivepilot.runners.worker_runner`'s worker-token
handling):
  * `forge_base_url` is REQUIRED (there is no universal public Forgejo host
    the way github.com/gitlab.com exist) and MUST be `https://` -- or a
    loopback `http://` for local dev -- enforced by
    `hivepilot.forges.provider.resolve_forge_base_url`/`require_secure_forge_url`.
  * The auth token is resolved from the project's `${secret:FORGEJO_TOKEN}`
    catalog entry (`hivepilot.forges.provider.resolve_forge_token`) --
    NEVER a plaintext environment read -- and is registered for redaction
    before it can appear in any raised error text.

Change-request lifecycle: Forgejo/Gitea also calls these "pull requests"
(same term as GitHub), but -- unlike `gh pr <cmd> <branch>` -- every
PR-scoped endpoint is keyed by a per-repo integer `index`, not the branch
name. `_pr_index_for_branch` resolves it by listing open PRs and matching
`head.ref` against *branch* (the v1 API has no server-side "find PR by head
branch" filter).

`httpx` is imported at module level (not lazily inside each method) so this
module's tests can mock it directly (`patch("hivepilot.forges.forgejo.httpx")`
-- see tests/test_forgejo.py); `hivepilot/forges/__init__.py` is what keeps a
core `hivepilot` install from requiring httpx -- it imports this module
inside a try/except ImportError, so "forgejo" simply isn't registered in
FORGE_MAP (and any project declaring `forge: forgejo` fails closed with a
clear "unknown forge" error) when httpx isn't installed. See the `forge`
extra in pyproject.toml.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import httpx

from hivepilot.config import Settings
from hivepilot.forges.provider import (
    ForgeRegistry,
    extract_pr_url_from_response,
    resolve_forge_base_url,
    resolve_forge_token,
)
from hivepilot.models import GitActions, ProjectConfig
from hivepilot.services.config_provenance import redact_text
from hivepilot.utils.logging import get_logger
from hivepilot.utils.shell import run_command

logger = get_logger(__name__)

_TOKEN_REF = "FORGEJO_TOKEN"
_TIMEOUT = 30.0


class ForgejoForge:
    """`ForgeProvider` for a self-hosted Forgejo/Gitea instance, via its v1 REST API."""

    name = "forgejo"

    # ---- internal helpers ----

    def _headers(self, project: ProjectConfig) -> dict[str, str]:
        token = resolve_forge_token(project, _TOKEN_REF)
        return {"Authorization": f"token {token}", "Content-Type": "application/json"}

    def _api_base(self, project: ProjectConfig) -> str:
        return f"{resolve_forge_base_url(project)}/api/v1"

    def _request(
        self,
        method: str,
        url: str,
        *,
        project: ProjectConfig,
        json_body: dict | None = None,
        params: dict | None = None,
    ) -> httpx.Response:
        headers = self._headers(project)  # resolved (and secret-registered) before any I/O
        try:
            resp = httpx.request(
                method, url, headers=headers, json=json_body, params=params, timeout=_TIMEOUT
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(
                redact_text(f"Forgejo request {method} {url} failed: {type(exc).__name__}")
            ) from None
        if resp.status_code >= 400:
            raise RuntimeError(
                redact_text(
                    f"Forgejo request {method} {url} returned {resp.status_code}: {resp.text[:300]}"
                )
            )
        return resp

    def _pr_index_for_branch(self, project: ProjectConfig, branch: str) -> int:
        slug = project.owner_repo
        url = f"{self._api_base(project)}/repos/{slug}/pulls"
        resp = self._request("GET", url, project=project, params={"state": "open"})
        for pr in resp.json():
            if pr.get("head", {}).get("ref") == branch:
                return int(pr["number"])
        raise RuntimeError(f"No open PR found for branch {branch!r} in {slug!r}")

    # ---- repo lifecycle ----

    def build_repo_url(self, repo: str, protocol: str, project: ProjectConfig | None = None) -> str:
        if project is None:
            raise ValueError(
                "ForgejoForge.build_repo_url requires project (a self-hosted "
                "forge_base_url is per-project, unlike github.com's single fixed host)"
            )
        base_url = resolve_forge_base_url(project)
        if protocol == "https":
            return f"{base_url}/{repo}.git"
        host = urlparse(base_url).hostname
        return f"git@{host}:{repo}.git"

    def repo_exists(self, slug: str, settings: Settings, project: ProjectConfig) -> bool:
        url = f"{self._api_base(project)}/repos/{slug}"
        headers = self._headers(project)
        try:
            resp = httpx.request("GET", url, headers=headers, timeout=_TIMEOUT)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    def create_repo(
        self,
        slug: str,
        *,
        settings: Settings,
        project: ProjectConfig,
        visibility: str,
        description: str | None,
    ) -> None:
        # ASSUMPTION (medium confidence): the configured owner is an
        # organization -- HivePilot projects are typically org-owned, and
        # Gitea/Forgejo's "create as a user" endpoint (/user/repos) only
        # ever creates under the AUTHENTICATED user, never an arbitrary
        # owner. If `owner` isn't an org this call 404s with a clear error
        # from the Forgejo API (never a silent wrong-owner create).
        owner, _, name = slug.partition("/")
        url = f"{self._api_base(project)}/orgs/{owner}/repos"
        body: dict = {"name": name, "private": visibility == "private"}
        if description:
            body["description"] = description
        self._request("POST", url, project=project, json_body=body)

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
            logger.info("forgejo.repo_exists", repo=slug)
        else:
            logger.info("forgejo.repo_create", repo=slug)
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
        url = f"{self._api_base(project)}/repos/{slug}/issues"
        payload: dict = {"title": title}
        if body:
            payload["body"] = body
        if labels:
            payload["labels"] = labels
        self._request("POST", url, project=project, json_body=payload)

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
        del generate_notes  # Forgejo v1 has no server-side "generate notes" flag
        slug = project.owner_repo
        if not slug:
            raise ValueError("owner_repo missing for release creation")
        url = f"{self._api_base(project)}/repos/{slug}/releases"
        payload: dict = {"tag_name": tag}
        if title:
            payload["name"] = title
        if notes_file:
            payload["body"] = Path(notes_file).read_text(encoding="utf-8")
        self._request("POST", url, project=project, json_body=payload)

    # ---- change-request (pull request) lifecycle ----

    def open_pr(self, *, project: ProjectConfig, branch: str, git: GitActions) -> str | None:
        """Open a pull request via the Forgejo/Gitea API.

        Returns the ``html_url`` the FORGE itself reported for the PR it just
        created, or ``None`` when the response carries no usable one
        (propose -> ratify -> dispatch PRD, spec §8). **Never a fabricated
        URL** -- the partition journal shows "—" for ``None``.
        """
        slug = project.owner_repo
        if not slug:
            raise ValueError("owner_repo missing for PR creation")
        base = project.default_branch or "main"
        title = git.pr_title or f"HivePilot: {branch}"
        if git.draft:
            title = f"WIP: {title}"
        body = "Automated pull request opened by HivePilot."
        if git.pr_body_file:
            body = Path(git.pr_body_file).read_text(encoding="utf-8")
        url = f"{self._api_base(project)}/repos/{slug}/pulls"
        payload = {"title": title, "head": branch, "base": base, "body": body}
        resp = self._request("POST", url, project=project, json_body=payload)
        pr_url = extract_pr_url_from_response(resp, "html_url")
        logger.info(
            "forgejo.pr_created",
            project=project.path.name,
            branch=branch,
            base=base,
            draft=git.draft,
            pr_url_captured=pr_url is not None,
        )
        return pr_url

    def promote_pr(self, *, project: ProjectConfig, branch: str, git: GitActions) -> None:
        del git
        slug = project.owner_repo
        index = self._pr_index_for_branch(project, branch)
        url = f"{self._api_base(project)}/repos/{slug}/pulls/{index}"
        resp = self._request("GET", url, project=project)
        current_title = str(resp.json().get("title", ""))
        new_title = current_title[4:] if current_title.startswith("WIP:") else current_title
        self._request("PATCH", url, project=project, json_body={"title": new_title.strip()})
        logger.info("forgejo.pr_promoted", project=project.path.name, branch=branch)

    def comment_pr(self, *, project: ProjectConfig, branch: str, body: str) -> None:
        slug = project.owner_repo
        index = self._pr_index_for_branch(project, branch)
        url = f"{self._api_base(project)}/repos/{slug}/issues/{index}/comments"
        self._request("POST", url, project=project, json_body={"body": body})
        logger.info("forgejo.pr_commented", project=project.path.name, branch=branch)

    def pr_status(self, *, project: ProjectConfig, branch: str) -> str:
        slug = project.owner_repo
        index = self._pr_index_for_branch(project, branch)
        url = f"{self._api_base(project)}/repos/{slug}/pulls/{index}"
        resp = self._request("GET", url, project=project)
        data = resp.json()
        if data.get("merged"):
            return "MERGED"
        state = str(data.get("state", "")).upper()
        return state or "UNKNOWN"

    def merge_pr(self, *, project: ProjectConfig, branch: str, git: GitActions) -> None:
        slug = project.owner_repo
        index = self._pr_index_for_branch(project, branch)
        method = git.merge_method if git.merge_method in {"merge", "squash", "rebase"} else "merge"
        url = f"{self._api_base(project)}/repos/{slug}/pulls/{index}/merge"
        self._request("POST", url, project=project, json_body={"Do": method})
        logger.info("forgejo.pr_merged", project=project.path.name, branch=branch, method=method)


ForgeRegistry.register("forgejo", ForgejoForge())
