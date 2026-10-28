# Spécification iac-run v0.1

Statut : **v0.1**. Sont implémentés : lecture, puissance (`start`, `stop`,
`restart`), persistance d'état (`state`, `restore`) et génération de nœuds
(`new`, `render`). Restent spécifiés mais non implémentés :
`net.create`, `net.attach`, `net.detach`, `net.move`, `connect`, `disconnect`.

## 1. Objectifs

1. Décrire l'infra dans une **arborescence filesystem** navigable en bash.
2. Offrir une **API homogène** (`modops`) à un humain et à un agent.
3. Donner à chaque agent un **périmètre borné** : un nœud, sa fiche, ses verbes.
4. Minimiser les appels LLM : le déterministe d'abord, le modèle en secours.
5. Rendre les actions **observables, idempotentes et auditables**.

## 2. Non-objectifs

- Orchestrateur multi-hôte ou cloud.
- Remplacement de Terraform, Ansible, libvirt ou de l'installeur `compute`.
- Shell arbitraire piloté par un LLM.
- Génération sans spécification : le dépôt descriptif JSON reste la source, un
  modèle ne fait que proposer un nœud ou une arborescence.

## 3. Vocabulaire

| Terme | Définition |
|---|---|
| **Nœud** | Répertoire de `tree/` avec `node.json` + `AGENT.md`. |
| **Branche** | Nœud `network` : un segment L2/L3 (WAN, LAN). |
| **Feuille** | Nœud `app` ou `device`, sans enfant opérationnel. |
| **Provider** | Adaptateur qui parle au réel : `local`, `qemu`, `ssh`, `docker`, `http`. |
| **Verbe** | Action atomique de l'API (`status`, `start`, `net.move`, …). |
| **Opérateur** | Agent LLM lié à un nœud, qui choisit un verbe autorisé. |
| **Coordinateur** | Agent LLM qui décompose et délègue, sans agir directement. |
| **Scaffolder** | Agent LLM qui propose `spec` pour `new`/`render` ; il ne touche jamais au réel. |
| **Observation** | Résultat structuré d'un verbe, tronqué pour le LLM. |

## 4. Modèle d'arborescence

### 4.1 Règle fondamentale

**Un nœud = un répertoire = un `id` unique.** Un nœud n'est jamais dupliqué.
Le chemin encode la topologie :

```text
site / réseau / hôte / réseau / bridge / vm / app
```

Une vue réseau secondaire (multi-homing) sera générée (v1) dans
`state/views/`, jamais matérialisée par des copies. En v0, l'appartenance est
portée par `addresses[].network`.

### 4.2 Contenance autorisée

| Parent | Enfants autorisés |
|---|---|
| `site` | `network` |
| `network` | `host`, `bridge`, `vm`, `container` |
| `host` | `network`, `vm`, `device`, `app` |
| `bridge` | `vm`, `container`, `app` |
| `vm` | `app`, `container`, `device` |
| `container` | `app` |
| `app` | — |
| `device` | — |

Tout autre cas est une erreur de validation (`doctor`).

### 4.3 Nommage

- Préfixes : `net-`, `host-`, `br-`, `vm-`, `ctr-`, `app-`, `dev-`.
- `id` : `^[a-z0-9][a-z0-9-]{1,62}$`, identique au nom du répertoire.
- Un nœud peut déclarer des `aliases` courts (`model-service`, `services`, `dashboard`), mais
  l'`id` reste la référence canonique.
- L'unicité est globale. Une ambiguïté d'alias est une erreur, jamais un choix
  silencieux.

### 4.4 Résolution d'une cible

Ordre de résolution pour `PATH` :

1. chemin absolu ou relatif existant, puis remontée jusqu'au `node.json` ;
2. `id` exact ;
3. alias exact ;
4. suffixe unique d'`id` (ex. `services` → `vm-services`), sinon erreur `ambiguous`.

`modops path <id>` imprime le chemin canonique. Toutes les commandes acceptent
indifféremment un chemin ou un `id`.

### 4.5 Descripteurs de kind

