# ADR 0003 — Métadonnées JSON, prose Markdown

Date : 2026-10-27 · Statut : accepté

## Contexte

Le projet doit rester sans dépendance installable et lisible en bash. YAML
demande PyYAML ou un parseur maison fragile.

## Décision

- `node.json` : faits machine, validés par un JSON Schema.
- `AGENT.md` : périmètre, mission, interdits, escalade.
- Pas de YAML dans le dépôt.
- `jq` est l'outil de lecture optionnel en bash.

## Conséquences

- Zéro dépendance Python (stdlib).
- Validation et parsing triviaux.
- JSON est plus verbeux que YAML ; les commentaires vont dans `AGENT.md` ou
  `notes`.
- Les scripts de l'installeur peuvent rester en YAML (cloud-init) : ce sont des
  entrées, pas la source de vérité `iac-run`.
