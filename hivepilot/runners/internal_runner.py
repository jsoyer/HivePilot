from __future__ import annotations

from dataclasses import dataclass

from hivepilot.config import Settings
from hivepilot.models import RunnerDefinition
from hivepilot.runners.base import BaseRunner, RunnerPayload
from hivepilot.services.github_service import (
    create_issue,
    create_release,
    ensure_repository,
)


@dataclass
class InternalRunner(BaseRunner):
    definition: RunnerDefinition
    settings: Settings

    def run(self, payload: RunnerPayload) -> None:
        action = payload.step.metadata.get("action") or self.definition.options.get("action")
        if action == "gh_repo_init":
            ensure_repository(
                project=payload.project,
                settings=self.settings,
                push=payload.step.metadata.get("push", True),
            )
        elif action == "gh_issue":
            title = payload.step.metadata.get("title") or payload.metadata.get("extra_prompt")
            create_issue(
                project=payload.project,
                settings=self.settings,
                title=title or "Automated issue",
                body=payload.step.metadata.get("body"),
                labels=payload.step.metadata.get("labels", []),
            )
        elif action == "gh_release":
            tag = payload.step.metadata.get("tag") or payload.metadata.get("extra_prompt")
            create_release(
                project=payload.project,
                settings=self.settings,
                tag=tag or "v0.0.0",
                title=payload.step.metadata.get("title"),
            )
        else:
            raise ValueError(f"Unknown internal runner action: {action}")
