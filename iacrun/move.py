"""Déplacement déclaratif d'un nœud dans l'arborescence.

`move` change l'emplacement du répertoire et réécrit les faits qui en
dépendent : `parent`, `addresses`, `management.ssh_alias`, `runtime.bind` et
`health.url`. Il ne migre ni conteneur, ni volume, ni données : le runtime réel
reste à faire, et le verbe refuse de déplacer une app en cours d'exécution.
"""

from __future__ import annotations

import ipaddress
import json
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from . import providers, scaffold
from .model import ALLOWED_CHILDREN, Node
from .tree import Tree

# Types déplaçables par ce verbe. Les VM relèvent de `net move` (v1).
MOVABLE = ("app", "container", "device")


class MoveError(Exception):
    """Déplacement refusé : rien n'a été écrit."""

    def __init__(self, code: str, message: str, hint: str | None = None, exit_code: int = 4):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.exit_code = exit_code


def _first_address(node: Node) -> dict[str, Any] | None:
    for entry in node.addresses:
        if entry.get("address"):
            return entry
    return None


def _entry_ip(entry: dict[str, Any]) -> str | None:
    value = str(entry.get("address") or "")
    if not value:
        return None
    try:
        return str(ipaddress.ip_interface(value).ip)
    except ValueError:
        return None


def _parent_ip(parent: Node) -> str | None:
    values = sorted(parent.ips)
    return values[0] if values else None


def _network_of(node: Node) -> str | None:
    entry = _first_address(node)
    if entry and entry.get("network"):
        return str(entry["network"])
    return None


def _network_cidr(tree: Tree, network_id: str | None) -> str | None:
    if not network_id:
        return None
    node = tree.nodes.get(network_id)
    if node is None or node.kind != "network":
        return None
    cidr = node.raw.get("cidr")
    return str(cidr) if cidr else None


def _rewrite_bind(value: str, ip: str, port: int | None) -> str:
    left, sep, rest = value.partition("->")
    if not sep:
        return value
    host, colon, old_port = left.rpartition(":")
    if not colon:
        old_port = ""
    new_port = str(port) if port is not None else old_port
    return f"{ip}:{new_port}{sep}{rest}" if new_port else f"{ip}{sep}{rest}"


def _rewrite_url(value: str, ip: str, port: int | None) -> str:
    parts = urlsplit(value)
    if not parts.hostname:
        return value
    host = f"[{ip}]" if ":" in ip else ip
    new_port = port if port is not None else parts.port
    netloc = f"{host}:{new_port}" if new_port else host
    if parts.username:
        credentials = parts.username
        if parts.password:
            credentials += f":{parts.password}"
        netloc = f"{credentials}@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _check_target(tree: Tree, node: Node, parent: Node) -> Path:
    if node.path == tree.root:
        raise MoveError("cannot_move_root", "La racine ne peut pas être déplacée.")
    if node.kind not in MOVABLE:
        raise MoveError(
            "unsupported_kind",
            f"Le déplacement d'un nœud {node.kind} n'est pas supporté.",
            hint="Les VM relèvent de `net move` (v1, docs/specification.md §6.5).",
            exit_code=3,
        )
    if node.parent == parent.id:
        raise MoveError("same_parent", f"{node.id} est déjà sous {parent.id}.", exit_code=2)
    if node.kind not in ALLOWED_CHILDREN.get(parent.kind, set()):
        raise MoveError(
            "containment",
            f"{node.kind} ne peut pas être enfant de {parent.kind}.",
            hint=f"Enfants autorisés de {parent.kind} : "
            + (", ".join(sorted(ALLOWED_CHILDREN.get(parent.kind, set()))) or "aucun")
            + ".",
        )
    if parent.path == node.path or node.path in parent.path.parents:
        raise MoveError(
            "invalid_target",
            f"Impossible de déplacer {node.id} sous lui-même ou un descendant.",
        )
    children = tree.children(node.id)
    if children:
        raise MoveError(
            "has_children",
            f"{node.id} a {len(children)} enfant(s) : déplacer la sous-arborescence n'est pas supporté.",
            hint="Déplacer ou supprimer les enfants d'abord.",
            exit_code=3,
        )
    target = parent.path / node.id
    if target.exists():
        raise MoveError(
            "target_exists",
            f"Le répertoire cible existe déjà : {target}.",
            exit_code=8,
        )
    return target


