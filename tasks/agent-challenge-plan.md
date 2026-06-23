# Plan — Challenges inter-agents : visibilité (A) + vrai va-et-vient (B)

État actuel : le challenge est émis (ex. `cto.md` → `rejection_notice`) mais **noyé dans
le résumé**, non parsé, non rendu distinctement ; pipeline **linéaire** (1 tour/agent,
pas de ping-pong). A rend visible ; B ajoute un vrai round de rebuttal. A est le socle de B.

---

## A — Rendre les challenges VISIBLES (socle, faible risque)

### A1. Champ structuré côté agents (prompts)
- Normaliser un champ de sortie **`challenge:`** sur les rôles qui contestent l'amont
  (CTO, CISO, CSO, Developer) : `challenge: <agent amont> — <objection concise> | none`.
- Garder les champs existants (`rejection_notice` pour CTO, `blockers`/`NEEDS_HUMAN`) ;
  `challenge` est la forme canonique destinée à l'affichage.

### A2. Parsing (`agent_report.py`)
- Étendre `parse_agent_report` → champ `challenge: ChallengeInfo | None`
  (`target` = agent amont visé, `point` = l'objection). Parser `challenge:` ET, en repli,
  `rejection_notice:` / un `blockers` qui référence un agent amont.
- Pur + testé.

### A3. Rendu distinct (stream + interactions)
- `notification_service` : quand `report.challenge` existe, **stream un tour dédié** avec
  icône **`⚔️`** : `⚔️ Blaise (CTO) challenge Aliénor (CEO) — priorités non classées`,
  posté dans le topic de l'agent qui challenge (et/ou un topic « Débats »).
- Ajouter `⚔️` aux `_ICON_LABELS` (label « challenge »).
- `interaction_service` : logger le challenge comme **interaction distincte** (actor →
  target, action="challenge", summary=point) → visible dans la timeline + le vault.

### A4. Tests
- `parse_agent_report` extrait `challenge` (forme directe + repli rejection_notice).
- `stream_agent_turn`/helper émet le tour `⚔️` quand un challenge est présent.
- interaction « challenge » enregistrée.

---

## B — Vrai va-et-vient (round de rebuttal)

But : quand un agent conteste l'amont, **renvoyer l'objection à l'amont pour une réponse**,
avant de continuer le pipeline → débat réel, visible, borné.

### B1. Détection
- Après la sortie d'un stage, si `report.challenge` cible un **agent amont déjà passé**
  (et statut BLOCKED/NEEDS_HUMAN ou rejection_notice non vide) → déclencher un **round de
  rebuttal** (sauf si désactivé).

### B2. Round de rebuttal (orchestrator)
- Ré-invoquer l'**agent amont visé** avec, en contexte : sa sortie initiale + l'objection
  du challenger. Prompt : « réponds à cette objection : accepte+révise, défends avec
  arguments, ou escalade ». Capturer la **réponse (rebuttal)**.
- Puis (optionnel, round 2) : redonner le rebuttal au challenger → il **accepte** (résolu)
  ou **maintient** (escalade `NEEDS_HUMAN`).
- **Cap** : `max_challenge_rounds: int = 1` (config) pour éviter les boucles.
- **Convergence** : non résolu après N rounds → marquer `NEEDS_HUMAN`, le checkpoint humain
  tranche. La résolution (accord ou escalade) est injectée dans `prior_context` pour la suite.

### B3. Coût / sécurité
- Chaque rebuttal = 1 appel agent **amont** (opencode/cursor, **pas claude** → quota
  préservé). Borné par `max_challenge_rounds`.
- N'enclenche le débat que pour des objections réelles (rejection_notice / blockers /
  NEEDS_HUMAN ciblant l'amont), jamais sur un simple désaccord cosmétique.

### B4. Visibilité du débat (réutilise A)
- Streamer la séquence avec icônes : `⚔️ challenge` → `🛡️ rebuttal` → `⚖️ résolu` /
  `🙋 escaladé`. Dans un **topic « Débats »** dédié pour suivre le fil.
- Chaque tour = une interaction loggée (timeline + vault).

### B5. Config & garde-fous
- `enable_challenge_rounds: bool = True`, `max_challenge_rounds: int = 1`.
- Le checkpoint humain reste l'arbitre final ; le débat ne fait que **clarifier** avant.

### B6. Tests
- Un challenge déclenche 1 rebuttal de l'amont (mock runners) ; round cappé ;
  résolution → `prior_context` enrichi ; non-résolu → `NEEDS_HUMAN`.

---

## Ordre de build
1. **A** (A1→A4) — visibilité, socle, faible risque.
2. **B** (B1→B6) — s'appuie sur le parsing + le streaming de A ; touche la boucle de stages
   de l'orchestrator (cœur) → tests complets exigés, idempotence du re-run.

## Note quota
A = surtout prompts + rendu (coût quota nul à l'usage). B = +1 appel amont par challenge
(opencode/cursor, hors quota claude). Build des sous-agents = après reset si nécessaire.
