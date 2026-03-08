from __future__ import annotations

from hivepilot.utils.logging import get_logger

try:
    from crewai import Agent, Crew, Task
except ImportError:  # pragma: no cover - optional dependency
    Agent = Crew = Task = None

logger = get_logger(__name__)


def build_crew(project, payload):
    if Crew is None or Agent is None or Task is None:
        raise RuntimeError("Install hivepilot[crewai] to run CrewAI workflows.")

    agent = Agent(
        role="Documentation Specialist",
        goal=f"Update README and docs for {project.path}",
        backstory="Expert technical writer familiar with large repos.",
        allow_delegation=False,
    )
    task = Task(
        description="Review repository artifacts and refresh documentation.",
        expected_output="Summary of edits and TODOs.",
        agent=agent,
    )
    logger.info("crewai.prepare", project=project.path.name)
    return Crew(agents=[agent], tasks=[task])
