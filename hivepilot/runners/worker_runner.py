"""RemoteWorkerRunner — forward a step to a remote HivePilot worker (W1).

When a role/runner's ``host`` is an ``http(s)://`` URL, the hub does not run the
agent locally or over SSH — it POSTs the step to a ``hivepilot worker`` running on
that machine, which executes the real runner locally and returns its stdout.
This keeps remote execution behind the same ``host`` abstraction as SSH.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import requests

from hivepilot.config import Settings
from hivepilot.models import RunnerDefinition
from hivepilot.runners.base import BaseRunner, RunnerPayload
from hivepilot.utils.logging import get_logger

logger = get_logger(__name__)

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _require_secure_transport(host: str) -> None:
    """Refuse to send the worker bearer token over plaintext http to a non-loopback
    host — require https:// (or a loopback http:// for local dev)."""
    parsed = urlparse(host)
    if parsed.scheme == "http" and (parsed.hostname or "") not in _LOOPBACK_HOSTS:
        raise ValueError(
            f"Refusing plaintext http to non-loopback worker {parsed.hostname!r}; "
            "use https:// (or a loopback host)."
        )


@dataclass
class RemoteWorkerRunner(BaseRunner):
    definition: RunnerDefinition
    settings: Settings

    def _post(self, payload: RunnerPayload) -> str:
        host = (self.definition.host or "").rstrip("/")
        _require_secure_transport(host)
        url = f"{host}/run-step"
        body: dict[str, Any] = {
            "kind": self.definition.kind,
            "model": self.definition.model,
            "command": self.definition.command,
            "project_name": payload.project_name,
            "project_path": str(payload.project.path),
            "task_name": payload.task_name,
            "step_name": payload.step.name,
            "prompt_file": payload.step.prompt_file,
            "metadata": payload.metadata,
        }
        headers = {}
        if self.settings.worker_token:
            headers["Authorization"] = f"Bearer {self.settings.worker_token}"
        timeout = payload.step.timeout_seconds or self.definition.timeout_seconds
        logger.info(
            "worker_runner.dispatch", url=url, kind=self.definition.kind, step=payload.step.name
        )
        resp = requests.post(url, json=body, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json().get("output", "")

    def capture(self, payload: RunnerPayload) -> str:
        return self._post(payload)

    def run(self, payload: RunnerPayload) -> None:
        self._post(payload)
