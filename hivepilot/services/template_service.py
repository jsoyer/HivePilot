from __future__ import annotations

import warnings
from pathlib import Path

# Well-known community registry — a GitHub repo whose root contains
# subdirectories, each being a template (with a template.yaml manifest).
_REGISTRY_URL = "https://raw.githubusercontent.com/hivepilot-community/templates/main"
_REGISTRY_INDEX = f"{_REGISTRY_URL}/index.json"

_TEMPLATES: dict[str, dict] = {
    "minimal": {
        "description": "Bare-bones setup: one project, one docs task",
        "files": {
            "projects.yaml": """\
projects:
  {{project_name}}:
    path: {{project_path}}
    description: "{{project_name}}"
    env: {}
""",
            "tasks.yaml": """\
runners:
  claude-default:
    kind: claude
    command: claude

tasks:
  docs:
    description: "Rewrite and normalize documentation"
    steps:
      - name: rewrite docs
        runner: claude
        runner_ref: claude-default
        prompt_file: prompts/docs_rewrite.md
        timeout_seconds: 3600
    git:
      commit: true
      push: true
      create_pr: true
      commit_message: "docs: rewrite documentation"
      branch_prefix: hivepilot
""",
            "policies.yaml": """\
policies:
  default:
    require_approval: false
    allowed_runners: [claude, shell]
""",
            "schedules.yaml": """\
schedules: {}
""",
            "prompts/docs_rewrite.md": """\
# Documentation Rewrite

Rewrite and normalize all documentation in this project.

- Fix grammar and spelling
- Ensure consistent formatting
- Add missing sections (usage, examples, configuration)
- Remove outdated or duplicate content
- Use clear, concise language
""",
        },
    },
    "blog": {
        "description": "Blog publishing: bilingual blog-post tasks with approval policy",
        "files": {
            "projects.yaml": """\
projects:
  {{project_name}}:
    path: {{project_path}}
    description: "{{project_name}} blog"
    env: {}
""",
            "tasks.yaml": """\
runners:
  claude-default:
    kind: claude
    command: claude

tasks:
  blog-post-en:
    description: "Generate an English blog post"
    steps:
      - name: write post
        runner: claude
        runner_ref: claude-default
        prompt_file: prompts/blog/en/tech.md
        timeout_seconds: 3600
    git:
      commit: true
      push: true
      create_pr: true
      commit_message: "content: add english blog post"
      branch_prefix: blog

  blog-post-fr:
    description: "Generate a French blog post"
    steps:
      - name: write post
        runner: claude
        runner_ref: claude-default
        prompt_file: prompts/blog/fr/tech.md
        timeout_seconds: 3600
    git:
      commit: true
      push: true
      create_pr: true
      commit_message: "content: add french blog post"
      branch_prefix: blog
""",
            "policies.yaml": """\
policies:
  default:
    require_approval: true
    allowed_runners: [claude]
""",
            "schedules.yaml": """\
schedules:
  weekly-blog-en:
    task: blog-post-en
    projects: [{{project_name}}]
    interval_minutes: 10080
    enabled: false

  weekly-blog-fr:
    task: blog-post-fr
    projects: [{{project_name}}]
    interval_minutes: 10080
    enabled: false
""",
            "prompts/blog/en/tech.md": """\
# Tech Blog Post

Write a well-structured technical blog post on a topic relevant to this project.

Requirements:
- Audience: software engineers and technical practitioners
- Length: 600-900 words
- Include a clear introduction, body with examples or code snippets where relevant, and a concise conclusion
- Tone: professional but accessible
- Author: {{author}}

Choose a topic that adds value to the community and showcases recent developments or best practices.
""",
            "prompts/blog/fr/tech.md": """\
# Article de blog technique

Redige un article de blog technique bien structure sur un sujet pertinent pour ce projet.

Exigences:
- Public cible: ingenieurs logiciels et praticiens techniques
- Longueur: 600 a 900 mots
- Inclure une introduction claire, un corps avec des exemples ou extraits de code si pertinent, et une conclusion concise
- Ton: professionnel mais accessible
- Auteur: {{author}}

Choisis un sujet qui apporte de la valeur a la communaute et met en evidence les developpements recents ou les meilleures pratiques.
""",
        },
    },
    "iac": {
        "description": "Infrastructure: OpenTofu plan/apply tasks with drift-check schedule",
        "files": {
            "projects.yaml": """\
projects:
  {{project_name}}:
    path: {{project_path}}
    description: "{{project_name}} infrastructure"
    env: {}
""",
            "tasks.yaml": """\
runners:
  claude-default:
    kind: claude
    command: claude
  shell-default:
    kind: shell

tasks:
  tofu-plan:
    description: "Run OpenTofu plan and review changes"
    steps:
      - name: plan
        runner: shell
        runner_ref: shell-default
        command: tofu plan -out=tfplan
        timeout_seconds: 300
    git:
      commit: false
      push: false
      create_pr: false

  tofu-apply:
    description: "Apply the OpenTofu plan"
    steps:
      - name: apply
        runner: shell
        runner_ref: shell-default
        command: tofu apply tfplan
        timeout_seconds: 600
    git:
      commit: false
      push: false
      create_pr: false

  drift-check:
    description: "Check for infrastructure drift"
    steps:
      - name: drift
        runner: shell
        runner_ref: shell-default
        command: tofu plan --detailed-exitcode
        timeout_seconds: 300
    git:
      commit: false
      push: false
      create_pr: false
""",
            "policies.yaml": """\
policies:
  default:
    require_approval: false
    allowed_runners: [shell]

  tofu-apply:
    require_approval: true
    allowed_runners: [shell]
""",
            "schedules.yaml": """\
schedules:
  daily-drift-check:
    task: drift-check
    projects: [{{project_name}}]
    interval_minutes: 1440
    enabled: false
""",
        },
    },
    "security": {
        "description": "Security review: pentest and architecture review tasks",
        "files": {
            "projects.yaml": """\
projects:
  {{project_name}}:
    path: {{project_path}}
    description: "{{project_name}} security review"
    env: {}
""",
            "tasks.yaml": """\
runners:
  claude-default:
    kind: claude
    command: claude

tasks:
  pentest:
    description: "Automated security and vulnerability review"
    steps:
      - name: security review
        runner: claude
        runner_ref: claude-default
        prompt_file: prompts/security_review.md
        timeout_seconds: 3600
    git:
      commit: true
      push: true
      create_pr: true
      commit_message: "security: automated vulnerability review"
      branch_prefix: security

  arch-review:
    description: "Architecture security review"
    steps:
      - name: architecture review
        runner: claude
        runner_ref: claude-default
        prompt_file: prompts/architecture_review.md
        timeout_seconds: 3600
    git:
      commit: true
      push: true
      create_pr: true
      commit_message: "security: architecture review"
      branch_prefix: security
""",
            "policies.yaml": """\
policies:
  default:
    require_approval: false
    allowed_runners: [claude]
""",
            "schedules.yaml": """\
schedules: {}
""",
            "prompts/security_review.md": """\
# Security Review

Perform a thorough security and vulnerability review of this codebase.

Checklist:
- Identify injection vulnerabilities (SQL, command, path traversal)
- Check authentication and authorization logic
- Review secret and credential handling
- Audit dependency versions for known CVEs
- Inspect input validation and sanitization
- Check for insecure defaults or configurations
- Review error handling for information leakage
- Identify insecure cryptographic usage

For each finding, provide:
- Severity: Critical / High / Medium / Low / Informational
- Location: file and line number where applicable
- Description: what the issue is and why it matters
- Recommendation: concrete remediation steps
""",
            "prompts/architecture_review.md": """\
# Architecture Security Review

Review the system architecture from a security perspective.

Areas to assess:
- Trust boundaries and data flows between components
- Authentication and session management design
- Authorization model and privilege escalation paths
- Network exposure and attack surface
- Secrets management and key rotation
- Logging, monitoring, and incident response capability
- Dependency and supply chain risk
- Compliance considerations (GDPR, SOC2, etc.)

Produce a structured report with an executive summary, detailed findings, and prioritized recommendations.
""",
        },
    },
}


