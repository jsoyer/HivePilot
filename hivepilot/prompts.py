from __future__ import annotations

from pathlib import Path

from hivepilot.models import ProjectConfig, TaskStep


class PromptError(RuntimeError):
    pass



def build_prompt(
    *,
    prompt_path: Path,
    project_name: str,
    project: ProjectConfig,
    task_name: str,
    step: TaskStep,
    extra_prompt: str | None,
) -> str:
    if not prompt_path.exists() or not prompt_path.is_file():
        raise PromptError(f"Prompt file not found: {prompt_path}")

    base_prompt = prompt_path.read_text(encoding="utf-8").strip()

    sections = [
        f"Project: {project_name}",
        f"Task: {task_name}",
        f"Step: {step.name}",
        f"Repository path: {project.path}",
    ]

    if project.description:
        sections.append(f"Project description: {project.description}")
    if project.claude_md:
        sections.append(f"Repository instructions file: {project.claude_md}")
    if step.agent:
        sections.append(f"Preferred agent or skill: {step.agent}")
    if step.model:
        sections.append(f"Preferred Claude model: {step.model}")
    if extra_prompt:
        sections.append(f"Extra instructions from user: {extra_prompt}")
    if step.append_prompt:
        sections.append(f"Step-specific instructions: {step.append_prompt}")

    meta = "\n".join(sections)
    return f"{meta}\n\nInstructions:\n{base_prompt}\n"
