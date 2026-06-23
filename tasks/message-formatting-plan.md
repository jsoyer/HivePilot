# Plan — Lisibilité des messages Telegram (stream + approbation)

## Problème
La carte (#61) affiche le `summary` parsé de l'agent. Pour le **Developer**, ce summary
est un MUR : liste de fichiers, tableaux markdown (`| … |`), `##`, `---` — rien de tout
ça ne rend dans Telegram, et c'est trop long. D'où l'illisibilité signalée.

## Brique 1 — Renderer concis & Telegram-safe (notification_service / agent_report)
- **Strip le markdown non-rendu** avant envoi : retirer/convertir `#`/`##`, `---`,
  tableaux `| … |` → lignes simples ; garder **gras + puces** uniquement.
- **Cap dur du corps** : ~5 puces / ~700 chars max, puis « … détails complets dans le
  vault → <lien> ».
- **Carte dev dédiée** : pour le rôle developer, afficher un résumé **structuré court** au
  lieu du dump : `status` badge + 1 ligne « N fichiers, N tests, branche `feat/…` » +
  hand-off, pas la liste exhaustive des fichiers.

## Brique 2 — Conciseness imposée à TOUS les agents (prompts)
- Étendre la règle de #74 (CoS) à tous les rôles : `summary` = **≤5 puces, langage clair,
  pas de tableaux ni de dump de fichiers**. Le détail exhaustif (fichiers, payloads,
  matrices) va dans **l'artefact vault**, jamais dans le stream/approbation.
- Le `next_handoff` reste 1 ligne.

## Brique 3 — Lien vault cliquable propre
- Déjà partiellement là (#61 transforme les chemins `.md` en liens). Vérifier que le
  lien de l'artefact du stage est bien rendu dans la carte + le DM d'approbation, et
  pointe vers le bon fichier.

## Ordre
1. Brique 1 (renderer) — gros gain immédiat, code only.
2. Brique 2 (prompts) — borne la source.
3. Brique 3 (lien) — finition.

## Note
À construire après le reset quota (sous-agents = claude). Coût quota du build modéré.
