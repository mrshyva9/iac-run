# ADR 0002 — Déterministe d'abord, LLM en secours

Date : 2026-10-27 · Statut : accepté

## Contexte

Les modèles locaux sont utiles mais coûteux en VRAM, en latence et en tokens.
La plupart des opérations sont connues à l'avance.

## Décision

1. Une commande `modops` explicite ne consomme aucun token.
2. Un routeur déterministe (regex + alias) traite les demandes simples.
3. Le LLM n'intervient que pour une intention ambiguë, un plan multi-nœuds ou
   un diagnostic.
4. L'opérateur de nœud reçoit une fiche ≤ 45 lignes et un schéma d'outils fixe.
5. Budget : 6 étapes, 4 appels LLM, 1 800 tokens de contexte, 256 tokens de
   sortie.

## Conséquences

- L'outil reste utilisable sans service de modèle.
- Le coût IA est proportionnel à l'ambiguïté, pas au nombre d'actions.
- Le routeur doit être testé et maintenu.
- Un modèle plus gros n'est utilisé que sur `--deep` ou après échec.
