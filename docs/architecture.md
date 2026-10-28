# Architecture

## Composants

- **Arbre filesystem** : un répertoire par nœud d'infrastructure.
- **Métadonnées** : `node.json` contient identité, kind, parent et runtime
  facultatif.
- **Descripteurs de kind** : `tree/kinds/` définit les défauts et les règles de
  contenance.
- **CLI** : `bin/modops` expose des verbes déterministes de lecture et mutation.
- **Providers** : `iacrun/providers.py` observe les ressources configurées ;
  `iacrun/power.py` gère les opérations de puissance supportées.
- **État persistant** : état désiré, observations et audit sont séparés de
  l'arbre descriptif.

## Flux de données

```text
node.json + descripteurs de kind
              │
              ▼
        arbre validé
        ┌─────┴──────┐
        ▼            ▼
      lecture    plan de mutation
                     │
               confirmation
                     │
                  provider
                     │
          état observé + audit
```

L'arbre fourni dans `tree/home` est illustratif et n'emploie que des valeurs
runtime fictives. Les vrais déploiements doivent garder adresses privées,
noms d'hôtes, alias SSH, identifiants et scripts propres au déploiement hors
des dépôts publics.

## Limites

L'arbre décrit des ressources ; ce n'est ni un shell généraliste ni un
remplacement d'un système complet de provisionnement. Un modèle peut proposer
une spécification, mais le CLI la valide et n'effectue une opération qu'après
confirmation explicite.
