"""Shadcn skill plugin — a Mirador web accelerator for the `skill` plugin
type (see `plugins/sample_skill.py` for the canonical example this mirrors).

Contributes one skill (`register()["skills"]`) whose `SKILL.md` gives a
practical, concise cheat sheet for building the Mirador web UI
(`web/` — React + Vite + Tailwind v4 + shadcn/ui) faster: the stack, how to
add a shadcn component, where things live, the panel/graph render
conventions Mirador already uses, and the theme/dark-mode discipline. The
content is grounded in the ACTUAL `web/` source tree (not invented
components) so an agent using this skill reuses real primitives instead of
guessing.

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

Enable/disable: gated on `settings.shadcn_enabled` (default False -- opt-IN,
dormant), same pattern as `plugins/sample_skill.py`. `register()`
early-returns `{}` when the flag is False; it also still respects the
central plugin gate (`settings.plugins_enabled` / `settings.plugins_disabled`,
keyed off this file's stem `shadcn`) same as every other local-file plugin.
"""

from __future__ import annotations

from typing import Any

_SKILL_MD = """# Shadcn — Mirador Web Accelerator

Practical, concise guidance for building the Mirador web dashboard
(`web/`) faster with shadcn/ui. Mirador is the insight dashboard shipped
with HivePilot -- a dark, tabbed React app.

## Stack

- **React 19** + **Vite** (`web/vite.config.ts`), TypeScript, strict mode.
- **Tailwind CSS v4** via `@tailwindcss/vite` -- no `tailwind.config.js`;
  theme tokens live in `web/src/index.css` (`@theme inline` + CSS custom
  properties under `:root` / `.dark`).
- **shadcn/ui** (`web/components.json`: style `base-nova`, base color
  `neutral`, CSS variables on, icon library `lucide`). Existing primitives
  live in `web/src/components/ui/` (`badge.tsx`, `button.tsx`, `card.tsx`,
  `input.tsx`, `table.tsx`, `tabs.tsx`).
- `lucide-react` for icons, `class-variance-authority` + `clsx` +
  `tailwind-merge` (via the `cn()` helper in `web/src/lib/utils.ts`) for
  variant/conditional class composition.
- `@xyflow/react` + `dagre` power the graph view (`GraphCanvas.tsx`,
  `GraphView.tsx`).
- Tests: **Vitest** (`web/vitest.config.ts`, `jsdom` environment) --
  co-located `*.test.tsx` next to every component. Lint: `oxlint`.

## Adding a shadcn component

Run the shadcn CLI from `web/` (it reads `components.json` and writes
straight into `web/src/components/ui/`):

```bash
cd web && npx shadcn@latest add <component>
```

This drops a new file in `web/src/components/ui/<component>.tsx` using the
project's existing style/base-color/CSS-variable settings -- do not
hand-roll a primitive that the CLI can generate. After adding, import it via
the `@/components/ui/<component>` alias (see `aliases` in
`components.json`: `@/components`, `@/lib`, `@/hooks`).

## Project conventions (mirror these, don't invent new patterns)

- **Where things live:** app shell = `web/src/components/Mirador.tsx`
  (dark, tabbed layout); auth gate = `TokenGate.tsx`; tab bodies =
  `web/src/components/views/*View.tsx` (one file per tab, e.g.
  `RunsView.tsx`, `HealthView.tsx`, `AnalyticsView.tsx`, `CostView.tsx`,
  `GraphView.tsx`, `Mem0View.tsx`); shared UI primitives =
  `web/src/components/ui/`; data fetching + API clients =
  `web/src/lib/api.ts` and `web/src/lib/mirador-api.ts`; the
  `useAsyncData` hook (`web/src/lib/use-async-data.ts`) standardizes
  loading/error/success state for every view.
- **Panel render pattern:** a generic tab body composes
  `Card` / `CardHeader` / `CardTitle` / `CardDescription` / `CardContent`
  from `@/components/ui/card`, drives data via `useAsyncData(() =>
  fetchX(...), [deps])`, and renders three states inline: a
  `role="status"` pulsing "Loading…" block, a `role="alert"` destructive
  error block (or a graceful `ApiForbiddenError` message when a panel's
  `min_role` exceeds the caller's token), and the success content. See
  `web/src/components/views/PanelView.tsx` +
  `web/src/components/views/PanelRenderer.tsx` for the reference
  implementation of this pattern -- follow it for any new
  plugin-contributed panel or view rather than building a bespoke
  loading/error scaffold.
- **Graph render pattern:** graph-shaped data (nodes/edges) renders via
  `GraphCanvas.tsx` (a thin `@xyflow/react` wrapper with `dagre` auto-layout)
  driven by `GraphView.tsx`, which fetches from a `graph_sources`
  plugin-contributed endpoint. Reuse `GraphCanvas`, don't reimplement
  node/edge rendering.
- **Errors:** `web/src/lib/format-error.ts`'s `describeApiError()` turns a
  thrown error into a user-facing string; use it instead of
  `String(error)`/`error.message` directly in a view.

## Tailwind conventions

- Utility classes only, composed via `cn(...)` when a component has
  variant/conditional classes (see any `web/src/components/ui/*.tsx` for the
  pattern: `cva()` for variants, `cn(variantClasses, className)` to allow
  caller overrides).
- Use the semantic color tokens (`bg-background`, `text-foreground`,
  `bg-card`, `text-muted-foreground`, `border-border`,
  `bg-destructive/10 text-destructive`, etc.) from `index.css` -- never
  hardcode a raw color value; every token already has a light (`:root`) and
  dark (`.dark`) definition.
- Radii use the `--radius-*` scale (`rounded-lg`, `rounded-xl`, ...), also
  theme-driven -- don't hardcode `rounded-[Npx]`.

## Theme / dark-mode discipline

Mirador is a dark-first dashboard. Dark mode is driven by the `.dark` class
on a root element (`@custom-variant dark (&:is(.dark *))` in `index.css`) --
never write a component that assumes only the light palette, and never
hardcode a color that bypasses the `--background`/`--foreground`/etc. CSS
variables. Every new component should look correct under both `:root` and
`.dark` without any component-level conditional logic -- that's what the
semantic tokens are for.

## Where this skill helps

Use it when asked to add a new Mirador tab/view, a new panel renderer for a
plugin-contributed panel, or any UI element in `web/` -- it should save a
round trip of re-discovering the stack, the component library, and the
existing conventions before writing new UI code.
"""

_SYSTEM_PROMPT = (
    "When working in web/ (the Mirador dashboard), reuse existing shadcn/ui "
    "primitives from web/src/components/ui/ and the existing view/panel "
    "render patterns (see PanelView.tsx / PanelRenderer.tsx) instead of "
    "hand-rolling new UI scaffolding. Match the project's Tailwind v4 "
    "semantic-token conventions (index.css) and verify every new component "
    "renders correctly in both the default (light) and .dark palettes."
)


def register() -> dict[str, Any]:
    from hivepilot.config import settings

    if not settings.shadcn_enabled:
        return {}
    return {
        "skills": [
            {
                "name": "shadcn",
                "description": (
                    "Mirador web accelerator: shadcn/ui + Tailwind conventions for "
                    "building the web/ dashboard faster."
                ),
                "provider": "shadcn",
                "files": {"SKILL.md": _SKILL_MD},
                "system_prompt": _SYSTEM_PROMPT,
            }
        ]
    }
