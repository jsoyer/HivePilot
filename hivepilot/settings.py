from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HIVEPILOT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    base_dir: Path = Field(default_factory=lambda: Path.cwd())
    projects_file: Path = Path("projects.yaml")
    tasks_file: Path = Path("tasks.yaml")
    prompts_dir: Path = Path("prompts")
    default_branch_prefix: str = "hivepilot"
    claude_command: str = "claude"
    gh_command: str = "gh"
    git_command: str = "git"
    default_model: str | None = None


settings = Settings()
