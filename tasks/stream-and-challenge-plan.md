# Plan — Stream en langage naturel + culture de challenge entre agents

## Partie A — Stream Telegram plus naturel & mieux formaté

### Constat (investigation code)
- `stream_agent_turn` envoie du **texte brut** (pas de `parse_mode`), header OK
  (`icon (label) Nom (Titre) — stage` + `↳ cible`), puis le `summary`.
- Le `summary` = **`stage_output` = sortie LLM BRUTE** (`r.detail`), juste
  whitespace-collapsée et tronquée à 1500 car. D'où le côté technique/illisible.
- Les agents émettent DÉJÀ un format structuré (`status`, `summary` 3-5 puces,
  `decisions`, `blockers`, `next_handoff`, `confidence`) — mais **RIEN ne le parse**
  aujourd'hui. On jette de l'or : on affiche le dump au lieu des puces.

### Plan
1. **Parser** `parse_agent_report(text)` → extrait status / summary(puces) /
   next_handoff / confidence / blockers depuis la sortie de l'agent (tolérant :
   si rien ne matche → None).
2. **Renderer de carte** propre par tour (au lieu du dump) :
   - En-tête : `🗣 Blaise (CTO) → Hugo (CISO)` (nom+titre, flèche vers le suivant).
   - Badge statut : ✅ PASS · ⛔ BLOCKED · 🙋 NEEDS_HUMAN.
   - Corps : les **puces `summary`** de l'agent (langage naturel), pas le brut.
   - Hand-off : « Passe la main à Hugo : <contexte next_handoff> ».
   - Confiance : `MEDIUM — raison` (optionnel, en gris).
3. **Formatage Telegram** : passer `parse_mode=HTML` (plus robuste que MarkdownV2),
   nom en gras, puces, et transformer le chemin du vault en **lien cliquable**.
4. **Fallback** : si pas de champs structurés (ex. debate « synthesis (path) »),
   afficher un excerpt nettoyé + le lien, pas le brut.
5. **Renforcer côté prompts** (lié à la Partie B) : demander à chaque agent une
   ligne `summary` **en langage clair, non-jargon**, pensée pour le canal d'équipe.

## Partie B — Les agents doivent se challenger

### Constat
Les prompts sont « fais ton job et passe la main ». Aucune instruction de
**critique de l'amont**. D'où : Jules ne challenge pas CTO/CISO, CTO/CISO ne
challengent pas le CEO, le dev exécute sans contester le plan.

### Plan — ajouter une section « Challenge upstream » à chaque prompt
- **CTO (Blaise)** : challenger la demande du CEO — scope irréaliste, infos
  manquantes, priorités discutables ; pousser un `rejection_notice` si besoin.
- **CISO (Hugo)** : challenger l'archi du CTO sur le plan sécu/souveraineté ;
  ne pas approuver par défaut ; exiger des preuves.
- **CSO (Jules)** : challenger ET réconcilier CTO vs CISO ; pointer les
  contradictions et les zones où la demande CEO est sous-spécifiée.
- **Developer (Gustave)** : challenger le plan reçu du CSO — si ambigu/risqué/
  incomplet, remonter en `blockers`/`NEEDS_HUMAN` au lieu d'implémenter en silence.
- **Reviewer/QA** : déjà critiques par nature ; renforcer « refuser, pas polir ».
- Cadre commun : chaque agent ouvre par une courte **évaluation critique de
  l'amont** (accord/désaccord + pourquoi) avant de produire sa contribution.
- Henri (auditeur) observe déjà ce comportement → il pourra coacher sur le manque
  de challenge.

### Risque / dosage
Trop de challenge = boucles/blocages. Garde-fou : critique CONCISE, orientée
décision, et le checkpoint humain tranche. Mesurer via les retros d'Henri.

## Ordre suggéré
1. Partie B (prompts) — rapide, gros impact qualité + rend le stream plus vivant.
2. Partie A (parser + renderer + HTML + topics déjà prêts) — rend l'observation lisible.
