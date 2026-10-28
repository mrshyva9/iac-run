# ADR 0007 — Scaffolder et génération depuis une spécification

Date : 2026-10-27 · Statut : accepté

## Contexte

Les opérateurs savent agir sur un nœud existant, pas en créer un. La demande
« dans ce réseau, crée une VM de tel type » doit produire deux choses : la
**représentation** (`node.json`, `AGENT.md`) et l'**instanciation** (`runtime`,
`resources`, `health`, `links`). Laisser un modèle écrire des fichiers ou du
shell rendrait le résultat invérifiable.

## Décision

Troisième rôle, le **scaffolder**, avec deux entrées CLI :

- `modops new PARENT --spec FILE|-` : un nœud sous un parent existant ;
- `modops render FILE|- --out DIR` : une arborescence complète depuis un dépôt
  descriptif JSON (`schema: 1`, `root` + `nodes` imbriqués).

La spécification est du JSON, comme `node.json`. Les défauts `provider`,
`verbs` et `risk` viennent du descripteur `tree/kinds/<kind>.json` ; `health` a
un défaut déduit du kind : le modèle ne les invente pas. Le scaffolder ne voit
que le parent (fiche, métadonnées, enfants, CIDR) : il ne peut pas créer hors de
son périmètre.

Toute génération est validée par `Tree.validate()`. En cas d'erreur, les
fichiers créés sont supprimés (rollback) : jamais d'arborescence à moitié
générée. `--dry-run` liste les fichiers sans écrire ; `--yes` applique.

## Conséquences

- Le modèle produit un objet JSON, pas du shell ni des fichiers.
- Un rendu est déterministe : mêmes entrées, mêmes fichiers, donc comparable.
- Un nœud sans `runtime` est déclaratif (`status: unknown`,
  `start: unsupported`) : la représentation peut précéder l'instanciation.
- Les créations et refus sont audités (`state/audit.jsonl`).
- Le prompt associé est `agent/prompts/scaffolder.md` ; le workflow complet est
  dans `docs/agent-workflow.md`.
