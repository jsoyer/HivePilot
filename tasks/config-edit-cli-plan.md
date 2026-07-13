# Plan — CLI d'édition de configuration

**Statut :** build-candidate existant (`docs/tasks/cli/feature/2026-07-12_1701-config-edit-commands/`).
Sprints 1 & 4 **mergés** (round-trip writer `config_writer.py` PR #113 + dedup model_profiles).
Reste **Sprint 2 + Sprint 3**. Ordre global : **ce plan AVANT le système de plugins**.

## Décisions figées
- `role wire` édite **tous les champs de `Role`** (pas un allowlist) → setter générique avec coercition typée + validation par champ.

## Briques réutilisées (déjà livrées — ne pas réécrire)
- `config_writer.apply_and_validate(file, mutate, *, dry_run, base_dir)` → deep-copy → mutation → validation prospective dans un tempdir (`validate_config`) → écrit seulement si 0 erreur.
- `load_roundtrip` / `dump_roundtrip` (ruamel.yaml, **préserve commentaires**).
- `resolve_reference(kind, value)`, `prompt_or_refuse(valid, label)` (picker TTY / refus headless).
- `validate_config(base_dir)` (cross-refs pipeline→task, task→role, group→hub/components→project).

## Sprint 2 — Lecture : `config get` / `config list`
- [ ] `hivepilot config get <file> [key]` — affiche la valeur résolue.
- [ ] `hivepilot config list` — inventaire des surfaces de conf.
- [ ] **Provenance XDG** : indiquer quel fichier de la chaîne de résolution fournit la valeur (XDG → config_repo → base_dir).
- [ ] **Redaction des secrets** (api_tokens.yaml, tokens telegram, `--show-secrets` explicite pour lever).
- [ ] Nouveau `hivepilot/services/config_provenance.py`.
- [ ] Tests `tests/test_cli_config_get.py` (CliRunner).
- Risque : **faible** (read-only).

## Sprint 3 — Mutations guidées (fix `role-mapping`)
- [ ] `hivepilot project add <name> --path <p> [--description ...]` / `project rm <name>`.
- [ ] `hivepilot task set-role <task> <role>` (valide que le rôle existe via `resolve_reference`).
- [ ] `hivepilot role wire <role> <field> <value>` — **tous les champs `Role`** avec coercition typée :
  - str : `runner`, `model`, `model_profile`, `prompt_file`, `title`, `display_name`, `host`, `permission_mode`, `command_task`
  - int : `order` · bool : `can_block` · list[str] : `models`, `inputs`, `outputs`
  - [ ] validation d'enum pour `permission_mode`, existence pour `prompt_file`/`runner`.
- [ ] Toutes les commandes passent par `apply_and_validate` → `--dry-run`, idempotence, refus headless propre, commentaires préservés.
- [ ] Tests `tests/test_cli_config_commands.py` (CliRunner : happy path, dry-run, valeur invalide rejetée, idempotence, refus non-TTY).
- Risque : **moyen** (surface de validation plus large avec "tous les champs").

## Pattern d'implémentation
- Sous-app Typer `config_edit` (ou étendre `config_app`) suivant `x_app = typer.Typer()` + `app.add_typer` (cli.py:36-37).
- Chaque commande = petit `mutate(CommentedMap) -> None` passé à `apply_and_validate`. Le CLI ne parse/valide jamais lui-même — délègue au writer + `validate_config`.

## Vérif de complétude
- [ ] `config get`/`list` affichent provenance + secrets masqués.
- [ ] Une mutation invalide (rôle inexistant, enum faux) est **refusée avant écriture**, exit ≠ 0, YAML intact.
- [ ] Round-trip : éditer un fichier commenté ne détruit pas les commentaires (assert dans un test).
- [ ] Headless (`--no-input`/non-TTY) : jamais de prompt, refus explicite si input requis.

## Exécution
Build-candidate déjà tagué → `/plan-build-test` sur les sprints 2 puis 3 (dépend de 1, déjà fait).
