# Opérations

Les commandes ci-dessous utilisent l'arbre fictif fourni. Ses adresses sont
réservées à la documentation, ses noms SSH utilisent `.invalid` et ses
commandes VM sont sans effet. Ce sont des exemples d'interface CLI, pas des
instructions pour opérer une machine réelle.

## Inspecter

```sh
./bin/modops tree
./bin/modops ls home
./bin/modops show vm-services
./bin/modops status vm-services
./bin/modops doctor
```

## Générer un arbre de déploiement

Préparer une spécification JSON en gardant les faits propres au déploiement
dans une copie de travail privée :

```sh
./bin/modops render examples/net-lab.json --out /tmp/iac-example --dry-run
./bin/modops --root /tmp/iac-example/home doctor
```

Relire les fichiers générés avant de les appliquer à un vrai déploiement.
Garder noms d'hôtes, adresses, alias SSH, chemins de clés et commandes runtime
hors des commits publics.

## Mutations

Pour un arbre privé configuré, inspecter d'abord le plan et exiger une
confirmation explicite :

```sh
./bin/modops start <node-id> --dry-run
./bin/modops start <node-id> --yes
```

L'arbre d'exemple ne cible aucune infrastructure réelle. Configurer un
provider et des valeurs runtime relues dans un arbre privé avant toute vraie
mutation.

## État persistant

Le CLI distingue état désiré et observé et journalise les opérations prises en
charge. Utiliser un répertoire d'état privé pour les données de déploiement ;
ne pas versionner observations ni journaux qui pourraient révéler des détails
d'infrastructure.
