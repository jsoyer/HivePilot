# 🐝 HivePilot

**Pilot your repositories with an AI swarm powered by Claude Code.**

HivePilot is a lightweight Python CLI that routes repeatable workflows (docs revamps, pentests, refactors, release chores) across any number of repositories while respecting your existing Claude Code setup, project-specific `CLAUDE.md`, and GitHub CLI automation.

The orchestrator stays thin on purpose:

- it **does not** call hosted Anthropic APIs
- it **does** shell out to your local `claude` CLI per repository
- it **can** mix Claude steps with shell validations (`pytest`, `npm test`, etc.)
- it **can** commit/push/create PRs through `git` + `gh` when you opt in

---

## 🧱 Why this stack

- **Typer** keeps the CLI type-safe and ergonomic.  
- **Rich** renders tables/panels for fast status at a glance.  
- **Pydantic + Pydantic Settings** validate YAML configs and `.env` overrides early.  
- **GitHub CLI (`gh`)** enables optional repository automation without wiring OAuth here.

---

## 📂 Layout

```
hivepilot-starter/
├── hivepilot/               # CLI, runner, git helpers, prompt builder
├── prompts/                 # reusable prompt templates
├── .env.example
├── Dockerfile
├── docker-compose.yml
├── projects.yaml
├── tasks.yaml
└── PR_BODY.md
```

---

## ⚙️ Configuration

### Projects (`projects.yaml`)

Describe where each repository lives plus metadata HivePilot passes to prompts and git automation:

```yaml
projects:
  example-api:
    path: ~/dev/example-api
    description: Example API project.
    claude_md: CLAUDE.md
    default_branch: main
    owner_repo: your-user/example-api
    env:
      PYTHONUNBUFFERED: "1"
```

### Tasks (`tasks.yaml`)

Define reusable workflows composed of Claude and/or shell steps. Git/PR automation is optional per task.

```yaml
tasks:
  docs:
    description: Rewrite and normalize repository documentation.
    steps:
      - name: rewrite docs
        runner: claude
        prompt_file: docs_rewrite.md
        agent: docs-writer
        model: sonnet
    git:
      commit: true
      push: true
      create_pr: true
      pr_body_file: PR_BODY.md
```

### Prompt templates (`prompts/`)

Prompt files are simple Markdown instructions. At runtime HivePilot prepends structured context:

- project name / description / repository path
- preferred `CLAUDE.md`
- agent and model hints per step
- user-provided `--extra-prompt` text
- step-specific `append_prompt`

This keeps prompts reusable and short.

---

## 🚀 Installation

### Local

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .
cp .env.example .env
```

Edit `.env`, `projects.yaml`, and `tasks.yaml` to match your machine.

### Docker

```bash
docker compose build
docker compose run --rm hivepilot hivepilot doctor
```

The compose file mounts:

- your repo checkout into `/workspace`
- `~/.claude` so your Claude Code agents/instructions are available
- `~/.config/gh` for GitHub CLI auth (if already logged in)
- `~/dev` for the projects referenced in `projects.yaml`

---

## 🕹 CLI usage

```bash
hivepilot doctor              # show resolved config paths/commands
hivepilot list-projects       # inspect configured repositories
hivepilot list-tasks          # list workflows and their steps
hivepilot run example-api docs
hivepilot run example-api docs --extra-prompt "Focus on auth flows"
hivepilot run example-api refactor --auto-git
hivepilot run example-api pentest --dry-run
```

---

## 🤖 GitHub automation

When `--auto-git` is set and the task defines a `git:` block, HivePilot can:

1. create or reset a working branch (defaults to `hivepilot/<project>`).
2. `git add -A` and optionally `git commit` with a custom message.
3. `git push -u origin <branch> --force-with-lease`.
4. `gh pr create` with the configured title/body template.

Important notes:

- HivePilot never authenticates with GitHub for you. Run `gh auth login` beforehand.
- A PR body file must exist if `create_pr: true`.
- Push/PR actions respect `--dry-run` for previewing commands.

---

## 🤝 Suggested workflow

1. Keep one HivePilot orchestrator repo.  
2. Store project-specific behavior in each repo’s `CLAUDE.md`.  
3. Start with `--dry-run` and `--auto-git` disabled until comfortable.  
4. Expand prompts/tasks for docs refreshes, security reviews, refactors, release prep, etc.

---

## 🛠 Next steps / ideas

- Batch mode to run a task across many projects.
- Interactive project selection (`fzf`) or prompt history.
- Richer PR body templates and release-note generators.
- Task chaining (e.g., pentest → fixes → tests → PR).

---

## ⚠️ Limitations

- Requires the local `claude` CLI, Git, and optionally GitHub CLI to be installed.
- Does not call Anthropic APIs directly nor manage Claude Code auth.
- Does not auto-discover your Claude agents/skills (keep them in Claude Code itself).

If HivePilot speeds up your AI workflows, consider sharing feedback or automations!
