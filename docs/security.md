# Sécurité

## Contenu du dépôt

Ne pas versionner d'identifiants, clés privées, tokens, mots de passe, noms
d'hôtes de production, inventaires de réseaux privés, configuration SSH ou
commandes runtime sensibles. L'arbre fourni est fictif et utilise des plages
IP réservées à la documentation.

## Modèle opérationnel

- Privilégier l'inspection en lecture seule.
- Prévisualiser les mutations avec `--dry-run`.
- Exiger une confirmation explicite pour les écritures et opérations réelles.
- Utiliser des commandes provider limitées et des permissions minimales.
- Garder état désiré, observations et journaux d'audit hors du contrôle de
  version public s'ils contiennent des faits de déploiement.
- Traiter la sortie du modèle comme une entrée non fiable ; valider verbes et
  arguments avant exécution.

Le modèle n'est pas une frontière de sécurité. Ne pas configurer un provider
ou une opération privilégiée pour exécuter du shell arbitraire fourni par un
agent.

## Avant publication

Relire le commit et tout son historique, pas seulement l'arbre de travail.
Supprimer un secret de la dernière version ne le retire pas des anciens
commits. Si un identifiant a été versionné, le renouveler et retirer sa trace
de l'historique publié avant de rendre le dépôt public.