Chaque kind est décrit une seule fois dans `tree/kinds/<kind>.json` :

| Champ | Rôle |
|---|---|
| `role` | phrase de mission reprise dans `AGENT.md` |
| `provider` | provider par défaut |
| `risk` | risque par défaut |
| `verbs` | verbes autorisés |
| `children` | kinds autorisés comme enfants |

Un `node.json` hérite de ces champs et ne les réécrit que pour surcharger le
descripteur (ex. `app-cli` en `provider: local`). `show`, `status` et les
fiches `AGENT.md` exposent les valeurs effectives ; `doctor` valide la
contenance avec le descripteur.

## 5. Contrat de nœud

### 5.1 `node.json`

Schéma : [`schema/node.schema.json`](../schema/node.schema.json).

| Champ | Type | Obligatoire | Rôle |
|---|---|---|---|
| `schema` | `1` | oui | version du format |
| `id` | string | oui | identifiant unique |
| `kind` | enum | oui | `site`, `network`, `host`, `bridge`, `vm`, `container`, `app`, `device` |
| `title` | string | oui | libellé humain |
| `parent` | string \| null | oui | `id` du parent (validation croisée) |
| `provider` | enum | non | `local`, `qemu`, `ssh`, `docker`, `http`, `none` ; hérité du descripteur |
| `verbs` | string[] | non | verbes autorisés ; hérités du descripteur |
| `risk` | enum | non | `low`, `medium`, `high` ; hérité du descripteur |
| `summary` | string | non | une phrase pour l'index du coordinateur |
| `aliases` | string[] | non | noms courts |
| `addresses` | object[] | non | `{iface?, address, network?, port?, mac?}` |
| `cidr` / `gateway` / `dns` | — | non | réseaux uniquement |
| `runtime` | object | non | détails du provider (pidfile, unité, conteneur… ) |
| `resources` | object | non | vCPU, RAM, disque |
| `management` | object | non | alias SSH, user, clé (référence) |
| `health` | object | non | test de santé |
| `links` | object[] | non | `{to, protocol?, port?, models?, purpose?}` |
| `privileges` | object | non | `{run_as, sudo:[...]}` |
| `facts_from` | string[] | non | références vers `config.sh` |
| `ai` | object | non | `{model, context_budget_tokens, max_steps}` |
| `details` | object | non | champs spécifiques au kind |
| `notes` | string | non | remarque courte |

**Interdit** : clé privée, mot de passe, token, secret. On référence un chemin
ou un nom de variable.

### 5.2 `AGENT.md`

Carte de périmètre chargée par l'opérateur. Format imposé, ≤ 45 lignes :

```markdown
# <titre>
`<id>` · `<kind>` · `<provider>` · risque `<risk>`

## Mission
Une à trois phrases.

## Périmètre
- ce que l'agent peut inspecter ;
- ce qu'il peut modifier.

## Interdits
- actions hors périmètre ;
- ressources d'autres nœuds.

## Verbes
`status`, `health`, … (sous-ensemble de `node.json`)

## Faits
- adresse, port, unité, conteneur, dépendance.

## Escalade
- vers le coordinateur si …
- vers un humain si …
```

Règles :

- pas de duplication des adresses : `node.json` fait foi, la fiche peut les
  rappeler à titre humain ;
- pas d'historique ni de journal : l'état va dans `state/` ;
- si le périmètre dépasse 45 lignes, le nœud est trop gros : le découper.

## 6. API `modops`

### 6.1 Enveloppe JSON

Toute commande accepte `--json` et renvoie :

```json
{
  "ok": true,
  "verb": "status",
  "target": "vm-services",
  "data": {},
  "warnings": [],
  "duration_ms": 12
}
```

Erreur :

```json
{
  "ok": false,
  "verb": "start",
  "target": "vm-services",
  "error": {"code": "not_implemented", "message": "...", "hint": "..."}
}
```

`stdout` ne contient que le résultat ; les diagnostics vont sur `stderr`.

### 6.2 Codes de sortie