def list_templates() -> list[str]:
    return list(_TEMPLATES.keys())


# ---------------------------------------------------------------------------
# Community marketplace
# ---------------------------------------------------------------------------

def list_remote_templates(source: str | None = None) -> list[dict]:
    """
    Fetch the template index from a remote source.

    source : GitHub shorthand (user/repo), full HTTPS URL, or None to use
             the official registry.

    Returns a list of dicts with keys: name, description, url.
    """
    import json
    import urllib.request

    index_url = _resolve_index_url(source)
    try:
        with urllib.request.urlopen(index_url, timeout=10) as resp:  # noqa: S310
            data = json.loads(resp.read())
    except Exception as exc:
        raise RuntimeError(f"Could not fetch template index from {index_url}: {exc}") from exc

    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "templates" in data:
        return data["templates"]
    raise RuntimeError(f"Unexpected index format at {index_url}")


def pull_template(name: str, dest: Path, source: str | None = None) -> list[str]:
    """
    Download a community template and write its files to dest.

    name   : template name as listed in the remote index.
    dest   : destination directory (typically ~/.config/hivepilot or cwd).
    source : same as list_remote_templates.

    Returns list of written file paths.
    """
    import json
    import urllib.request

    templates = list_remote_templates(source)
    entry = next((t for t in templates if t.get("name") == name), None)
    if entry is None:
        available = ", ".join(t.get("name", "?") for t in templates)
        raise ValueError(f"Template {name!r} not found. Available: {available}")

    base_url = entry.get("url", "").rstrip("/")
    files_manifest_url = f"{base_url}/files.json"

    try:
        with urllib.request.urlopen(files_manifest_url, timeout=10) as resp:  # noqa: S310
            files: dict[str, str] = json.loads(resp.read())
    except Exception as exc:
        raise RuntimeError(f"Could not fetch files manifest for {name!r}: {exc}") from exc

    written: list[str] = []
    for relative_path, file_url in files.items():
        target = dest / relative_path
        if target.exists():
            warnings.warn(f"Skipping existing file: {target}", stacklevel=2)
            continue
        try:
            with urllib.request.urlopen(file_url, timeout=10) as resp:  # noqa: S310
                content = resp.read().decode("utf-8")
        except Exception as exc:
            raise RuntimeError(f"Could not fetch {file_url}: {exc}") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(str(target))

    return written


