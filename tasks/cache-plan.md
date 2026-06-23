# Plan — Réduction des tokens (caching & contexte)

## Constat (audit du code)
- **Aucun cache** aujourd'hui (`cache_control`, ephemeral, memoization → zéro).
- **Gouffre #1 — `prior_context` en boule de neige** (`orchestrator.py:412/425`) :
  chaque stage reçoit la CONCATÉNATION de TOUTES les sorties précédentes. Au stage
  10, Théo reçoit les 9 stages d'avant → coût ~quadratique sur la fin du pipeline.
- **Gouffre #2 — pas de prompt caching** : les gros préfixes statiques (prompt de
  rôle + CLAUDE.md + docs de gouvernance) sont renvoyés plein tarif à chaque appel.
- **Gouffre #3 — resume re-exécute tout** : après le checkpoint, un re-run rejoue
  les stages 1-4 déjà faits.
- Rappel : **`/ask` n'a AUCUN `prior_context`** → déjà l'option la moins chère.

## Couches proposées (par ROI décroissant)

### Couche 1 — Prompt caching Anthropic [ROI MAX, effort faible] ⭐ préconisé
- Mode API (`prompt_cli_runner._run_api`, provider anthropic) : marquer le préfixe
  statique (system/role + gouvernance) avec `cache_control: {type: ephemeral}`.
  → réutilisation à ~10 % du coût pendant 5 min (durée d'un cycle).
- `claude --print` : le CLI fait déjà son propre prompt caching ; on en profite en
  gardant les sections STABLES en tête de prompt (déjà le cas) et en évitant
  d'injecter du volatil (timestamps) tôt. Action : ordonner stable→volatil.
- OpenAI/compatibles : caching auto des préfixes ≥1024 tokens → même bénéfice si
  préfixe stable.

### Couche 2 — Capper/résumer `prior_context` [ROI élevé, effort moyen] ⭐ préconisé
- Ne plus passer TOUT l'historique. Options (config `prior_context_mode`) :
  - `synthesis` : ne passer que la synthèse de Jules + le stage immédiatement
    précédent.
  - `cap` : tronquer à `max_prior_context_chars` (ex. 6000) avec coupe propre.
- Plus gros impact sur les stages 5-10. Aucun changement de qualité de sortie
  attendu (les agents n'ont pas besoin du verbatim de tous les prédécesseurs).

### Couche 3 — Mémoïsation de stage / skip-unchanged [ROI moyen]
- Hash des entrées d'un stage (prompt + modèle + contexte résolu + HEAD du repo).
  Si inchangé vs un run précédent → réutiliser la sortie cachée au lieu de
  relancer l'agent. Stockage : `state.db` ou `.hivepilot/stage_cache/`.
- Gain clé : **`resume_pipeline` ne rejoue PAS les stages 1-4** après le checkpoint.

### Couche 4 — Bon dimensionnement des modèles [orthogonal, gros $]
- Déjà partiel (modèles par rôle). Vérifier que les rôles « légers » (docs, QA)
  tournent sur des modèles bon marché (haiku / gemini-flash). Pas du cache mais
  levier de coût direct.

## Préconisation
1. **Couche 1 + Couche 2** d'abord : l'essentiel du coût, risque faible, sorties
   inchangées.
2. **Couche 3** ensuite, surtout pour rendre le resume gratuit.
3. **Couche 4** = réglage config continu.
4. Promouvoir **`/ask`** pour le travail ciblé (déjà sans surcoût pipeline).

## Découpage d'implémentation (si go)
- [ ] config : `prior_context_mode` (`full|synthesis|cap`), `max_prior_context_chars`,
      `anthropic_prompt_cache` (bool).
- [ ] orchestrator : construire `prior_context` selon le mode (couche 2).
- [ ] prompt_cli_runner._run_api : blocs `cache_control` anthropic (couche 1).
- [ ] claude_runner : garantir l'ordre stable→volatil du prompt (couche 1).
- [ ] (couche 3) cache de stage hashé + court-circuit dans run_task/resume.
- [ ] tests : prior_context tronqué/synthétisé ; payload anthropic porte cache_control.
- [ ] doc CONFIG.md.
