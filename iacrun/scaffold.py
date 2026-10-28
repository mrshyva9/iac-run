"""Génération de nœuds depuis une spécification descriptive.

Deux entrées, un même format de nœud :

- `create_node()` : ajoute **un** nœud sous un parent existant (cas
  « je suis dans `net-example`, crée une VM ») ;
- `render_spec()` : matérialise une arborescence complète depuis un fichier
  de description (cas « reconstruis une infra depuis un dépôt descriptif »).

La spécification est du JSON, comme `node.json` : aucun YAML, aucune
dépendance. Les valeurs par défaut (provider, verbes, risque, santé) sont
dérivées du `kind` pour qu'un agent n'ait pas à les inventer.

Toute écriture est validée par `Tree.validate()` ; en cas d'erreur, les
fichiers créés sont retirés (rollback), de sorte qu'un échec ne laisse jamais
une arborescence à moitié générée.
"""

from __future__ import annotations

import ipaddress
import json
import re
import shutil
from pathlib import Path
from typing import Any

from .model import ALLOWED_CHILDREN, KINDS, children_kinds, is_uuid, load_descriptors, new_uuid
from .tree import Tree, TreeError

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

SITE_VERBS = ["ls", "show", "path", "status", "health", "tree", "doctor", "ask", "net.list"]
NETWORK_VERBS = ["ls", "show", "path", "status", "health", "net.list"]
BRIDGE_VERBS = ["ls", "show", "path", "status", "health", "start", "stop"]
HOST_VERBS = ["ls", "show", "path", "status", "health", "logs"]
VM_VERBS = ["ls", "show", "path", "status", "health", "logs", "start", "stop", "restart"]
CONTAINER_VERBS = ["ls", "show", "path", "status", "health", "logs", "start", "stop", "restart"]
APP_VERBS = ["ls", "show", "path", "status", "health", "logs", "start", "stop", "restart", "connect", "disconnect"]
DEVICE_VERBS = ["ls", "show", "path", "status", "health"]

KIND_DEFAULTS: dict[str, dict[str, Any]] = {
    "site": {"provider": "local", "verbs": SITE_VERBS, "risk": "low"},
    "network": {"provider": "local", "verbs": NETWORK_VERBS, "risk": "low"},
    "bridge": {"provider": "local", "verbs": BRIDGE_VERBS, "risk": "low"},
    "host": {"provider": "ssh", "verbs": HOST_VERBS, "risk": "medium"},
    "vm": {"provider": "qemu", "verbs": VM_VERBS, "risk": "medium"},
    "container": {"provider": "docker", "verbs": CONTAINER_VERBS, "risk": "medium"},
    "app": {"provider": "docker", "verbs": APP_VERBS, "risk": "medium"},
    "device": {"provider": "none", "verbs": DEVICE_VERBS, "risk": "low"},
}

RESOURCE_KEYS = {"vcpus", "ram_mib", "disk_gib"}
HEALTH_TYPES = {"http", "command", "ssh", "docker", "qemu"}
HEALTH_KEYS = {"type", "url", "argv", "ssh_alias", "container", "timeout_s", "expect"}

PASSTHROUGH = (
    "runtime",
    "resources",
    "management",
    "health",
    "links",
    "privileges",
    "facts_from",
    "ai",
    "details",
    "notes",
    "dns",
    "gateway",
    "cidr",
    "mac",
    "aliases",
)

KIND_ROLE = {
    "network": "segment réseau : les enfants sont ses membres directs.",
    "bridge": "pont local qui porte la passerelle du segment.",
    "host": "machine physique ; ses enfants sont des ressources hébergées.",
    "vm": "machine virtuelle ; ses enfants sont des applications ou devices.",
    "container": "conteneur ; ses enfants sont des applications.",
    "app": "service applicatif ; il ne contient pas d'enfant.",
    "device": "périphérique physique exposé à un nœud parent.",
    "site": "racine de l'arborescence ; elle ne contient que des réseaux.",
}


class SpecError(Exception):
    """Spécification invalide : rien n'a été écrit."""

    def __init__(self, code: str, message: str, hint: str | None = None, exit_code: int = 4):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.exit_code = exit_code


# --------------------------------------------------------------------------
# Construction du nœud
# --------------------------------------------------------------------------


