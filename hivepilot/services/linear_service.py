from __future__ import annotations

import re

import requests

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_LINEAR_API = "https://api.linear.app/graphql"


def _headers() -> dict:
    """Return auth headers. Raises RuntimeError if not configured."""
    if not settings.linear_api_key:
        raise RuntimeError("LINEAR_API_KEY not configured")
    return {"Authorization": settings.linear_api_key, "Content-Type": "application/json"}


def _gql(query: str, variables: dict | None = None) -> dict:
    """Execute a GraphQL query. Raises RuntimeError on errors."""
    payload: dict = {"query": query}
    if variables:
        payload["variables"] = variables
    resp = requests.post(_LINEAR_API, json=payload, headers=_headers(), timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        messages = "; ".join(e.get("message", str(e)) for e in data["errors"])
        raise RuntimeError(f"Linear API error: {messages}")
    return data.get("data", {})


def create_issue(
    title: str,
    description: str = "",
    *,
    team_id: str | None = None,
    project_id: str | None = None,
    priority: int = 0,
) -> dict:
    """Create a Linear issue. Returns the created issue dict with id and url.

    priority: 0=no priority, 1=urgent, 2=high, 3=medium, 4=low
    """
    resolved_team_id = team_id or settings.linear_team_id
    if not resolved_team_id:
        raise RuntimeError("linear_team_id not configured and no team_id provided")
    resolved_project_id = project_id or settings.linear_default_project_id

    input_fields: dict = {
        "title": title,
        "teamId": resolved_team_id,
        "priority": priority,
    }
    if description:
        input_fields["description"] = description
    if resolved_project_id:
        input_fields["projectId"] = resolved_project_id

    query = """
    mutation CreateIssue($input: IssueCreateInput!) {
        issueCreate(input: $input) {
            success
            issue {
                id
                identifier
                title
                url
                priority
                state {
                    name
                }
            }
        }
    }
    """
    data = _gql(query, {"input": input_fields})
    result = data.get("issueCreate", {})
    if not result.get("success"):
        raise RuntimeError("Linear issue creation returned success=false")
    return result.get("issue", {})


def update_issue(
    issue_id: str,
    *,
    state_name: str | None = None,
    description: str | None = None,
) -> dict:
    """Update an existing issue's state or description."""
    input_fields: dict = {}

    if state_name is not None:
        # Resolve the state ID by fetching workflow states for the issue's team
        state_id = _resolve_state_id_for_issue(issue_id, state_name)
        if state_id:
            input_fields["stateId"] = state_id

    if description is not None:
        input_fields["description"] = description

    if not input_fields:
        # Nothing to update — fetch and return current issue
        return _get_issue(issue_id)

    query = """
    mutation UpdateIssue($id: String!, $input: IssueUpdateInput!) {
        issueUpdate(id: $id, input: $input) {
            success
            issue {
                id
                identifier
                title
                url
                state {
                    name
                }
            }
        }
    }
    """
    data = _gql(query, {"id": issue_id, "input": input_fields})
    result = data.get("issueUpdate", {})
    if not result.get("success"):
        raise RuntimeError("Linear issue update returned success=false")
    return result.get("issue", {})


def _get_issue(issue_id: str) -> dict:
    """Fetch a single issue by id."""
    query = """
    query GetIssue($id: String!) {
        issue(id: $id) {
            id
            identifier
            title
            url
            state {
                name
            }
        }
    }
    """
    data = _gql(query, {"id": issue_id})
    return data.get("issue", {})


def _resolve_state_id_for_issue(issue_id: str, state_name: str) -> str | None:
    """Fetch the team for the given issue, then resolve state_name to an ID."""
    query = """
    query IssueTeam($id: String!) {
        issue(id: $id) {
            team {
                id
            }
        }
    }
    """
    data = _gql(query, {"id": issue_id})
    team_id = (data.get("issue") or {}).get("team", {}).get("id")
    if not team_id:
        return None
    states = get_workflow_states(team_id)
    for state in states:
        if state.get("name", "").lower() == state_name.lower():
            return state.get("id")
    return None


def get_teams() -> list[dict]:
    """List teams (id, name, key)."""
    query = """
    query {
        teams {
            nodes {
                id
                name
                key
            }
        }
    }
    """
    data = _gql(query)
    return data.get("teams", {}).get("nodes", [])


def get_workflow_states(team_id: str) -> list[dict]:
    """List workflow states for a team (id, name, type)."""
    query = """
    query WorkflowStates($teamId: String!) {
        workflowStates(filter: { team: { id: { eq: $teamId } } }) {
            nodes {
                id
                name
                type
            }
        }
    }
    """
    data = _gql(query, {"teamId": team_id})
    return data.get("workflowStates", {}).get("nodes", [])


def verify_webhook(body: bytes, signature: str) -> bool:
    """Verify Linear webhook HMAC-SHA256 signature."""
    import hashlib
    import hmac

    secret = settings.linear_webhook_secret
    if not secret:
        return True  # no secret configured — accept all
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def on_run_failure(project: str, task: str, error: str) -> str | None:
    """Create a Linear issue for a failed run. Returns issue URL or None."""
    if not settings.linear_api_key:
        return None
    title = f"[HivePilot] {project}/{task} failed"
    description = f"**Project:** {project}\n**Task:** {task}\n**Error:** {error}"
    try:
        issue = create_issue(title, description, priority=2)  # high priority
        return issue.get("url")
    except Exception as exc:  # noqa: BLE001
        logger.warning("linear.on_run_failure.error", error=str(exc))
        return None


def on_run_success(project: str, task: str) -> None:
    """Log a successful run (no-op if not configured)."""
    if settings.linear_api_key:
        logger.info("linear.run_success", project=project, task=task)


# ---------------------------------------------------------------------------
# [HP] title pattern: "[HP] project/task"
# ---------------------------------------------------------------------------
_HP_TITLE_RE = re.compile(r"\[HP\]\s+([^/]+)/(.+)")


def handle_webhook(payload: dict) -> str:
    """
    Handle a Linear webhook event.

    Supports: Issue created/updated with label 'hivepilot' triggers a run.

    Payload type field:
    - "Issue" with action "create" or "update" and label "hivepilot"
      -> extract project+task from issue title format "[HP] project/task"
      -> trigger orchestrator.run_task()
    """
    event_type = payload.get("type", "")
    action = payload.get("action", "")

    if event_type != "Issue" or action not in ("create", "update"):
        return f"ignored event type={event_type} action={action}"

    data = payload.get("data", {})
    labels = data.get("labels", []) or []
    label_names = [lbl.get("name", "").lower() for lbl in labels]

    if "hivepilot" not in label_names:
        return "ignored: missing 'hivepilot' label"

    title = data.get("title", "")
    match = _HP_TITLE_RE.match(title)
    if not match:
        return f"ignored: title does not match [HP] project/task pattern: {title!r}"

    project_name = match.group(1).strip()
    task_name = match.group(2).strip()

    logger.info("linear.webhook.trigger", project=project_name, task=task_name)

    try:
        from hivepilot.orchestrator import Orchestrator

        orch = Orchestrator()
        results = orch.run_task(
            project_names=[project_name],
            task_name=task_name,
            extra_prompt=None,
            auto_git=False,
        )
        statuses = [("success" if r.success else "failed") for r in results]
        return f"triggered {project_name}/{task_name}: {', '.join(statuses)}"
    except Exception as exc:  # noqa: BLE001
        logger.error("linear.webhook.run_error", project=project_name, task=task_name, error=str(exc))
        return f"error triggering {project_name}/{task_name}: {exc}"
