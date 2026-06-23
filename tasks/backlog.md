# HivePilot — Backlog (post-session 2026-06)

Ordre de priorité : 1 → 4 pour l'opérationnel ; 5 & 6 = extensions produit (détaillées).

---

## 1. Valider la boucle autonome en vrai + finir le SIEM ⭐
Tout est codé+testé unitairement mais **jamais validé en run réel**. Le SIEM est à peine
entamé : `noxys-developer` runs = **3 success / 0 deferred / 13 failed** (les 13 = ancien
crash quota d'avant l'auto-resume → marqués `failed`, non repris). Seul **noxys-api** a du
vrai code (branches `feat/siem-tls-realtime-emission` + `hivepilot/noxys-api`, non mergées).

- [ ] Relancer le fan-out SIEM quota-résilient avec `HIVEPILOT_DEV_BATCH_SIZE=3`.
- [ ] **Valider en vrai** : fallback claude→codex→cursor produit du code ; isolation worktree
      isole bien (repo réel intact) ; topics par agent se créent ; auto-resume reprend au reset.
- [ ] Reviewer (Victor) → ouvrir les PRs → merger le SIEM noxys-api (humain merge).
- [ ] Finir les ~12 composants restants.
- [ ] Purger/relancer les 13 runs `failed`.

## 2. Distribution — dé-noxys-ifier HivePilot (ta cible)
Rendre l'outil générique/livrable.
- [ ] `_NOXYS_ROOT = "/home/jeromesoyer/Documents/Github/noxys"` **hardcodé** (`agent_rules.py`,
      utilisé par les 2 runners pour `governance_repo`) → réglage `governance_repo` en config.
- [ ] `obsidian_vault` chemin par défaut hardcodé → config/onboarding.
- [ ] Le groupe `noxys`, les noms d'agents FR, les `NOXYS_*` constants → templates de config
      (un projet = un fichier de config, pas du code).
- [ ] Packaging : `pip install hivepilot`, `hivepilot init` (génère projects/roles/policies/
      groups templates), doc de démarrage générique.
- [ ] Séparer "moteur HivePilot" (code) de "config produit" (noxys = un exemple).

## 3. Nettoyage / hygiène
- [ ] Ajouter `.hivepilot-wt/` au `.gitignore` des repos composants (worktrees jetables).
- [ ] Sort des branches `hivepilot/noxys-api` + `feat/siem-*` (merger ou supprimer).
- [ ] Réaligner le pointeur `main` local (divergent, cosmétique) : `git reset --hard origin/main`.
- [ ] Purger les worktrees périmés `.claude/worktrees/agent-*` (ceux de Claude Code).

## 4. Polish
- [ ] Doc à jour : quota-resilience, isolation worktree, /ask + alias + @mentions, topics.
- [ ] Valider qu'**Henri** (auditeur vibe/Mistral) produit un audit utile en vrai cycle.
- [ ] Messages : confirmer le rendu concis + liens vault cliquables sur un vrai run.

---

