# Workflow agent

Les agents peuvent traduire une demande en proposition de spécification de
nœud. Ils ne modifient pas directement l'infrastructure.

## Séquence recommandée

1. Lire la fiche du nœud concerné et son descripteur de kind.
2. Demander au scaffolder une proposition JSON.
3. Valider la proposition avec `modops new` ou `modops render --dry-run`.
4. Relire le diff pour repérer champs inattendus ou informations privées.
5. Appliquer la génération seulement après confirmation humaine explicite.
6. Utiliser `doctor` et inspecter l'arbre obtenu.

## Exemple

```sh
./bin/modops render examples/net-lab.json --out /tmp/iac-example --dry-run
```

L'exemple utilise des valeurs réservées à la documentation et des placeholders
runtime. Le remplacer uniquement dans une copie privée de déploiement. Ne pas
fournir aux agents d'identifiants, de configuration SSH privée ni de
métadonnées d'autres nœuds.

## Limites des agents

- Le coordinateur route les demandes et délègue un périmètre borné.
- L'opérateur de nœud ne lit que son périmètre et ses verbes autorisés.
- Le scaffolder propose des métadonnées et n'exécute aucune commande provider.
- Les mutations passent par le CLI déterministe, validation et confirmation
  humaine.
