"""Improve skill plugin — a read-only auditor feeding the review/lessons
loop, for the `skill` plugin type (see `plugins/sample_skill.py` for the
canonical example this mirrors).

Contributes one skill (`register()["skills"]`) whose `SKILL.md` is a
concise audit methodology: an agent using this skill surfaces improvement
findings (correctness, security, simplification, test-coverage,
performance) as a STRUCTURED FINDINGS LIST -- without making any changes.
Pure read/analysis, so its output feeds the project's existing
code-reviewer / lessons machinery rather than editing files itself.

Deliberately built as a plain DICT LITERAL, never a local `@dataclass` --
`SkillSpec` is a `TypedDict` (type-checking only construct, a plain dict at
runtime). Local-file plugins are loaded via
`importlib.util.spec_from_file_location()` / `exec_module()`
(`hivepilot.plugins._scan_local_plugins`), which never registers the module
in `sys.modules`; combined with `from __future__ import annotations`, a
local `@dataclass` on that load path trips a real CPython 3.14
`dataclasses` bug (`_is_type` does `sys.modules[cls.__module__].__dict__`,
which is `None` for an unregistered module) -- see `plugins/rtk.py` for the
full write-up. A dict literal sidesteps it entirely.

Enable/disable: gated on `settings.improve_enabled` (default False -- opt-IN,
dormant), same pattern as `plugins/sample_skill.py`. `register()`
early-returns `{}` when the flag is False; it also still respects the
central plugin gate (`settings.plugins_enabled` / `settings.plugins_disabled`,
keyed off this file's stem `improve`) same as every other local-file plugin.
"""

from __future__ import annotations

from typing import Any

_SKILL_MD = """# Improve — Read-Only Audit Skill

A read-only auditor. An agent using this skill surfaces improvement
findings -- it NEVER writes, edits, or commits anything (never commit, never
write, never edit). Its sole output is
a structured findings list that feeds the project's existing code-review
and lessons-learned loop.

## Hard rule: READ-ONLY, no exceptions

- Never write, edit, or delete any file.
- Never run a command that mutates state (no `git commit`, no `git add`,
  no package installs, no formatters/linters run with `--fix`/`--write`,
  no database writes, no deploys).
- Only use read/analysis tools: reading files, searching/grepping,
  running read-only static analysis (lint/type-check in check-only mode,
  test suites in read-only "run and report" mode), and reasoning over the
  results.
- If a finding requires a fix, describe the fix in the findings output --
  do not apply it. Fixing is a separate, explicit follow-up step owned by
  a human or a different (write-capable) agent/workflow.

## What to audit

Scan for improvement opportunities across five categories:

1. **Correctness** -- logic bugs, incorrect edge-case handling, off-by-one
   errors, unhandled error paths, race conditions.
2. **Security** -- hardcoded secrets, missing input validation, injection
   risks, missing authn/authz checks, unsafe deserialization, data leaks
   in logs/error messages.
3. **Simplification** -- unnecessary complexity, dead code, duplicated
   logic that could be extracted, overly deep nesting, misapplied
   abstractions.
4. **Test coverage** -- untested branches, missing edge-case tests,
   assertions that check output shape but not real behavior (Goodhart
   risk), missing regression tests for previously fixed bugs.
5. **Performance** -- obvious algorithmic inefficiencies (e.g. O(n^2)
   where O(n) is available), unnecessary re-computation, unbounded
   growth, N+1 query patterns -- only where there's a real, identifiable
   cost, not speculative micro-optimization.

## Output format: structured findings list

Every finding MUST include:

- **severity** -- one of `critical`, `high`, `medium`, `low`, `info`.
- **file:line** -- the exact location (e.g. `hivepilot/plugins.py:412`).
  If a finding spans a range, use `file:line-line`.
- **category** -- one of the five audit categories above.
- **why** -- a concise explanation of the problem and its real-world
  impact (not just "this looks odd").
- **suggested fix** -- a concrete, actionable suggestion (not "consider
  refactoring this") that a human or a write-capable follow-up agent can
  act on directly.

Example finding:

```
severity: high
file:line: hivepilot/plugins.py:412
category: security
why: user-supplied `name` is used to build a filesystem path without a
  traversal check, allowing `../../` to escape the intended directory.
suggested fix: validate `name` against a safe-slug pattern (e.g.
  `^[a-zA-Z0-9_-]+$`) before joining it into the path, and reject the
  request otherwise.
```

Group findings by severity (critical/high first) so the highest-impact
items surface first. Do not pad the list with trivial style nits unless
explicitly asked for a style-focused pass -- keep it focused on real
improvement opportunities.

## How this feeds the review/lessons loop

This skill's findings are meant to be consumed downstream (by a human, or
by the project's code-reviewer agent / session-learnings capture) -- never
applied automatically by the same invocation that produced them. Treat the
findings list as the deliverable, not a to-do list to immediately execute.
"""

_SYSTEM_PROMPT = (
    "You are operating in read-only audit mode. Never write, edit, delete, "
    "or commit any file, and never run a state-mutating command. Only read, "
    "search, and analyze. Produce a structured findings list -- each entry "
    "with severity, file:line, category, why, and a suggested fix -- and "
    "nothing else. Do not apply any fix yourself."
)


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.improve_enabled:
        return {}
    return {
        "skills": [
            {
                "name": "improve",
                "description": (
                    "Read-only auditor: surfaces structured improvement findings "
                    "(correctness, security, simplification, coverage, performance) "
                    "without making any changes."
                ),
                "provider": "improve",
                "files": {"SKILL.md": _SKILL_MD},
                "system_prompt": _SYSTEM_PROMPT,
            }
        ]
    }
