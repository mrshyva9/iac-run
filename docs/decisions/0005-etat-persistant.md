# ADR 0005 — État persistant minimal et arrêt brutal accepté

Date : 2026-10-27 · Statut : accepté

## Contexte

L'infra doit pouvoir être remise dans un état connu après un reboot ou un arrêt
manuel. Un orchestrateur complet (base de données, démon, transactions) serait
disproportionné. Les VMs sont déjà arrêtées brutalement lors des tests, et
l'utilisateur accepte ce mode.

## Décision

1. `state/desired.json` ne stocke que la **puissance désirée** par nœud
   (`running` / `stopped`), pas la topologie : le tree reste la source de vérité.
2. `state/observed.json` est un **instantané** de la dernière observation.
3. `state/audit.jsonl` journalise chaque mutation (acteur, verbe, cible, args,
   résultat, durée).
4. `modops restore` calcule le diff, arrête les applications avant les VMs,
   démarre bridge → VMs → applications, puis réécrit l'observation.
5. `stop --force` fait SIGTERM, attend 8 s, SIGKILL, puis supprime pidfile et
   socket QMP. Les disques et volumes ne sont jamais supprimés.
6. Un `start`, `stop` ou `restart` réussi met à jour `desired.json` : l'état
   persiste sans commande de synchronisation séparée.
7. Aucun démon : chaque commande est un processus court, l'état est sur disque.

## Conséquences

- Un arrêt brutal peut laisser le guest dans un état sale : `fsck` au démarrage.
- L'état n'est pas versionné et peut être perdu ; il est reconstructible par
  `state set`.
- Pas de verrou distribué : deux `restore` simultanés peuvent se chevaucher.
  Un verrou par nœud (`state/locks/`) est prévu.
- Le désiré ne couvre que la puissance en v0.1 ; adresses et liens restent dans
  `node.json`.