| Code | Sens |
|---|---|
| 0 | succès |
| 2 | usage / argument invalide |
| 3 | non implémenté ou non supporté par le provider |
| 4 | cible introuvable ou ambiguë |
| 5 | précondition non remplie |
| 6 | permission refusée |
| 7 | vérification de santé échouée |
| 8 | conflit / verrou |
| 9 | erreur interne |

### 6.3 Verbes de lecture

| Verbe | Cibles | Effet |
|---|---|---|
| `ls [PATH] [--depth N]` | tous | liste les enfants directs |
| `show PATH [--card]` | tous | métadonnées + fiche |
| `path ID` | tous | chemin canonique |
| `status [PATH] [--recursive]` | tous | état observé du provider ; `--recursive` inclut le nœud et ses descendants, avec kind, endpoint, état déclaré, état réel, pid et port |
| `health [PATH]` | tous | test de santé déclaré |
| `logs PATH [-n N]` | vm, container, app | dernières lignes |
| `tree [PATH]` | tous | rendu ASCII annoté |
| `net list` | site, network | réseaux et bridges |
| `doctor [--installer PATH]` | site | validation + dérive |
| `ask TEXT` | site | routage d'intention déterministe (0 token) : lecture, inventaires, `tree`, `status` récursif ; une création renvoie `scaffold_required` (3), une mutation `explicit_verb_required` (5) |
| `state show\|save\|plan` | site | état désiré, observé, plan |

Méta-verbes locaux, non exposés aux opérateurs de nœuds :

| Verbe | Rôle |
|---|---|
| `index` | index compact (id, kind, endpoint, summary) pour le coordinateur |
| `context PATH` | assemble le prompt exact d'un opérateur + estime les tokens |

Verbes d'état local (aucun effet réel, pas de `--yes`) :

| Verbe | Rôle |
|---|---|
| `state set PATH --power running\|stopped` | écrit la puissance désirée dans `desired.json` |
| `state reset [PATH]` | efface le désiré (nœud ou tout) |

### 6.4 Verbes mutants

Tous acceptent `--dry-run`, `--yes`, `--json`. Sans `--yes` en mode non
interactif : refus (`confirmation_required`). En interactif : saisie du `id`
pour les actions `risk: high`.

| Verbe | Cibles | État | Sémantique |
|---|---|---|---|
| `start PATH` | vm, app, container | implémenté | démarre et attend la santé |
| `stop PATH [--force]` | vm, app, container | implémenté | arrête sans supprimer ; `--force` = brutal |
| `restart PATH` | vm, app, container | implémenté | stop puis start |
| `move PATH --to PARENT [--address IP] [--port N]` | app, container, device | implémenté | déplacement déclaratif ; refuse une source allumée |
| `net create ID --cidr …` | host, network | spécifié | crée un bridge + segment |
| `net attach PATH --network NET [--address …]` | vm, container | spécifié | rattache à un réseau |
| `net detach PATH [--network NET]` | vm, container | spécifié | détache |
| `net move PATH --to NET [--address …]` | vm, container | spécifié | migre d'un réseau à l'autre |
| `connect SRC --to DST --port N` | app, vm | spécifié | déclare et vérifie une dépendance |
| `disconnect SRC --to DST` | app, vm | spécifié | retire la dépendance déclarée |
| `new PARENT --spec FILE\|-` | tous | implémenté | crée un nœud (`node.json` + `AGENT.md`) sous le parent |
| `render FILE\|- --out DIR` | site | implémenté | matérialise une arborescence depuis une spécification |
| `restore [--dry-run] [--yes]` | site | implémenté | rejoue le désiré : arrêts, puis bridge, VMs, apps |

`new` ne prend qu'une spécification JSON (`--spec FILE|-`). Le parent peut
être `.` lorsque le répertoire courant est un nœud. La spécification
d'arborescence est du JSON `schema: 1` (`root` + `nodes` imbriqués) ; `new`
attend un objet `node` ou directement les champs du nœud. Les défauts
(`provider`, `verbs`, `risk`, `health`, rôle, contenance) viennent du
descripteur du kind (`tree/kinds/<kind>.json`) et `node.json` ne conserve que
les surcharges. Toute erreur de validation annule les fichiers créés
(`output_busy`, `address_outside`, `containment`, `invalid_tree`, …).