def _resolve_index_url(source: str | None) -> str:
    if source is None:
        return _REGISTRY_INDEX
    if source.startswith("https://") or source.startswith("http://"):
        return source.rstrip("/") + "/index.json"
    # GitHub shorthand: user/repo
    if "/" in source and not source.startswith("/"):
        return f"https://raw.githubusercontent.com/{source}/main/index.json"
    raise ValueError(f"Unrecognised source format: {source!r}. Use 'user/repo' or a full HTTPS URL.")


def get_template(name: str) -> dict:
    if name not in _TEMPLATES:
        raise ValueError(f"Unknown template: {name!r}. Available: {', '.join(_TEMPLATES)}")
    return _TEMPLATES[name]


def render_template(name: str, variables: dict[str, str]) -> dict[str, str]:
    template = get_template(name)
    rendered: dict[str, str] = {}
    for filename, content in template["files"].items():
        for key, value in variables.items():
            content = content.replace(f"{{{{{key}}}}}", value)
        rendered[filename] = content
    return rendered


def write_template(name: str, dest: Path, variables: dict[str, str]) -> list[str]:
    rendered = render_template(name, variables)
    written: list[str] = []
    for relative_path, content in rendered.items():
        target = dest / relative_path
        if target.exists():
            warnings.warn(f"Skipping existing file: {target}", stacklevel=2)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(str(target))
    return written
