# Avatar attribution

`base.avatar.json` is the **Strobi** example avatar definition from the
**Bible Strong Avatar Lab**, reused here as the base rig for the per-role
Pollen avatars (each role overrides only the body colour — see
`web/src/lib/role-avatars.ts`).

- Source: https://github.com/smontlouis/bible-strong-avatar-lab
  (`examples/react-vite-consumer/src/strobi.avatar.json`)
- Author: Stéphane Montlouis-Calixte
- License: **AGPL-3.0-only**

The renderer `@bible-strong/avatar-react` is also AGPL-3.0-only. HivePilot is
GPL-3.0; the two are compatible (GPLv3 §13), with the AGPL adding a
network-use source-availability obligation for the Pollen web UI portion that
links the renderer. This is acceptable because HivePilot is already
open-source.