## 5. Runners IaC / config-management (extension produit) — DÉTAIL
Concept : un *runner* HivePilot exécute un outil en **non-interactif**, capture la sortie,
et s'insère dans le pipeline **plan → checkpoint → apply**. Le **dry-run/diff = le « plan »**
que l'humain approuve au checkpoint, puis **apply** réel. Tie-ins communs : `policies.yaml`
(`require_approval` pour la prod), git (commit de l'IaC), `secrets_service` (vault/values),
notifications (stream du diff), isolation worktree, SIEM (audit des apply).

Pattern de runner IaC (à généraliser depuis `PromptCliRunner`) :
- sous-commande **plan** (dry-run/diff) → produit l'artefact de plan,
- gate checkpoint (humain),
- sous-commande **apply**,
- parsing du résultat (changed/created/destroyed) → résumé + statut.

| Runner | Plan (dry-run/diff) | Apply | Notes |
|---|---|---|---|
| **Ansible** | `ansible-playbook --check --diff` | `ansible-playbook` | inventaire dynamique (`-i`), `--vault-password-file` via secrets_service, parse `changed=N`. Idempotent par nature → bon fit checkpoint. |
| **Salt** | `salt '*' state.apply test=True` (ou `salt-call --local state.highstate test=True`) | `state.apply` / `state.highstate` | master/minion **ou** masterless (`salt-call --local`), `pillar.refresh`. |
| **Chef** (legacy) | `chef-client --why-run` | `chef-client` | cookbooks ; `--why-run` = dry-run. |
| **Puppet** (legacy) | `puppet apply --noop` | `puppet apply` | manifests ; `--noop` = dry-run. |
| **Packer** | `packer validate` | `packer build` (HCL) | construit des images VM/conteneur ; artefact = image ID/AMI ; pas d'« apply prod » mais un build gate. |
| **Helm** | `helm diff upgrade` (plugin) **ou** `helm upgrade --dry-run` | `helm upgrade --install` / `helm rollback` | charts K8s ; values via secrets ; rollback natif = remédiation. |
| **Kustomize** | `kubectl diff -k` (ou `kustomize build \| kubectl apply --dry-run=server`) | `kubectl apply -k` | overlays K8s déclaratifs. |
| **Atlantis** | déclenche `atlantis plan` via API/webhook sur une PR | `atlantis apply` | modèle différent : Atlantis exécute Terraform **sur la PR** ; HivePilot **dispatche** (comme le worker distant) et lit le plan/apply. |

Implémentation : sous-classes de `PromptCliRunner` (déjà conçu pour CLI non-interactif),
avec `cli_subcommand`/flags propres à chaque outil + un parseur de résultat. Les rôles
(CTO/CISO) peuvent **reviewer le diff** au checkpoint avant apply → revue d'infra automatique.

---

## 6. Drift detection & remediation (extension produit) — DÉTAIL
But : détecter quand l'**état réel** diverge de l'**état déclaré (IaC)** et remédier sous gate.
Réutilise des briques **déjà présentes** : le **scheduler daemon** (périodicité), le
**checkpoint** (gate d'approbation), `notification_service` + **SIEM** (alerting), les
**runners IaC** (le diff).

### 6.1 Baseline / état désiré
- [ ] Snapshot de l'état attendu par cible : pour Terraform = state/last-applied ; pour
      Ansible/Helm/K8s = le rendu de l'IaC à un commit donné. Stocker (vault/DB) un hash/snapshot.

### 6.2 Scan de drift (périodique, via le scheduler daemon)
- [ ] Un `schedules.yaml` entry « drift-scan » qui, à intervalle, exécute le **plan/diff** de
      chaque cible sans apply :
  - Terraform : `terraform plan -detailed-exitcode` (exit 2 = drift).
  - Ansible : `--check --diff` (tâches `changed` = drift).
  - Helm/K8s : `helm diff` / `kubectl diff` (sortie non vide = drift).
  - Repo/config : comparer HEAD vs attendu.
- [ ] **Diff non vide ⇒ drift détecté** → produire un **rapport de drift** (quoi a changé, où).

### 6.3 Alerting
- [ ] Rapport de drift → `notification_service` (Telegram/stream) **+ event SIEM** (réutilise la
      brique SIEM du #1 : un drift = un security/ops event). Sévérité selon la ressource.

### 6.4 Remédiation sous gate
- [ ] Si drift : déclencher un pipeline de remédiation = **re-apply de l'IaC**, mais **gated par
      le checkpoint humain** (montrer le diff, demander Approve) — sauf cibles marquées
      `auto_remediate: true` en policy (apply auto pour le non-critique).
- [ ] Journaliser chaque remédiation (audit/SIEM).

### 6.5 Boucle complète
`schedule → scan (plan/diff) → drift? → rapport + alerte SIEM → checkpoint → remédiation (apply) → audit`
— c'est exactement le pipeline HivePilot existant, appliqué à l'infra au lieu du code.

---

## 7. Autres items ROADMAP (long terme, non bloquants)
- [ ] **Observabilité** : OpenTelemetry tracing pipeline+steps, propagation de spans entre
      runners, export Jaeger/Zipkin/OTLP, corrélation d'erreurs trace-aware.
- [ ] **Secrets avancés** : intégration HashiCorp Vault, fichiers SOPS, KMS AWS/GCP (envelope),
      rotation + TTL.
- [ ] **Supply chain** : scan de vulnérabilités (OSV, Grype), génération SBOM (CycloneDX/SPDX).
- [ ] **Multi-agent** : protocole agent-à-agent, passage d'artefacts, playbooks de patterns
      multi-agents, résolution de conflits quand 2 agents touchent les mêmes fichiers.
