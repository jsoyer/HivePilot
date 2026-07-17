"""Sample skill plugin — trivial example contribution for the `skill` plugin
type (skill-plugin-type PRD, Sprint 5).

Demonstrates the minimal `SkillSpec` shape any plugin can contribute via
`register()["skills"]` (see `hivepilot/plugins.py` for the full contract:
`name`/`description`/`provider`/`files` required, `system_prompt`/
`applies_to`/`min_role` optional). Deliberately built as a plain DICT
LITERAL, never a local `@dataclass` -- `SkillSpec` is a `TypedDict`
(type-checking only construct, a plain dict at runtime). Local-file plugins
are loaded via `importlib.util.spec_from_file_location()` / `exec_module()`
(`hivepilot.plugins._scan_local_plugins`), which never registers the module
in `sys.modules`; combined with `from __future__ import annotations`, a
local `@dataclass` on that load path trips a real CPython 3.14
`dataclasses` bug (`_is_type` does `sys.modules[cls.__module__].__dict__`,
which is `None` for an unregistered module) -- see `plugins/rtk.py` for the
full write-up. A dict literal sidesteps it entirely.

Enable/disable is handled ENTIRELY by the central plugin gate
(`settings.plugins_enabled` / `settings.plugins_disabled`, keyed off this
file's stem `sample_skill`) -- unlike `plugins/rtk.py` / `plugins/sample.py`,
this plugin declares no per-plugin settings flag of its own; `register()`
always contributes its one skill when this file is loaded at all.
"""

from __future__ import annotations

from typing import Any

_SKILL_MD = """# Sample Skill

Trivial example skill contributed by `plugins/sample_skill.py`
(skill-plugin-type PRD). Demonstrates the minimal `SkillSpec` shape a
plugin declares via `register()["skills"]` -- it carries no runtime
behavior of its own. A runner that supports skills may write this file out
under `.claude/skills/sample-skill/SKILL.md` for the agent to read, and/or
append a declared `system_prompt` (not set here) to its prompt. Runners
without skill support silently ignore this contribution.
"""


def register() -> dict[str, Any]:
    return {
        "skills": [
            {
                "name": "sample-skill",
                "description": "Trivial example skill demonstrating the SkillSpec contract.",
                "provider": "sample_skill",
                "files": {"SKILL.md": _SKILL_MD},
            }
        ]
    }