def _build_payload(
    node: Node,
    parent: Node,
    ip: str | None,
    port: int | None,
    network: str | None,
) -> dict[str, Any]:
    payload = json.loads(json.dumps(node.raw))
    payload["parent"] = parent.id
    if ip is None:
        return payload

    entry: dict[str, Any] = {"address": ip}
    if network:
        entry["network"] = network
    if port is not None:
        entry["port"] = int(port)
    payload["addresses"] = [entry]

    alias = parent.ssh_alias
    if alias and (node.provider == "docker" or isinstance(payload.get("management"), dict)):
        management = payload.get("management")
        if isinstance(management, dict):
            management["ssh_alias"] = alias

    runtime = payload.get("runtime")
    if isinstance(runtime, dict):
        if alias and runtime.get("ssh_alias"):
            runtime["ssh_alias"] = alias
        if runtime.get("bind"):
            runtime["bind"] = _rewrite_bind(str(runtime["bind"]), ip, port)

    health = payload.get("health")
    if (
        isinstance(health, dict)
        and health.get("type") == "http"
        and health.get("url")
    ):
        health["url"] = _rewrite_url(str(health["url"]), ip, port)
    return payload


def _plan(
    tree: Tree,
    node: Node,
    parent: Node,
    address: str | None,
    port: int | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    target = _check_target(tree, node, parent)
    current = _first_address(node)
    current_ip = _entry_ip(current) if current else None
    current_port = current.get("port") if current else None

    wanted_ip: str | None = None
    wanted_network: str | None = None
    warnings: list[str] = []

    if address:
        try:
            iface = ipaddress.ip_interface(address)
        except ValueError as exc:
            raise MoveError("bad_address", f"adresse invalide : {address!r}.", exit_code=2) from exc
        wanted_ip = str(iface.ip)
        wanted_network = _network_of(parent) or _network_of(node)
        cidr = _network_cidr(tree, wanted_network)
        if cidr and iface.ip not in ipaddress.ip_network(cidr, strict=False):
            raise MoveError(
                "address_outside",
                f"{wanted_ip} n'appartient pas au réseau {wanted_network} ({cidr}).",
            )
    elif current is not None or port is not None:
        wanted_ip = _parent_ip(parent)
        wanted_network = _network_of(parent) or _network_of(node)
        if wanted_ip is None:
            raise MoveError(
                "parent_no_address",
                f"{parent.id} n'a pas d'adresse : préciser --address.",
            )

    wanted_port = int(port) if port is not None else (int(current_port) if current_port is not None else None)
    if wanted_port is not None and not 1 <= wanted_port <= 65535:
        raise MoveError("bad_port", f"port invalide : {wanted_port}.", exit_code=2)

    if wanted_ip and wanted_port is not None:
        for other in tree.nodes.values():
            if other.id == node.id:
                continue
            for entry in other.addresses:
                if _entry_ip(entry) == wanted_ip and entry.get("port") == wanted_port:
                    raise MoveError(
                        "address_conflict",
                        f"{wanted_ip}:{wanted_port} est déjà utilisé par {other.id}.",
                        exit_code=8,
                    )

    payload = _build_payload(node, parent, wanted_ip, wanted_port, wanted_network)
    changes = [
        key
        for key in ("parent", "addresses", "management", "runtime", "health")
        if payload.get(key) != node.raw.get(key)
    ]
    if wanted_ip and not parent.ssh_alias:
        warnings.append(
            f"ssh_alias absent pour {parent.id} : management.ssh_alias inchangé."
        )

    after = None
    if wanted_ip:
        after = f"{wanted_ip}:{wanted_port}" if wanted_port is not None else wanted_ip

    next_steps: list[str] = []
    if node.kind in ("app", "container"):
        next_steps += [
            f"copier la configuration et les données vers {parent.id}",
            f"recréer le conteneur sur {parent.id} (`docker compose up -d`)",
            f"./bin/modops health {node.id}",
        ]
    else:
        next_steps += [
            f"vérifier {node.id} sur {parent.id}",
            "./bin/modops doctor",
        ]
    next_steps.append(
        f"rollback : ./bin/modops move {node.id} --to {node.parent}"
        + (f" --address {current_ip}" if current_ip else "")
        + (f" --port {current_port}" if current_port is not None else "")
    )

    plan = {
        "node": node.id,
        "from": node.parent,
        "to": parent.id,
        "source": str(node.path),
        "target": str(target),
        "address_before": node.endpoint or None,
        "address_after": after,
        "changes": changes,
        "warnings": warnings,
        "next": next_steps,
    }
    return plan, payload


def _error_keys(issues: list[dict[str, str]]) -> set[tuple[str, str, str]]:
    return {
        (issue.get("level", ""), issue.get("code", ""), issue.get("message", ""))
        for issue in issues
    }


def _write(
    directory: Path,
    payload: dict[str, Any],
    descriptor: dict[str, Any] | None = None,
) -> None:
    (directory / "node.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    effective = dict(payload)
    for key in ("provider", "verbs", "risk"):
        if key not in effective and descriptor and key in descriptor:
            effective[key] = descriptor[key]
    (directory / "AGENT.md").write_text(
        scaffold.card_text(effective, descriptor), encoding="utf-8"
    )


def move_node(
    tree: Tree,
    node: Node,
    parent: Node,
    address: str | None = None,
    port: int | None = None,
    dry_run: bool = False,
    observe: bool = True,
) -> dict[str, Any]:
    plan, payload = _plan(tree, node, parent, address, port)
    if dry_run:
        return {"dry_run": True, **plan}

    if observe and node.provider in ("docker", "qemu", "http", "ssh"):
        try:
            observed = str(providers.runtime_state(node).get("state") or "unknown")
        except Exception:
            observed = "unknown"
        plan["source_state"] = observed
        if observed in ("running", "degraded", "up"):
            raise MoveError(
                "source_running",
                f"{node.id} est {observed} sur {node.parent} : arrêter avant de déplacer.",
                hint=f"./bin/modops stop {node.id} --yes, puis relancer move.",
                exit_code=5,
            )

    original_dir = node.path
    target_dir = Path(plan["target"])
    originals = {
        name: (original_dir / name).read_text(encoding="utf-8")
        for name in ("node.json", "AGENT.md")
        if (original_dir / name).is_file()
    }
    before = _error_keys(Tree.load(tree.root).validate())
    moved = False
    try:
        _write(original_dir, payload, tree.descriptors.get(node.kind) or {})
        shutil.move(str(original_dir), str(target_dir))
        moved = True
        issues = Tree.load(tree.root).validate()
        new_errors = [
            issue
            for issue in issues
            if issue["level"] == "error" and _error_keys([issue]) - before
        ]
        if new_errors:
            detail = "; ".join(
                f"{issue['code']}: {issue['message'].rstrip('.')}" for issue in new_errors
            )
            raise MoveError(
                "invalid_tree",
                f"Le déplacement rend l'arborescence invalide : {detail}.",
            )
    except Exception:
        if moved and target_dir.exists() and not original_dir.exists():
            shutil.move(str(target_dir), str(original_dir))
        for name, text in originals.items():
            (original_dir / name).write_text(text, encoding="utf-8")
        raise

    warnings = [
        issue["message"]
        for issue in issues
        if issue["level"] == "warning" and issue.get("node") == node.id
    ]
    return {"dry_run": False, **plan, "warnings": [*plan["warnings"], *warnings]}
