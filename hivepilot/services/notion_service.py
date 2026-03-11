from __future__ import annotations

import requests

from hivepilot.config import settings
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_NOTION_API = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------


def _headers() -> dict:
    if not settings.notion_token:
        raise RuntimeError("HIVEPILOT_NOTION_TOKEN not configured")
    return {
        "Authorization": f"Bearer {settings.notion_token}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }


def _req(method: str, path: str, **kwargs) -> dict:
    """Make a Notion API request. Raises RuntimeError on non-2xx."""
    url = f"{_NOTION_API}{path}"
    resp = requests.request(method, url, headers=_headers(), timeout=10, **kwargs)
    if not resp.ok:
        raise RuntimeError(
            f"Notion API error {resp.status_code} {method} {path}: {resp.text}"
        )
    return resp.json()


# ---------------------------------------------------------------------------
# Run logging (primary use case)
# ---------------------------------------------------------------------------


def log_run(
    *,
    run_id: int,
    project: str,
    task: str,
    status: str,
    detail: str = "",
    started_at: str = "",
) -> str | None:
    """
    Create a page in the runs database representing this run.
    Returns the Notion page ID, or None if not configured.

    Expected database schema:
      - Name      (title)     : "{project} / {task} #{run_id}"
      - Status    (select)    : status value
      - Project   (rich_text) : project name
      - Task      (rich_text) : task name
      - RunID     (number)    : run_id
      - Detail    (rich_text) : error or detail message
      - StartedAt (date)      : ISO timestamp
    """
    if not settings.notion_token or not settings.notion_runs_database_id:
        return None

    title = f"{project} / {task} #{run_id}"
    properties: dict = {
        "Name": {
            "title": [{"text": {"content": title}}]
        },
        "Status": {
            "select": {"name": status}
        },
        "Project": {
            "rich_text": [{"text": {"content": project}}]
        },
        "Task": {
            "rich_text": [{"text": {"content": task}}]
        },
        "RunID": {
            "number": run_id
        },
        "Detail": {
            "rich_text": [{"text": {"content": detail}}]
        },
    }
    if started_at:
        properties["StartedAt"] = {"date": {"start": started_at}}

    payload = {
        "parent": {"database_id": settings.notion_runs_database_id},
        "properties": properties,
    }

    try:
        result = _req("POST", "/pages", json=payload)
        page_id: str = result["id"]
        logger.info("notion.log_run", run_id=run_id, page_id=page_id, status=status)
        return page_id
    except Exception as exc:  # noqa: BLE001
        logger.warning("notion.log_run.failed", run_id=run_id, error=str(exc))
        return None


def update_run(page_id: str, *, status: str, detail: str = "") -> None:
    """Update status and detail of an existing run page."""
    if not settings.notion_token:
        return
    properties: dict = {
        "Status": {"select": {"name": status}},
        "Detail": {"rich_text": [{"text": {"content": detail}}]},
    }
    try:
        _req("PATCH", f"/pages/{page_id}", json={"properties": properties})
        logger.info("notion.update_run", page_id=page_id, status=status)
    except Exception as exc:  # noqa: BLE001
        logger.warning("notion.update_run.failed", page_id=page_id, error=str(exc))


def setup_database(parent_page_id: str) -> str:
    """
    Create the HivePilot runs database in Notion under parent_page_id.
    Returns the new database_id.

    Creates columns: Name, Status, Project, Task, RunID, Detail, StartedAt.
    """
    payload = {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "title": [{"type": "text", "text": {"content": "HivePilot Runs"}}],
        "properties": {
            "Name": {"title": {}},
            "Status": {
                "select": {
                    "options": [
                        {"name": "running", "color": "blue"},
                        {"name": "success", "color": "green"},
                        {"name": "failed", "color": "red"},
                        {"name": "pending", "color": "yellow"},
                    ]
                }
            },
            "Project": {"rich_text": {}},
            "Task": {"rich_text": {}},
            "RunID": {"number": {"format": "number"}},
            "Detail": {"rich_text": {}},
            "StartedAt": {"date": {}},
        },
    }
    result = _req("POST", "/databases", json=payload)
    database_id: str = result["id"]
    logger.info("notion.setup_database", database_id=database_id)
    return database_id


def list_recent_runs(limit: int = 10) -> list[dict]:
    """Query the runs database, sorted by StartedAt descending."""
    if not settings.notion_token or not settings.notion_runs_database_id:
        return []

    payload = {
        "sorts": [{"property": "StartedAt", "direction": "descending"}],
        "page_size": limit,
    }
    try:
        result = _req(
            "POST",
            f"/databases/{settings.notion_runs_database_id}/query",
            json=payload,
        )
        return result.get("results", [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("notion.list_recent_runs.failed", error=str(exc))
        return []


def get_database_info() -> dict:
    """Return basic info about the configured runs database."""
    if not settings.notion_token or not settings.notion_runs_database_id:
        return {}
    try:
        return _req("GET", f"/databases/{settings.notion_runs_database_id}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("notion.get_database_info.failed", error=str(exc))
        return {}


# ---------------------------------------------------------------------------
# Hook functions called from orchestrator
# ---------------------------------------------------------------------------


def on_run_start(run_id: int, project: str, task: str) -> str | None:
    """Log run start to Notion. Returns page_id or None."""
    return log_run(run_id=run_id, project=project, task=task, status="running")


def on_run_complete(page_id: str | None, *, status: str, detail: str = "") -> None:
    """Update run status in Notion. Silently no-ops if page_id is None."""
    if not page_id:
        return
    update_run(page_id, status=status, detail=detail)
