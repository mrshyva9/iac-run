# 0006 — Isoler les détails runtime de l'exemple public

**Statut :** accepté

La configuration runtime propre à un déploiement n'appartient pas à l'arbre
d'exemple public. Les réglages de provider, endpoints, chemins de stockage et
identifiants sont fournis par un arbre privé.

Le dépôt reste ainsi réutilisable sans exposer de configuration propre à une
machine ni faire croire que l'exemple décrit un système opérationnel.
