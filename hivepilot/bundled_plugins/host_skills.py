"""Host skills plugin — skill-discovery + delegate-task (HP-112).

Contributes two skills (`register()["skills"]`) whose `SKILL.md` files
teach an agent when to search the local catalog and when to hand work to
a HivePilot subagent, peer run, or named pipeline. Runtime wiring lives
in `hivepilot.host_skills`; this file only publishes the SkillSpec dicts.

Deliberately built as plain DICT LITERALS from `skill_specs()` — never a
local `@dataclass`. Local-file plugins are loaded via
`importlib.util.spec_from_file_location()` / `exec_module()`, which never
registers the module in `sys.modules`.

Always on when plugins load. Still respects the central plugin gate
(`settings.plugins_enabled` / `settings.plugins_disabled`, keyed off this
file's stem `host_skills`).
"""

from __future__ import annotations

from typing import Any

from hivepilot.host_skills import skill_specs


def register() -> dict[str, Any]:
    return {"skills": [dict(spec) for spec in skill_specs()]}