`start` et `stop` ne sont supportés que par les providers `qemu` et `docker` ;
les autres renvoient `unsupported` (code 3). `--dry-run` affiche l'argv exact
qui serait exécuté et ne touche ni au réel ni à l'état.

### 6.5 Sémantique de `net move`

**VM** (downtime requis en v1) :

1. refuser si la VM tourne, sauf `--force` (arrêt propre) ;
2. `ip link set <tap> nomaster` puis attache au bridge cible ;
3. mettre à jour l'adresse invitée (cloud-init/netplan) par SSH ;
4. redémarrer l'interface invitée, vérifier IP + route + DNS ;
5. `modops health` ; rollback vers l'ancien réseau si échec ;
6. déplacer le répertoire du nœud dans la branche cible (`git mv`) et mettre à
   jour `node.json` (`parent`, `addresses[].network`) ;
7. journaliser l'ancien et le nouveau réseau dans l'audit.

**Conteneur** (à chaud possible) : `docker network disconnect` puis `connect`,
vérifier la résolution et le port publié.

**Règle** : l'arborescence et le runtime sont modifiés dans la même opération.
Un échec de vérification déclenche un rollback des deux.

### 6.6 Sémantique de `move`

`move PATH --to PARENT [--address IP] [--port N]` déplace la **déclaration**
d'un `app`, `container` ou `device` : répertoire, `parent`, `addresses`,
`management.ssh_alias`, `runtime.bind`, `health.url`, et régénère `AGENT.md`.

1. refuse la racine, les kinds non déplaçables (`vm` → `net move`), un parent
   incompatible, un nœud avec enfants et une cible existante ;
2. refuse si la source est observée allumée (`source_running`, code 5) ;
3. reprend l'adresse IP du parent cible, ou `--address` (qui doit appartenir
   au réseau), en conservant le port ou en appliquant `--port` ;
4. refuse un `IP:port` déjà utilisé (`address_conflict`, code 8) ;
5. valide l'arborescence obtenue et **annule tout** si elle devient incohérente ;
6. journalise l'ancien et le nouveau parent dans l'audit.

Ce n'est pas une migration : conteneur, volume et données restent sur l'hôte
source. `move` réécrit la cible du runtime ; il faut copier la configuration et
les données, recréer le conteneur sur la cible (`docker compose up -d`) puis
vérifier `health`. Un `mv` manuel n'est pas supporté : il
laisse `parent`, adresse et `ssh_alias` périmés (`doctor` avertit via
`app_address_outside_parent` et `management_alias_mismatch`).

### 6.7 Sémantique de `connect`

`connect` ne crée pas de magie réseau. Il :

1. vérifie que `DST` expose `port` et que `SRC` peut l'atteindre (TCP/HTTP) ;
2. ajoute un lien déclaratif dans `node.json` de `SRC` ;
3. si les nœuds sont sur des réseaux différents et qu'aucune route n'existe,
   échoue avec `unreachable` et propose `net attach` ou un tunnel ;
4. est idempotent : rejouer ne duplique pas le lien.

### 6.8 Puissance, état et restauration

**VM (`provider: qemu`)**

