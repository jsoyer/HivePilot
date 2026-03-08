
# 🐝 HivePilot

**Pilot your repositories with an AI swarm.**

HivePilot is a lightweight **AI orchestration tool for developers** that lets you run **AI agents and workflows across multiple repositories** from a single command.

Instead of manually opening multiple terminals or machines, HivePilot lets you:

- orchestrate AI tasks across projects
- route tasks to the right AI (Claude, GPT, etc.)
- automate documentation, reviews, pentests and refactors
- manage multi‑repo workflows easily

Think of it as a **command center for your AI development agents.**

---

## ✨ Features

🧠 Multi‑AI orchestration  
Run different models or agents depending on the task.

📦 Multi‑repository workflows  
Execute tasks across multiple repos.

🤖 AI‑friendly architecture  
Built to integrate with:

- Claude Code
- LangChain
- GitHub CLI (`gh`)
- custom scripts and scanners

⚡ Lightweight  
No heavy framework required.

🐳 Docker ready  
Run locally or inside containers.

---

## 🚀 Example Use Cases

Rewrite documentation for all projects

```
hivepilot run docs --all
```

Run a security audit

```
hivepilot run pentest api
```

Generate architecture review

```
hivepilot run architecture backend
```

Refactor codebase

```
hivepilot run refactor webapp
```

---

## 🏗 Architecture

HivePilot acts as a **task router**.

```
task
 ↓
HivePilot
 ↓
select AI / runner
 ↓
execute on repository
 ↓
commit / PR
```

Example pipeline:

```
pentest → fix → review → PR
```

---

## 📂 Project Structure

```
hivepilot/
 ├ orchestrator CLI
 ├ runners
 ├ utilities
prompts/
projects.yaml
tasks.yaml
```

---

## ⚙️ Installation

Clone the repository

```
git clone https://github.com/yourusername/hivepilot.git
cd hivepilot
```

Create virtual environment

```
python -m venv .venv
source .venv/bin/activate
```

Install dependencies

```
pip install -r requirements.txt
pip install -e .
```

Run diagnostics

```
hivepilot doctor
```

---

## 🔧 Configuration

### projects.yaml

```
projects:
  api:
    path: ~/dev/api
  webapp:
    path: ~/dev/webapp
```

### tasks.yaml

```
tasks:
  docs:
    runner: claude
    prompt: prompts/docs.md
  pentest:
    runner: claude
    prompt: prompts/pentest.md
```

---

## 🤖 AI Integrations

HivePilot supports:

- Claude Code
- OpenAI models via LangChain
- local LLMs
- shell tools

---

## 🗺 Roadmap

- multi‑repo batch execution
- agent pipelines
- scheduling
- plugin system
- interactive dashboard

---

## 🤝 Contributing

Pull requests welcome!

If you have ideas for:

- new AI workflows
- integrations
- agents

feel free to open an issue.

---

## ⭐ Star the project

If HivePilot helps your workflow, consider starring the repo.