def _default_health(kind: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if kind == "vm":
        return {"type": "qemu"}
    if kind == "bridge":
        iface = ""
        addresses = payload.get("addresses") or []
        if addresses:
            iface = str(addresses[0].get("iface") or "")
        if iface:
            return {"type": "command", "argv": ["ip", "link", "show", iface]}
        return None
    if kind == "host":
        alias = str((payload.get("management") or {}).get("ssh_alias") or payload["id"])
        return {"type": "ssh", "ssh_alias": alias}
    if kind == "container":
        alias = str((payload.get("management") or {}).get("ssh_alias") or "")
        container = str((payload.get("runtime") or {}).get("container") or payload["id"])
        if alias:
            return {"type": "docker", "container": container}
        return None
    return None


def _as_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise SpecError("bad_int", f"{field} doit être un entier : {value!r}.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SpecError("bad_int", f"{field} doit être un entier : {value!r}.") from exc


def _addresses(spec: dict[str, Any], network: str | None) -> list[dict[str, Any]]:
    raw = spec.get("addresses")
    values: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                values.append(dict(item))
            elif isinstance(item, str):
                values.append({"address": item})
    single = spec.get("address")
    if single:
        values.append({"address": str(single)})
    for entry in values:
        entry["address"] = str(entry["address"])
        if "network" not in entry and network:
            entry["network"] = network
        if "port" in entry and entry["port"] is not None:
            entry["port"] = _as_int(entry["port"], f"port de {entry['address']}")
    return values


def build_payload(
    spec: dict[str, Any],
    parent_id: str | None,
    parent_kind: str | None = None,
    network: str | None = None,
    parent_address: str | None = None,
    descriptors: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Transforme une entrée de spécification en `node.json` complet."""
    node_id = str(spec.get("id") or "").strip()
    kind = str(spec.get("kind") or "").strip()
    if not node_id or not ID_RE.match(node_id):
        raise SpecError("invalid_id", f"id invalide : {node_id!r}.")
    if kind not in KINDS:
        raise SpecError("invalid_kind", f"kind inconnu : {kind!r}.")
    if kind in ("site", "network", "bridge", "vm", "container", "app", "device") and parent_id:
        allowed = children_kinds(parent_kind or "", descriptors)
        if kind not in allowed:
            raise SpecError(
                "containment",
                f"{kind} ne peut pas être enfant de {parent_kind}.",
                hint=f"Enfants autorisés : {', '.join(sorted(allowed)) or 'aucun'}.",
            )

    descriptor = (descriptors or {}).get(kind) or {}
    defaults = KIND_DEFAULTS[kind]
    provider = str(spec.get("provider") or descriptor.get("provider") or defaults["provider"])
    verbs = list(spec.get("verbs") or descriptor.get("verbs") or defaults["verbs"])
    risk = str(spec.get("risk") or descriptor.get("risk") or defaults["risk"])
    payload: dict[str, Any] = {
        "schema": 1,
        "uuid": str(spec.get("uuid") or new_uuid()),
        "id": node_id,
        "kind": kind,
        "title": str(spec.get("title") or node_id),
        "parent": parent_id,
    }
    if "uuid" in spec and not is_uuid(payload["uuid"]):
        raise SpecError("bad_uuid", f"uuid invalide : {spec['uuid']!r}.")
    # Un kind décrit par un descripteur n'écrit que ses surcharges : le
    # node.json hérite du reste. Sans descripteur, on garde les défauts code.
    if descriptor:
        if "provider" in spec:
            payload["provider"] = provider
        if "verbs" in spec:
            payload["verbs"] = verbs
        if "risk" in spec:
            payload["risk"] = risk
    else:
        payload.update(provider=provider, verbs=verbs, risk=risk)
    if spec.get("summary"):
        payload["summary"] = str(spec["summary"])

    addresses = _addresses(spec, network)
    if not addresses and spec.get("port") is not None:
        if not parent_address:
            raise SpecError("missing_address", "Un port exige une adresse (ou un parent adressé).")
        inherited: dict[str, Any] = {"address": parent_address}
        if network:
            inherited["network"] = network
        addresses = [inherited]
    if addresses:
        payload["addresses"] = addresses

    for key in PASSTHROUGH:
        if key in spec and key not in ("dns", "gateway", "cidr", "mac", "aliases"):
            payload[key] = spec[key]
    if kind == "network":
        for key in ("cidr", "gateway", "dns"):
            if key in spec:
                payload[key] = spec[key]
        payload.setdefault("verbs", NETWORK_VERBS)
    if "mac" in spec and addresses:
        addresses[0]["mac"] = str(spec["mac"])
    if "iface" in spec and addresses:
        addresses[0]["iface"] = str(spec["iface"])
    if spec.get("port") is not None and addresses:
        addresses[0]["port"] = _as_int(spec["port"], "port")
    if spec.get("aliases"):
        payload["aliases"] = [str(item) for item in spec["aliases"]]

    resources = payload.get("resources")
    if resources is not None:
        if not isinstance(resources, dict):
            raise SpecError("bad_resources", "resources doit être un objet JSON.")
        unknown = sorted(set(resources) - RESOURCE_KEYS)
        if unknown:
            raise SpecError(
                "bad_resources",
                f"resources : clés inconnues {', '.join(unknown)}.",
                hint=f"Clés admises : {', '.join(sorted(RESOURCE_KEYS))}.",
            )
        for key in RESOURCE_KEYS:
            if key in resources:
                resources[key] = _as_int(resources[key], f"resources.{key}")
                if resources[key] <= 0:
                    raise SpecError("bad_resources", f"resources.{key} doit être positif.")

    runtime = payload.get("runtime")
    if runtime is not None and not isinstance(runtime, dict):
        raise SpecError("bad_runtime", "runtime doit être un objet JSON.")

    if "health" not in payload:
        default = _default_health(kind, payload)
        if default:
            payload["health"] = default
    if "health" in payload:
        health = payload["health"]
        if not isinstance(health, dict):
            raise SpecError("bad_health", "health doit être un objet JSON.")
        unknown = sorted(set(health) - HEALTH_KEYS)
        if unknown:
            raise SpecError(
                "bad_health",
                f"health : clés inconnues {', '.join(unknown)}.",
                hint=f"Clés admises : {', '.join(sorted(HEALTH_KEYS))}.",
            )
        if health.get("type") not in HEALTH_TYPES:
            raise SpecError(
                "bad_health",
                f"health.type inconnu : {health.get('type')!r}.",
                hint=f"Types admis : {', '.join(sorted(HEALTH_TYPES))}.",
            )

    links = payload.get("links")
    if links is not None:
        if not isinstance(links, list):
            raise SpecError("bad_links", "links doit être une liste.")
        for item in links:
            if not isinstance(item, dict) or not item.get("to"):
                raise SpecError("bad_links", "Chaque lien doit être un objet avec un champ \"to\".")
            if not is_uuid(item["to"]):
                raise SpecError("bad_links", f"links[].to doit être un UUID : {item['to']!r}.")
            if "label" in item and not isinstance(item["label"], str):
                raise SpecError("bad_links", "links[].label doit être une chaîne.")

    if kind == "network":
        cidr = payload.get("cidr")
        if not cidr:
            raise SpecError("missing_cidr", f"Le réseau {node_id} doit déclarer un cidr.")
        try:
            network = ipaddress.ip_network(str(cidr), strict=False)
        except ValueError as exc:
            raise SpecError("bad_cidr", f"cidr invalide : {cidr!r} ({exc}).") from exc
        gateway = payload.get("gateway")
        if gateway:
            try:
                gateway_ip = ipaddress.ip_address(str(gateway))
            except ValueError as exc:
                raise SpecError("bad_gateway", f"passerelle invalide : {gateway!r}.") from exc
            if gateway_ip not in network:
                raise SpecError("bad_gateway", f"Passerelle {gateway} hors de {cidr}.")
        payload["cidr"] = str(network)
        payload["gateway"] = str(gateway or next(network.hosts()))
    return payload


def _check_address_in_parent(payload: dict[str, Any], network_cidr: str | None) -> None:
    if not network_cidr:
        return
    network = ipaddress.ip_network(network_cidr, strict=False)
    for entry in payload.get("addresses", []):
        value = str(entry.get("address", ""))
        try:
            iface = (
                ipaddress.ip_interface(value)
                if "/" in value
                else ipaddress.ip_interface(f"{value}/{network.prefixlen}")
            )
        except ValueError as exc:
            raise SpecError("bad_address", f"adresse invalide : {value!r} ({exc}).") from exc
        if iface.ip not in network:
            raise SpecError(
                "address_outside",
                f"{iface.ip} n'appartient pas à {network}.",
                hint="Vérifier l'adresse ou le réseau parent.",
            )


# --------------------------------------------------------------------------
# Fiche AGENT.md
# --------------------------------------------------------------------------


def card_text(
    payload: dict[str, Any],
    descriptor: dict[str, Any] | None = None,
) -> str:
    node_id = payload["id"]
    kind = payload["kind"]
    title = str(payload.get("title") or node_id)
    provider = payload.get("provider", "none")
    risk = payload.get("risk", "low")
    parent = payload.get("parent") or "aucun (racine)"
    descriptor = descriptor or {}
    role = str(descriptor.get("role") or KIND_ROLE[kind])
    lines = [
        f"# {title}",
        "",
        f"`{node_id}` · `{kind}` · `{provider}` · risque `{risk}`",
        "",
        "## Mission",
        str(payload.get("summary") or role),
        "",
        "## Périmètre",
        f"- Nœud `{node_id}` sous `{parent}`.",
        f"- {role}",
    ]
    allowed = (
        set(descriptor["children"])
        if "children" in descriptor
        else ALLOWED_CHILDREN.get(kind, set())
    )
    if allowed:
        lines.append("- Enfants autorisés : " + ", ".join(f"`{item}`" for item in sorted(allowed)) + ".")
    else:
        lines.append("- Aucun enfant : ce nœud est une feuille.")

    lines += [
        "",
        "## Interdits",
        "- Sortir du périmètre de ce nœud.",
        "- Modifier un nœud dont tu n'es pas l'opérateur.",
        "- Inventer une adresse, un port ou une ressource absents de `node.json`.",
        "",
        "## Verbes",
        ", ".join(f"`{verb}`" for verb in (payload.get("verbs") or descriptor.get("verbs") or [])),
    ]

    facts: list[str] = []
    addresses = payload.get("addresses") or []
    if addresses:
        shown = ", ".join(
            f"`{item['address']}:{item['port']}`" if item.get("port") else f"`{item['address']}`"
            for item in addresses
        )
        facts.append(f"Adresse : {shown}.")
    if payload.get("cidr"):
        facts.append(f"CIDR `{payload['cidr']}`, passerelle `{payload.get('gateway')}`.")
    resources = payload.get("resources") or {}
    if resources:
        parts = [
            f"{resources[key]} {'vCPU' if key == 'vcpus' else 'Mio' if key == 'ram_mib' else 'Gio'}"
            for key in ("vcpus", "ram_mib", "disk_gib")
            if key in resources
        ]
        if parts:
            facts.append(", ".join(parts) + ".")
    runtime = payload.get("runtime") or {}
    for key, label in (
        ("start", "démarrage"),
        ("stop", "arrêt"),
        ("pid_file", "pidfile"),
        ("tap", "tap"),
        ("container", "conteneur"),
        ("image", "image"),
    ):
        if runtime.get(key):
            facts.append(f"{label} : `{runtime[key]}`.")
    health = payload.get("health") or {}
    if health.get("type"):
        detail = health.get("url") or health.get("ssh_alias") or health.get("container") or ""
        facts.append(f"santé : `{health['type']}`" + (f" ({detail})" if detail else "") + ".")
    for link in payload.get("links") or []:
        target = link.get("label") or link.get("to")
        extra = []
        if link.get("protocol"):
            extra.append(str(link["protocol"]))
        if link.get("port"):
            extra.append(str(link["port"]))
        models = f", modèles {', '.join(link['models'])}" if link.get("models") else ""
        suffix = f" ({':'.join(extra)})" if extra else ""
        facts.append(f"lien : `{target}`{suffix}{models}.")
    if facts:
        lines += ["", "## Faits", *[f"- {fact}" for fact in facts]]

    lines += [
        "",
        "## Escalade",
        "- Coordinateur si la demande dépasse ce nœud.",
        "- Humain si l'action est destructive ou si la santé ne revient pas.",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# Écriture et validation
# --------------------------------------------------------------------------


def _issue_keys(issues: list[dict[str, str]]) -> set[tuple[str, str, str]]:
    return {
        (issue.get("level", ""), issue.get("code", ""), issue.get("message", ""))
        for issue in issues
    }


def _effective_payload(
    payload: dict[str, Any],
    descriptor: dict[str, Any] | None,
) -> dict[str, Any]:
    data = dict(payload)
    for key in ("provider", "verbs", "risk"):
        if key not in data and descriptor and key in descriptor:
            data[key] = descriptor[key]
    return data


def _write_node(
    directory: Path,
    payload: dict[str, Any],
    dry_run: bool,
    descriptor: dict[str, Any] | None = None,
) -> list[str]:
    if dry_run:
        return [str(directory / "node.json"), str(directory / "AGENT.md")]
    directory.mkdir(parents=True, exist_ok=False)
    node_file = directory / "node.json"
    node_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    card = card_text(_effective_payload(payload, descriptor), descriptor)
    (directory / "AGENT.md").write_text(card, encoding="utf-8")
    return [str(node_file), str(directory / "AGENT.md")]


def _fail_on_new_errors(
    root: Path,
    before: set[tuple[str, str, str]],
    created: list[Path],
) -> None:
    issues = Tree.load(root).validate()
    new_errors = [
        issue
        for issue in issues
        if issue["level"] == "error" and _issue_keys([issue]) - before
    ]
    if not new_errors:
        return
    for path in reversed(created):
        shutil.rmtree(path, ignore_errors=True)
    detail = "; ".join(
        f"{issue['code']}: {issue['message'].rstrip('.')}" for issue in new_errors
    )
    raise SpecError("invalid_tree", f"Le nœud généré rend l'arborescence invalide : {detail}.")


def create_node(
    tree: Tree,
    parent: str,
    spec: dict[str, Any],
    dry_run: bool = False,
) -> dict[str, Any]:
    """Ajoute un nœud sous `parent` et valide l'arborescence complète."""
    parent_node = tree.resolve(parent)
    network_node = parent_node
    while network_node is not None and network_node.kind != "network":
        network_node = tree.parent_of(network_node)
    network_id = network_node.id if network_node is not None else None
    kind = str(spec.get("kind") or "")
    parent_address = None
    if parent_node.addresses:
        parent_address = str(parent_node.addresses[0].get("address", "")).split("/")[0] or None
    payload = build_payload(
        spec,
        parent_node.id,
        parent_node.kind,
        None if kind == "network" else network_id,
        parent_address,
        tree.descriptors,
    )
    descriptor = tree.descriptors.get(kind) or {}
    directory = parent_node.path / payload["id"]
    if directory.exists():
        raise SpecError(
            "exists",
            f"{directory} existe déjà.",
            hint="Choisir un autre id ou supprimer le nœud explicitement.",
        )
    network_cidr = str(network_node.raw.get("cidr") or "") if network_node is not None else None
    if kind != "network":
        _check_address_in_parent(payload, network_cidr)

    before = _issue_keys(tree.validate())
    files = _write_node(directory, payload, dry_run, descriptor)
    if dry_run:
        return {"node": _effective_payload(payload, descriptor), "files": files, "dry_run": True}
    _fail_on_new_errors(tree.root, before, [directory])
    return {
        "node": _effective_payload(payload, descriptor),
        "files": files,
        "dry_run": False,
        "path": str(directory),
    }


def _flatten(nodes: list[dict[str, Any]], parent: str) -> list[tuple[dict[str, Any], str]]:
    flat: list[tuple[dict[str, Any], str]] = []
    for raw in nodes:
        item = dict(raw)
        children = item.pop("children", []) or []
        flat.append((item, parent))
        flat.extend(_flatten(children, str(item.get("id"))))
    return flat


def render_spec(spec: dict[str, Any], out: Path, dry_run: bool = False) -> dict[str, Any]:
    """Matérialise une arborescence complète dans `out` (répertoire vide)."""
    if int(spec.get("schema", 0)) != 1:
        raise SpecError("spec_schema", "La spécification doit déclarer \"schema\": 1.")
    root_spec = spec.get("root") or {}
    root_id = str(root_spec.get("id") or "home")
    if not ID_RE.match(root_id):
        raise SpecError("invalid_id", f"id de racine invalide : {root_id!r}.")
    try:
        descriptors = load_descriptors(out / root_id)
    except ValueError as exc:
        raise SpecError("invalid_descriptor", str(exc), exit_code=2) from exc
    nodes = spec.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise SpecError("missing_nodes", "La spécification doit contenir au moins un nœud.")

    out = out.expanduser()
    if out.exists():
        if not out.is_dir():
            raise SpecError(
                "output_busy", f"{out} existe et n'est pas un répertoire.",
                hint="Choisir un répertoire neuf.",
            )
        if any(out.iterdir()):
            raise SpecError("output_busy", f"{out} n'est pas vide.", hint="Choisir un répertoire neuf.")

    payloads: list[tuple[Path, dict[str, Any]]] = []
    root_payload = {
        "schema": 1,
        "uuid": str(root_spec.get("uuid") or new_uuid()),
        "id": root_id,
        "kind": "site",
        "title": str(root_spec.get("title") or root_id),
        "parent": None,
        "provider": "local",
        "verbs": SITE_VERBS,
        "risk": "low",
    }
    if "uuid" in root_spec and not is_uuid(root_payload["uuid"]):
        raise SpecError("bad_uuid", f"uuid de racine invalide : {root_spec['uuid']!r}.")
    if root_spec.get("summary"):
        root_payload["summary"] = str(root_spec["summary"])
    payloads.append((out / root_id, root_payload))

    seen: dict[str, str] = {root_id: "site"}
    networks: dict[str, str | None] = {root_id: None}
    cidrs: dict[str, str] = {}
    flat = _flatten(nodes, root_id)
    for item, parent_id in flat:
        node_id = str(item.get("id") or "")
        if node_id in seen:
            raise SpecError("duplicate_id", f"id en double dans la spécification : {node_id!r}.")
        if parent_id not in seen:
            raise SpecError("unknown_parent", f"parent inconnu : {parent_id!r}.")
        parent_dir = next(
            directory
            for directory, payload in payloads
            if payload["id"] == parent_id
        )
        parent_payload = next(
            payload
            for _, payload in payloads
            if payload["id"] == parent_id
        )
        parent_address = None
        if parent_payload.get("addresses"):
            parent_address = str(parent_payload["addresses"][0].get("address", "")).split("/")[0] or None
        network_id = networks[parent_id]
        kind = str(item.get("kind"))
        if kind == "network":
            network_id = node_id
        payload = build_payload(
            item,
            parent_id,
            seen[parent_id],
            None if kind == "network" else network_id,
            parent_address,
            descriptors,
        )
        _check_address_in_parent(payload, cidrs.get(network_id or ""))
        payloads.append((parent_dir / node_id, payload))
        seen[node_id] = payload["kind"]
        networks[node_id] = network_id
        if payload.get("cidr"):
            cidrs[node_id] = str(payload["cidr"])

    if dry_run:
        return {
            "root": str(out / root_id),
            "nodes": [payload["id"] for _, payload in payloads],
            "files": [str(directory / name) for directory, _ in payloads for name in ("node.json", "AGENT.md")],
            "dry_run": True,
        }

    created_root = False
    if not out.exists():
        out.mkdir(parents=True)
        created_root = True
    try:
        for directory, payload in payloads:
            descriptor = descriptors.get(str(payload["kind"])) or {}
            _write_node(directory, payload, dry_run=False, descriptor=descriptor)
        issues = Tree.load(out / root_id).validate()
        errors = [issue for issue in issues if issue["level"] == "error"]
        if errors:
            detail = "; ".join(
                f"{issue['code']}: {issue['message'].rstrip('.')}" for issue in errors
            )
            raise SpecError("invalid_tree", f"Arborescence générée invalide : {detail}.")
    except Exception:
        shutil.rmtree(out / root_id, ignore_errors=True)
        if created_root:
            shutil.rmtree(out, ignore_errors=True)
        raise
    return {
        "root": str(out / root_id),
        "nodes": [payload["id"] for _, payload in payloads],
        "warnings": len([issue for issue in issues if issue["level"] == "warning"]),
        "dry_run": False,
    }
