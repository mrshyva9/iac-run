# ADR 0004 — Coordinateur et opérateurs de nœuds

Date : 2026-10-27 · Statut : accepté

## Contexte

Un agent unique qui lit toute l'infra consomme trop de contexte et mélange les
périmètres. À l'inverse, un agent par nœud sans coordinateur ne sait pas
enchaîner un plan.

## Décision

Deux rôles :

- **Coordinateur** : lit l'index compact, décompose, délègue, agrège. Il
  n'exécute aucun verbe mutant.
- **Opérateur de nœud** : lié à un nœud, ne voit que sa fiche, son `node.json`,
  ses enfants directs et ses verbes. Une étape = un verbe + args.

Le coordinateur ne peut pas confirmer une action `risk: high` : il escalade à
l'humain.

## Conséquences

- Contexte court et auditable par nœud.
- Un opérateur compromis reste dans son périmètre.
- Le coordinateur doit maintenir un plan explicite et un budget d'appels.
- Les erreurs de routage sont visibles dans l'audit (`verb`, `target`).
