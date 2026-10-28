# iac-run

CLI déterministe pour décrire et opérer une petite arborescence
d'infrastructure. Les nœuds sont stockés en JSON et Markdown ; les providers
n'observent ou ne pilotent que les ressources explicitement configurées.

## Principes

- Un répertoire et un `node.json` par nœud.
- Lecture seule par défaut ; les mutations exigent une confirmation.
- Providers explicites et arguments validés.
- Les arbres générés sont validés avant leur écriture.
- La configuration runtime dépend du déploiement et doit être relue avant usage.

## Démarrage rapide

```sh
./bin/modops tree
./bin/modops doctor
./bin/modops show vm-services
./bin/modops status vm-services
```

Prévisualiser les mutations avant de les appliquer :

```sh
./bin/modops start vm-services --dry-run
./bin/modops start vm-services --yes
```

L'arbre fourni est fictif : il utilise des plages IP de documentation, des
alias SSH réservés au domaine `.invalid` et une commande VM sans effet. Il
n'est relié à aucune infrastructure réelle. Relire et remplacer les
placeholders avant de configurer un déploiement privé.

## Structure

```text
bin/modops          point d'entrée CLI
iacrun/             CLI, modèle d'arbre, providers, état, génération
tree/home/          inventaire fictif
tree/kinds/         défauts et règles de contenance par kind
schema/             JSON Schema des métadonnées
examples/           exemple d'entrée pour générer un arbre
docs/               architecture, opérations, sécurité, workflow
tests/              tests unitaires
```

Conserver les inventaires propres au déploiement, les identifiants, la
configuration SSH et les commandes runtime dans une copie de travail privée.

## Documentation

- [Architecture](docs/architecture.md)
- [Opérations](docs/operations.md)
- [Sécurité](docs/security.md)
- [Workflow agent](docs/agent-workflow.md)
- [Spécification](docs/specification.md)
