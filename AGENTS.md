# Repository Guidelines

## Project Structure & Module Organization
Core Python sources live under `hivepilot/`: `cli.py` exposes the Typer entrypoint, `orchestrator.py` drives scheduling, `runners/` holds CLI/API/container integrations, and `services/` encapsulates Git, policy, token, artifact, and secret helpers. Repository-level YAML (`projects.yaml`, `tasks.yaml`, `pipelines.yaml`, `model_profiles.yaml`) defines automation surfaces. Prompts go in `prompts/`, agent hooks in `plugins/`, and CrewAI or LangGraph builders in `workflows/`. `secrets_service.py` resolves per-step `secrets:` blocks so runners receive env vars without storing tokens in-repo, and the GitHub helpers respect the CLI flags for remote protocol, visibility, and release notes—mirror any new switches in README when you extend them. Update README/ROADMAP when adjusting layouts, and keep run outputs confined to `runs/<timestamp>/` (ignored by git).

## Build, Test, and Development Commands
Create a virtual environment and install dependencies before hacking:
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && pip install -e .
pip install -e .[full]    # langgraph + crewai + textual extras when needed
```
Validate configs with `hivepilot lint`, sanity-check integrations via `hivepilot doctor`, and dry-run workloads with `hivepilot run example-api docs --dry-run`.

## Coding Style & Naming Conventions
Target Python 3.10+, 4-space indentation, and explicit type hints. Keep modules/functions `snake_case`, classes `PascalCase`, and CLI commands short verbs (`list-projects`, `run-pipeline`). Place new services beside peers in `hivepilot/services/`, register runners via `hivepilot/runners/` + `registry.py`, and stick to YAML-driven steps rather than bespoke logic. Follow the existing `prompt-topic_action.md` naming when adding prompt files, log through `structlog`, and reuse helpers from `hivepilot/utils/` to avoid ad-hoc IO.

## Testing Guidelines
Pytest is the canonical harness; create suites under `tests/` and name files/functions `test_<unit>.py`. Attach CLI smoke checks to complex features (`hivepilot run <project> <task> --dry-run`, `hivepilot schedule list`) and extend the config linter when you add YAML surfaces. Run `pytest`, `hivepilot lint`, and the shell/container validations declared in `tasks.yaml` before requesting review.

## Commit & Pull Request Guidelines
Commits follow Conventional Commit prefixes (`feat:`, `chore:`, etc.) per `git log`; keep messages imperative and scoped (`feat: expand runner ecosystem`). Pull requests must fill out `PR_BODY.md`, summarizing orchestration changes and checking only the tests you executed (`pytest`, `npm test`, bespoke shell checks). Link the owning issue or schedule entry, include the exact `hivepilot run …` command reviewers can replay, and add screenshots/log snippets when touching the dashboard or API. Double-check that secrets, tokens, and policy overrides stay redacted before requesting review.
