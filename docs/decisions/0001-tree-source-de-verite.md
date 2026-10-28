# ADR 0001 — L'arborescence est la source de vérité

Date : 2026-10-27 · Statut : accepté

## Contexte

L'infra est petite mais hétérogène : un hôte, un bridge, deux VMs, deux
applications. Un inventaire centralisé (base, API) serait plus lourd que
l'infra elle-même. Les agents ont besoin d'un périmètre court.

## Décision

Chaque nœud est un répertoire de `tree/` contenant `node.json` et `AGENT.md`.
Le chemin encode la topologie. Un nœud n'existe qu'à un seul endroit.

## Conséquences

- Navigation naturelle en bash, `cd`, `ls`, globs, `git diff`.
- Contexte agent = un répertoire, pas tout le dépôt.
- Les vues multi-réseau doivent être générées, pas dupliquées.
- Un déplacement de nœud est un `git mv` + mise à jour de `node.json`.
- Risque : le tree dérive du réel → `doctor` et `diff` sont obligatoires.
