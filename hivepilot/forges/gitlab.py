"""GitLabForge -- a `ForgeProvider` for GitLab (SaaS gitlab.com or a
self-hosted instance), Forge Phase 2. Talks to the GitLab REST API
(`{forge_base_url or https://gitlab.com}/api/v4/...`) via httpx.

GitLab's change-request concept is a **Merge Request** (MR), not a "pull
request" -- this provider maps the `ForgeProvider` interface's PR-shaped
methods onto GitLab's MR endpoints: `open_pr` -> `POST .../merge_requests`,
`merge_pr` -> `PUT .../merge_requests/{iid}/merge`, `comment_pr` ->
`POST .../merge_requests/{iid}/notes`, `pr_status` -> the MR's `state`. An
MR is identified by its per-project `iid` (internal id), resolved from
*branch* via `_mr_iid_for_branch` -- unlike Forgejo's v1 API, GitLab's
`GET .../merge_requests` DOES support a server-side `source_branch` filter,
so no client-side scan is needed.

Security posture (mirrors `hivepilot.runners.worker_runner` /
`hivepilot.forges.forgejo`):
  * `forge_base_url` is OPTIONAL -- unlike Forgejo, GitLab has a real public
    SaaS host (`https://gitlab.com`), used as the default when unset. Either
    way the resolved URL MUST be `https://` (or a loopback `http://` for
    local dev), enforced by
    `hivepilot.forges.provider.resolve_forge_base_url`/`require_secure_forge_url`.
  * The auth token is resolved from the project's `${secret:GITLAB_TOKEN}`
    catalog entry (`hivepilot.forges.provider.resolve_forge_token`) --
    NEVER a plaintext environment read -- and is registered for redaction
    before it can appear in any raised error text. Sent as the
    `PRIVATE-TOKEN` header (GitLab's convention; NOT `Authorization`).

`httpx` is imported at module level (not lazily inside each method) so this
module's tests can mock it directly (`patch("hivepilot.forges.gitlab.httpx")`
-- see tests/test_gitlab.py); `hivepilot/forges/__init__.py` is what keeps a
core `hivepilot` install from requiring httpx -- see the module docstring in
`hivepilot/forges/forgejo.py` for the full rationale (identical here).

Scope note (deviation, documented rather than silently trimmed): GitLab's
merge endpoint distinguishes `squash` (bool) from a true "rebase-then-merge"
(a SEPARATE `PUT .../rebase` endpoint plus a poll for completion). This
phase maps `merge_method: "squash"` to `squash: true` and treats
`"merge"`/`"rebase"` identically (a plain merge) -- true rebase-before-merge
is left for a follow-up if it's actually needed.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote, urlparse

import httpx

from hivepilot.config import Settings
from hivepilot.forges.provider import (
    ForgeRegistry,
    resolve_forge_base_url,
    resolve_forge_token,
)
from hivepilot.models import GitActions, ProjectConfig
from hivepilot.services.config_provenance import redact_text
from hivepilot.utils.logging import get_logger
from hivepilot.utils.shell import run_command

logger = get_logger(__name__)

_TOKEN_REF = "GITLAB_TOKEN"
_PUBLIC_GITLAB = "https://gitlab.com"
_TIMEOUT = 30.0


class GitLabForge:
    """`ForgeProvider` for GitLab (SaaS or self-hosted), via its v4 REST API."""

    name = "gitlab"

    # ---- internal helpers ----

    def _headers(self, project: ProjectConfig) -> dict[str, str]:
        token = resolve_forge_token(project, _TOKEN_REF)
        return {"PRIVATE-TOKEN": token, "Content-Type": "application/json"}

    def _base_url(self, project: ProjectConfig) -> str:
        return resolve_forge_base_url(project, default=_PUBLIC_GITLAB)

    def _api_base(self, project: ProjectConfig) -> str:
        return f"{self._base_url(project)}/api/v4"

    def _project_path(self, project: ProjectConfig) -> str:
        slug = project.owner_repo
        if not slug:
            raise ValueError("owner_repo missing in project configuration.")
        return quote(slug, safe="")

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
                redact_text(f"GitLab request {method} {url} failed: {type(exc).__name__}")
            ) from None
        if resp.status_code >= 400:
            raise RuntimeError(
                redact_text(
                    f"GitLab request {method} {url} returned {resp.status_code}: {resp.text[:300]}"
                )
            )
        return resp

    def _mr_iid_for_branch(self, project: ProjectConfig, branch: str) -> int:
        project_id = self._project_path(project)
        url = f"{self._api_base(project)}/projects/{project_id}/merge_requests"
        resp = self._request(
            "GET", url, project=project, params={"source_branch": branch, "state": "opened"}
        )
        results = resp.json()
        if not results:
            raise RuntimeError(f"No open MR found for branch {branch!r} in {project.owner_repo!r}")
        return int(results[0]["iid"])

    # ---- repo lifecycle ----

    def build_repo_url(self, repo: str, protocol: str, project: ProjectConfig | None = None) -> str:
        if project is None:
            raise ValueError(
                "GitLabForge.build_repo_url requires project (a self-hosted "
                "forge_base_url is per-project)"
            )
        base_url = self._base_url(project)
        if protocol == "https":
            return f"{base_url}/{repo}.git"
        host = urlparse(base_url).hostname
        return f"git@{host}:{repo}.git"

    def repo_exists(self, slug: str, settings: Settings, project: ProjectConfig) -> bool:
        url = f"{self._api_base(project)}/projects/{quote(slug, safe='')}"
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
        owner, _, name = slug.partition("/")
        # Resolve the owner's namespace id (group or user) so the new project
        # lands under `owner`, not just the token owner's personal namespace.
        ns_url = f"{self._api_base(project)}/namespaces/{quote(owner, safe='')}"
        ns_resp = self._request("GET", ns_url, project=project)
        namespace_id = ns_resp.json().get("id")
        url = f"{self._api_base(project)}/projects"
        body: dict = {
            "name": name,
            "path": name,
            "visibility": "private" if visibility == "private" else "public",
        }
        if namespace_id is not None:
            body["namespace_id"] = namespace_id
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
            logger.info("gitlab.repo_exists", repo=slug)
        else:
            logger.info("gitlab.repo_create", repo=slug)
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
        project_id = self._project_path(project)
        url = f"{self._api_base(project)}/projects/{project_id}/issues"
        payload: dict = {"title": title}
        if body:
            payload["description"] = body
        if labels:
            payload["labels"] = ",".join(labels)
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
        del generate_notes  # GitLab has no server-side "generate notes" flag
        project_id = self._project_path(project)
        url = f"{self._api_base(project)}/projects/{project_id}/releases"
        payload: dict = {"tag_name": tag}
        if title:
            payload["name"] = title
        if notes_file:
            payload["description"] = Path(notes_file).read_text(encoding="utf-8")
        self._request("POST", url, project=project, json_body=payload)

    # ---- change-request (merge request) lifecycle ----

    def open_pr(self, *, project: ProjectConfig, branch: str, git: GitActions) -> None:
        project_id = self._project_path(project)
        base = project.default_branch or "main"
        title = git.pr_title or f"HivePilot: {branch}"
        if git.draft:
            title = f"Draft: {title}"
        description = "Automated merge request opened by HivePilot."
        if git.pr_body_file:
            description = Path(git.pr_body_file).read_text(encoding="utf-8")
        url = f"{self._api_base(project)}/projects/{project_id}/merge_requests"
        payload = {
            "source_branch": branch,
            "target_branch": base,
            "title": title,
            "description": description,
        }
        self._request("POST", url, project=project, json_body=payload)
        logger.info(
            "gitlab.mr_created",
            project=project.path.name,
            branch=branch,
            base=base,
            draft=git.draft,
        )

    def promote_pr(self, *, project: ProjectConfig, branch: str, git: GitActions) -> None:
        del git
        project_id = self._project_path(project)
        iid = self._mr_iid_for_branch(project, branch)
        url = f"{self._api_base(project)}/projects/{project_id}/merge_requests/{iid}"
        resp = self._request("GET", url, project=project)
        current_title = str(resp.json().get("title", ""))
        new_title = current_title
        for prefix in ("Draft:", "WIP:"):
            if new_title.startswith(prefix):
                new_title = new_title[len(prefix) :]
                break
        self._request("PUT", url, project=project, json_body={"title": new_title.strip()})
        logger.info("gitlab.mr_promoted", project=project.path.name, branch=branch)

    def comment_pr(self, *, project: ProjectConfig, branch: str, body: str) -> None:
        project_id = self._project_path(project)
        iid = self._mr_iid_for_branch(project, branch)
        url = f"{self._api_base(project)}/projects/{project_id}/merge_requests/{iid}/notes"
        self._request("POST", url, project=project, json_body={"body": body})
        logger.info("gitlab.mr_commented", project=project.path.name, branch=branch)

    def pr_status(self, *, project: ProjectConfig, branch: str) -> str:
        project_id = self._project_path(project)
        url = f"{self._api_base(project)}/projects/{project_id}/merge_requests"
        resp = self._request(
            "GET", url, project=project, params={"source_branch": branch, "state": "opened"}
        )
        results = resp.json()
        if not results:
            raise RuntimeError(f"No open MR found for branch {branch!r} in {project.owner_repo!r}")
        state = str(results[0].get("state", "")).upper()
        return state or "UNKNOWN"

    def merge_pr(self, *, project: ProjectConfig, branch: str, git: GitActions) -> None:
        project_id = self._project_path(project)
        iid = self._mr_iid_for_branch(project, branch)
        method = git.merge_method if git.merge_method in {"merge", "squash", "rebase"} else "merge"
        url = f"{self._api_base(project)}/projects/{project_id}/merge_requests/{iid}/merge"
        self._request("PUT", url, project=project, json_body={"squash": method == "squash"})
        logger.info("gitlab.mr_merged", project=project.path.name, branch=branch, method=method)


ForgeRegistry.register("gitlab", GitLabForge())