- `start` exécute `runtime.start` (script de l'installeur) via `sudo -n`, puis
  attend l'état `running`. Le script attend SSH ; la santé applicative est
  vérifiée séparément.
- `stop` exécute `runtime.stop` : arrêt invité propre, timeout 220 s.
- `stop --force` : lit `runtime.pid_file`, envoie SIGTERM, attend 8 s, puis
  SIGKILL, et supprime pidfile + socket QMP. **Les disques et volumes ne sont
  pas touchés.** C'est l'arrêt brutal accepté.
- Sans règle `sudo -n`, l'action échoue en `permission_denied` (code 6) avant
  toute modification.

**Application Docker (`provider: docker`)**

- `start` = `docker start <conteneur>` ;
- `stop` = `docker stop --time 15 <conteneur>` ;
- `stop --force` = `docker kill <conteneur>` ;
- exécution par `ssh -o BatchMode=yes <alias>`, argv fermé.

**État**

```text
state/desired.json   {"nodes": {"vm-services": {"power": "running"}}}
state/observed.json  {"nodes": {"vm-services": {"state": "stopped", ...}}}
state/audit.jsonl    une ligne JSON par mutation
```

- `state set` ne modifie **pas** le runtime : il déclare une intention.
- `start`, `stop` et `restart` réussis mettent à jour `desired.json` : l'état
  persiste automatiquement.
- `state save` observe et écrit `observed.json`.
- `restore` calcule le plan, arrête les applications avant les VMs, démarre
  bridge puis VMs puis applications, et réécrit `observed.json`.
- Un échec n'interrompt pas la restauration : il est collecté, audité et
  provoque le code 5.

| Code | Situation |
|---|---|
| 5 | `confirmation_required`, `command_failed` |
| 6 | `permission_denied` (sudo sans mot de passe absent) |
| 8 | `timeout` |
| 3 | `unsupported` |

## 7. Providers
| Provider | Observer | Démarrer/arrêter | Détails |
|---|---|---|---|
| `local` | `ip`, `nft`, `lspci`, `/proc` | bridge, NAT | commandes fixes, pas de shell modèle |
| `qemu` | pidfile + QMP `query-status` | QEMU via script/args | pidfile, socket QMP |
| `ssh` | `systemctl is-active` | unité systemd | alias SSH, `BatchMode=yes` |
| `docker` | `docker inspect`, santé HTTP | `docker start/stop` | via SSH vers la VM |
| `http` | GET avec timeout | — | statut < 400 = up |

Chaque provider expose les verbes qu'il sait faire ; sinon `unsupported` (code 3).
Les commandes sont construites en `argv`, jamais par concaténation de chaîne
issue du modèle.

Pour Docker, `runtime_state` est distinct de `health` : il interroge d'abord
l'API, puis `docker inspect`. Cela donne `running` (API OK), `degraded`
(conteneur vivant, API muette), `stopped` (`exited`, `dead`, conteneur absent)
ou `unreachable` (SSH en échec). Les décisions de puissance utilisent
`runtime_state` ; `health` reste le test applicatif explicite.

## 8. Agents

### 8.1 Coordinateur

Entrées : demande utilisateur, index compact (`id`, `kind`, `summary`), budget.
Sorties : un plan de ≤ 6 étapes, chaque étape ciblant **un** nœud.

Interdictions :

- exécuter un verbe mutant lui-même ;
- lire les `AGENT.md` de tous les nœuds ;
- inventer un verbe ou un argument hors schéma ;
- confirmer une action `risk: high` à la place de l'humain.

### 8.2 Opérateur de nœud

Contexte chargé :

- `AGENT.md` du nœud (≤ 45 lignes) ;
- `node.json` du nœud ;
- liste des enfants directs (`id`, `kind`, `summary`) ;
- schéma des verbes autorisés ;
- dernière observation (≤ 500 caractères).

Sortie, en JSON strict :

```json
{"verb": "status", "args": {}, "reason": "court"}
{"final": "réponse", "evidence": ["verb:target"]}
{"escalate": "coordinator|human", "reason": "court"}
```

Une étape = un verbe. Pas de prose, pas de bloc de code, pas de shell.

### 8.3 Boucle et budgets

```text
demande
  ├─ routeur déterministe (regex/alias) ──► verbe direct       0 token
  └─ coordinateur ─► opérateur ─► verbe ─► observation
                     └─ max 2 appels LLM par étape
```

| Budget | Valeur par défaut |
|---|---|
| étapes | 6 |
| appels LLM | 4 |
| contexte par appel | 1 800 tokens |
| observation | 500 caractères |
| sortie modèle | 256 tokens |
| température | 0,1 |
| modèle | `example-small-model` |
| escalade | `example-large-model` uniquement sur `--deep` ou échec |

Optimisations obligatoires :

- index en cache dans `state/cache/index.json` ;
- fiches statiques, jamais réinjectées en entier dans l'historique ;
- résultats structurés, logs tronqués à la demande ;
- pas d'historique conversationnel au-delà de la dernière observation ;
- routeur déterministe avant tout appel modèle.

### 8.4 Escalade

| Situation | Destination |
|---|---|
| nœud hors périmètre | coordinateur |
| verbe non autorisé | coordinateur |
| action `risk: high` | humain |
| santé inchangée après 2 tentatives | humain |
| divergence tree/réel | coordinateur + `doctor` |

### 8.5 Scaffolder

Rôle dédié à la création : il reçoit le parent (`AGENT.md` + `node.json`), ses
enfants directs, le CIDR du réseau parent et la demande. Il ne voit ni l'index
global ni les autres branches.

Sortie, en JSON strict :

```json
{"verb":"new","args":{"parent":"net-lab","spec":{"id":"vm-x","kind":"vm","address":"192.0.2.14/24"}}}
{"verb":"render","args":{"out":"/tmp/infra","spec":{"schema":1,"root":{},"nodes":[]}}}
{"question":"Quelle adresse pour la VM ?"}
{"escalate":"human","reason":"suppression demandée"}
```

Règles :

- ne jamais déclarer `provider`, `verbs`, `risk` : ils viennent du `kind` ;
- `address` dans le CIDR du parent, `kind` autorisé par la contenance ;
- `runtime` seulement si la demande décrit une instanciation ;
- dry-run puis `--yes` ; `doctor` et `status` après écriture ;
- un nœud sans `runtime` reste déclaratif (`status: unknown`).

## 9. État, dérive, audit

| Chemin | Contenu | Versionné |
|---|---|---|
| `tree/` | topologie et documentation (état désiré structurel) | oui |
| `state/desired.json` | puissance désirée par nœud | non |
| `state/observed.json` | dernière observation | non |
| `state/cache/` | index et fiches compilées (prévu) | non |
| `state/locks/` | verrous par nœud (prévu) | non |
| `state/views/` | vues réseau générées (prévu v1) | non |
| `state/audit.jsonl` | journal append-only | non |

`IACRUN_STATE_DIR` déplace tout `state/` (tests, CI).

`modops doctor` vérifie : ids uniques, parents cohérents, kinds autorisés,
fichiers présents, verbes connus, liens valides, absence de secrets évidents.
Deux avertissements détectent un déplacement manuel incohérent : une app dont
l'adresse n'est pas celle de sa VM parente (`app_address_outside_parent`) ou
dont l'`ssh_alias` diffère de celui du parent (`management_alias_mismatch`).
`doctor` peut comparer les faits déclarés à une source de déploiement fournie
explicitement par l'opérateur.

`modops state plan` compare la puissance désirée à l'état observé. Le diff
structurel complet (`modops diff`, adresses, liens, ressources) reste à
implémenter.

## 10. Sécurité

- `modops` tourne avec un compte dédié non privilégié, **jamais en root**.
- Les opérations privilégiées passent par une allowlist `sudo -n` exacte
  (voir [`security.md` §2.1](security.md#21-allowlist-sudoers-minimale)) ; le
  helper root `iacrund` reste une cible, pas encore nécessaire.
- Aucun verbe n'accepte une commande libre.
- `risk: high` : confirmation humaine + audit + vérification post-action.
- Le LLM ne reçoit ni secret, ni environnement, ni log complet par défaut.
- Les endpoints configurés peuvent être sans authentification ; ne les
  exposez qu'aux réseaux de confiance.
- Toute action est tracée : acteur, verbe, cible, args, résultat, durée.

## 11. Extensions

- Nouveau kind : ajouter au schéma, à la matrice de contenance, au renderer.
- Nouveau provider : implémenter `status/health/start/stop`, déclarer la
  matrice de capacités, ajouter un test.
- Nouveau verbe : entrée dans le catalogue, validation d'args, documentation,
  test, mise à jour du schéma d'outils LLM.

Toute extension qui augmente le contexte d'un opérateur doit être justifiée :
le budget de 1 800 tokens est une contrainte de conception, pas un réglage.
