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
    pipelines_file: Path = Path("pipelines.yaml")
    policies_file: Path = Path("policies.yaml")
    schedules_file: Path = Path("schedules.yaml")
    prompts_dir: Path = Path("prompts")
    runs_dir: Path = Path("runs")
    logs_dir: Path = Path("runs/logs")
    claude_profiles_file: Path = Path("model_profiles.yaml")
    state_db: Path = Path("state.db")
    tokens_file: Path = Path("api_tokens.yaml")
    default_runner: str = "claude"
    default_model: str | None = None
    claude_command: str = "claude"
    gh_command: str = "gh"
    git_command: str = "git"
    concurrency_limit: int = 4
    interactive_default_all: bool = False
    enable_textual_ui: bool = False
    output_format: str = "json"
    plugins_entry: str | None = None
    discovery_roots: list[str] = Field(default_factory=lambda: ["~/dev"])
    api_host: str = "127.0.0.1"
    api_port: int = 8045

    def resolve_path(self, path: Path) -> Path:
        return (self.base_dir / path).expanduser().resolve()


settings = Settings()
