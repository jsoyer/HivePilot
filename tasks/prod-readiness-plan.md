# Plan — Prod-readiness : dé-noxys-ifier + durcir l'orchestrateur

Déjà présent : `pyproject.toml`, `Dockerfile`, générateurs systemd (api/scheduler/telegram),
`audit_log`, tokens RBAC (read/run/approve/admin), rate-limit API, dashboard.
Manque : **CI**, et surtout la **génericisation** (le noxys est codé en dur, aggravé par les
renommages récents).

---

## PARTIE 1 — Génériciser (dé-noxys-ifier) ⭐ prérequis distribution

### 1a. Chemins en dur → config
- `agent_rules.py` : `_NOXYS_ROOT`, `_VAULT_SECURITY`, les constantes `NOXYS_*` (6 rule files)
  → réglages `governance_repo` + `governance_files: list[str]` + vault depuis settings.
- `config.py` : `obsidian_vault` (chemin absolu en dur), `default_target="noxys"` → sans défaut
  noxys ; valeurs via config/onboarding.

### 1b. Rôles en CODE → config
- `roles.py::ROLES` (dict en dur, noms FR) → **`roles.yaml`** chargé (garder un fallback code).
  Permet à chaque déploiement de définir ses agents sans toucher au code.

### 1c. Noms pipeline/tâches codés en dur → dérivés de la config
- `telegram_bot._AGENT_REGISTRY` : tâches `noxys-*` en dur → **dérivées des rôles/config**
  (alias → role → task résolus, pas de littéraux `noxys-`).
- `pipeline_name="noxys-v2"` (2 occurrences) + `@noxys` → réglage `default_pipeline` /
  `default_group` (le groupe par défaut, pas le littéral).
- `orchestrator.py:1132` : `stage.task == "noxys-documentation"` (commit vault) → **flag de
  stage** (`stage.commits_vault: bool`) ou détection par rôle, pas par nom de tâche en dur.

### 1d. Séparer moteur / config produit
- Le repo = **moteur HivePilot** (code) ; noxys = **un exemple de config** (projects/roles/
  policies/groups/pipelines/prompts) dans `examples/noxys/`.
- `hivepilot init` : scaffold une config vierge (templates) pour un nouveau déploiement.
- Validation de config au démarrage (projects/roles/policies/pipelines cohérents) + erreurs claires.

---

## PARTIE 2 — Durcissement prod (mes ajouts) — par priorité

### 2a. 🔴 Sécurité du dev autonome (LE gros risque prod)
`developer = claude bypassPermissions` sur de **vrais repos** = exécution arbitraire.
- **Sandbox** : exécuter le dev en conteneur/firejail/VM, **mount limité au repo composant**,
  **env scrubbé** (pas de clés SSH/secrets de l'hôte). (Le runtime conteneur existe déjà.)
- **Credentials scoping** : `merge_environments` ne doit PAS exposer les secrets globaux à un
  rôle en bypass ; secrets par-rôle, least-privilege.
- **Kill-switch** global + plafond de débit ; l'isolation worktree (faite) protège l'arbre, pas
  l'exécution → la sandbox complète le tableau. (Déjà flaggé par la revue sécu auto de la session.)

### 2b. 🔴 Secrets
- Plus de tokens en clair (`api_tokens.yaml`/`.env` — un token a déjà fuité cette session).
- Intégration **Vault / SOPS / KMS** (ROADMAP), rotation + TTL, secrets chiffrés au repos.

### 2c. 🟠 Auth / multi-tenant
- RBAC tokens existe → étendre : audit par utilisateur (table `audit_log` présente), isolation
  par tenant/équipe (plusieurs produits), expiration/rotation des tokens.

### 2d. 🟠 Observabilité & exploitation
- Endpoints **/healthz** + **/readyz**, métriques **Prometheus** (runs, échecs, quota, latence).
- **OTel tracing** (ROADMAP). **Dogfood le SIEM** : HivePilot émet ses propres interactions/
  alertes vers un SIEM (réutilise la brique du #1).
- **Budget/coût** : plafond tokens par run/projet, reporting de coût (style `rtk gain`).

### 2e. 🟡 Scale / état
- `state.db` SQLite = OK mono-hôte. Multi-hôte → backend **Postgres** (le cache L3 est déjà
  pluggable Redis ; les workers W1-W4 existent). Migrations de schéma.

### 2f. 🟡 CI/CD & release
- **GitHub Actions** (absent) : tests + ruff + mypy sur PR, gate de couverture, build image,
  release versionnée (tags, changelog).

### 2g. 🟢 Docs
- Doc d'install/exploitation générique (pas noxys), runbook (démarrer api/scheduler/telegram,
  rotation secrets, reprise quota, sandbox).

---

## Ordre suggéré (pour « utiliser en prod »)
1. **Partie 1** (génériciser) — sans ça, pas distribuable.
2. **2a + 2b** (sandbox dev + secrets) — sans ça, pas safe en prod.
3. **2f + 2d** (CI + health/metrics) — exploitabilité.
4. **2c + 2e + 2g** (multi-tenant, Postgres, docs) — selon l'échelle visée.
