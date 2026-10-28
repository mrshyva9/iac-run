# Agent iac-run

Trois rôles, un seul protocole JSON. Les prompts sont volontairement courts :
chaque token est budgété (ADR 0002).

```text
utilisateur
   │
   ├─ routeur déterministe ───────────────► modops <verbe>     0 token
   │
   ├─ coordinateur ──► opérateur nœud A ──► modops <verbe> A
   │               └─► opérateur nœud B ──► modops <verbe> B
   │
   └─ scaffolder ──────────────────────────► modops new | render
```

## Fichiers

| Fichier | Rôle |
|---|---|
| `prompts/coordinator.md` | prompt système du coordinateur |
| `prompts/node-operator.md` | prompt système d'un opérateur |
| `prompts/scaffolder.md` | prompt système du générateur de nœuds |
| `tools.json` | schéma compact des verbes exposés au modèle |

## Assemblage du contexte

**Coordinateur** : `AGENT.md` du site + index compact (id, kind, summary) +
demande + budget. Pas de `node.json` complet de chaque nœud.

**Opérateur** : `AGENT.md` du nœud + `node.json` du nœud + enfants directs
(id, kind, summary) + verbes autorisés + dernière observation tronquée.

**Scaffolder** : `AGENT.md` et `node.json` du parent + enfants directs + CIDR
du réseau parent + demande. Il ne reçoit ni l'index global ni les autres
branches : il ne peut créer que sous le parent courant.

## Règles

- Une réponse = un objet JSON, rien d'autre.
- Un verbe inconnu est un échec, jamais une approximation.
- Pas de shell, pas de bloc de code, pas de prose.
- `risk: high` → `escalate: human`.
- `new`, `render` et `move` sont `risk: medium` : dry-run puis confirmation.
- `move` ne migre pas les données : copier compose et volume, puis recréer le
  conteneur sur la cible.
- Maximum 2 appels LLM par étape, 6 étapes, 4 appels au total.

## Routeur déterministe

Avant tout LLM, `modops ask` tente :

1. un `id` ou alias présent dans la demande ;
2. un verbe de lecture explicite (`status`, `santé`, `logs`, `où`, `montre`) ;
3. un inventaire explicite (`liste les VMs`, `les applications`, `combien de
   nœuds`, `montre l'infra`).

Un `status` avec « global », « tous » ou « récursif » porte sur le nœud
mentionné et ses descendants (`status PATH --recursive`).

Une action mutante n'est jamais exécutée par `ask` : le routeur renvoie
`explicit_verb_required` (code 5) avec la commande exacte à lancer. Une création
renvoie `scaffold_required` (code 3). Si la demande ne rentre dans aucun cas,
le coordinateur prend le relais.
