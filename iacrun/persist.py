"""Découverte déterministe de l'environnement et persistance dans le tree.

Aucun modèle, aucune IA : les faits sont collectés par des commandes en
lecture seule, puis convertis en nœuds selon les descripteurs de kind.

Deux écritures :

- `persist_tree()` met à jour l'arbre courant en place, sans rien supprimer ;
- `persist_out()` matérialise un nouvel arbre complet dans un répertoire.

Un nœud existant est reconnu par son UUID, son adresse MAC, son interface,
son nom de conteneur ou son TAP. Son `id`, son `uuid` et ses liens sont
préservés ; seuls les faits découverts sont réécrits.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import providers, scaffold
from .model import Node, is_uuid, new_uuid
from .tree import Tree

Runner = Callable[[list[str], float], subprocess.CompletedProcess[str]]
SshRunner = Callable[..., subprocess.CompletedProcess[str]]


class PersistError(Exception):
    """Découverte ou écriture impossible, sans laisser d'arbre partiel."""

    def __init__(self, code: str, message: str, hint: str | None = None, exit_code: int = 4):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.exit_code = exit_code


@dataclass
class Inventory:
    hostname: str = "local"
    addresses: list[dict[str, Any]] = field(default_factory=list)
    bridges: list[dict[str, Any]] = field(default_factory=list)
    vms: list[dict[str, Any]] = field(default_factory=list)
    containers: list[dict[str, Any]] = field(default_factory=list)
    default_iface: str | None = None
    gateway: str | None = None
    kernel: str = ""
    resources: dict[str, Any] = field(default_factory=dict)


def _run(argv: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _text(runner: Runner, argv: list[str], timeout: float = 10.0) -> str:
    try:
        proc = runner(argv, timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _safe(value: str, prefix: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        slug = "node"
    if len(slug) < 3:
        slug = f"{slug}-node"
    candidate = slug if slug.startswith(prefix) else f"{prefix}-{slug}"
    return candidate[:63].rstrip("-")


def _ip_network(value: str) -> str:
    try:
        return str(ipaddress.ip_interface(value).network)
    except ValueError:
        return ""


def _parse_addresses(text: str) -> list[dict[str, Any]]:
    addresses: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = re.match(r"^\d+:\s+(\S+?)(?:@\S+)?\s+inet\s+(\S+)", line)
        if not match:
            continue
        iface, cidr = match.group(1), match.group(2)
        if iface == "lo" or cidr.startswith("127."):
            continue
        network = _ip_network(cidr)
        if not network:
            continue
        addresses.append({"iface": iface, "address": cidr, "network": network})
    return addresses


def _parse_macs(text: str) -> dict[str, str]:
    macs: dict[str, str] = {}
    for line in text.splitlines():
        match = re.match(r"^\d+:\s+(\S+?)(?:@\S+)?:.*link/ether\s+(\S+)", line)
        if match:
            macs[match.group(1)] = match.group(2)
    return macs


def _parse_bridges(text: str) -> list[str]:
    return [
        match.group(1)
        for line in text.splitlines()
        if (match := re.match(r"^\d+:\s+(\S+?)(?:@\S+)?:", line))
    ]


def _parse_default_route(text: str) -> tuple[str | None, str | None]:
    match = re.search(r"default\s+via\s+(\S+)\s+dev\s+(\S+)", text)
    if not match:
        return None, None
    return match.group(2), match.group(1)


def _parse_resources(text: str) -> dict[str, Any]:
    resources: dict[str, Any] = {}
    for line in text.splitlines():
        match = re.match(r"^\s*Mem:\s+(\d+)", line)
        if match:
            resources["ram_mib"] = int(match.group(1))
    return resources


def _parse_ports(value: str) -> list[dict[str, Any]]:
    ports: list[dict[str, Any]] = []
    for host, container in re.findall(r"(?::)?(\d+)->(\d+)/(?:tcp|udp)", value or ""):
        ports.append({"host": int(host), "container": int(container)})
    return ports


def _parse_containers(text: str) -> list[dict[str, Any]]:
    containers: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = str(data.get("Names") or data.get("Name") or "").strip()
        if not name:
            continue
        containers.append({
            "name": name,
            "image": str(data.get("Image") or ""),
            "ports": _parse_ports(str(data.get("Ports") or "")),
        })
    return containers


def _parse_qemu(text: str) -> list[dict[str, Any]]:
    vms: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or "qemu-system" not in line:
            continue
        _pid, _, rest = line.partition(" ")
        try:
            argv = shlex.split(rest)
        except ValueError:
            continue
        if not argv or "qemu-system" not in Path(argv[0]).name:
            continue

        def value(flag: str) -> str | None:
            for index, item in enumerate(argv):
                if item == flag and index + 1 < len(argv):
                    return argv[index + 1]
            return None

        name = value("-name") or f"vm-{_pid}"
        netdev = next((item for item in argv if item.startswith("tap,")), "")
        tap_match = re.search(r"ifname=([^,]+)", netdev)
        mac_match = re.search(r"mac=([0-9a-fA-F:]{17})", " ".join(argv))
        drive = next((item for item in argv if item.startswith("file=") and ".qcow2" in item), "")
        disk = drive[5:].split(",", 1)[0] if drive else ""
        memory = value("-m") or ""
        ram = re.match(r"^(\d+)([MG]?)", memory)
        vcpus = value("-smp") or ""
        qmp = value("-qmp") or ""
        if qmp.startswith("unix:"):
            qmp = qmp[5:].split(",", 1)[0]
        vms.append({
            "name": name,
            "argv": argv,
            "pid_file": value("-pidfile") or "",
            "qmp": qmp,
            "tap": tap_match.group(1) if tap_match else "",
            "mac": mac_match.group(1) if mac_match else "",
            "vcpus": int(vcpus.split(",", 1)[0]) if vcpus.isdigit() else None,
            "ram_mib": int(ram.group(1)) * (1024 if ram.group(2) == "G" else 1) if ram else None,
            "disk": disk,
        })
    return vms


def _bridge_for_tap(runner: Runner, tap: str) -> str:
    if not tap:
        return ""
    text = _text(runner, ["ip", "-o", "link", "show", tap])
    match = re.search(r"master\s+(\S+)", text)
    return match.group(1) if match else ""


def _local_inventory(runner: Runner) -> Inventory:
    inv = Inventory()
    inv.hostname = _text(runner, ["hostname"]).strip() or "local"
    inv.kernel = _text(runner, ["uname", "-sr"]).strip()
    inv.addresses = _parse_addresses(_text(runner, ["ip", "-4", "-o", "addr", "show"]))
    macs = _parse_macs(_text(runner, ["ip", "-o", "link", "show"]))
    for entry in inv.addresses:
        entry["mac"] = macs.get(str(entry["iface"]), "")

    default_iface, gateway = _parse_default_route(
        _text(runner, ["ip", "-4", "route", "show", "default"])
    )
    inv.default_iface = default_iface or (inv.addresses[0]["iface"] if inv.addresses else None)
    inv.gateway = gateway

    bridge_ifaces = _parse_bridges(_text(runner, ["ip", "-o", "link", "show", "type", "bridge"]))
    address_by_iface = {str(item["iface"]): item for item in inv.addresses}
    for iface in bridge_ifaces:
        entry = address_by_iface.get(iface)
        if entry is None:
            continue
        taps = _parse_bridges(_text(runner, ["ip", "-o", "link", "show", "master", iface]))
        inv.bridges.append({
            "iface": iface,
            "address": entry["address"],
            "network": entry["network"],
            "gateway": gateway if iface == inv.default_iface else None,
            "taps": taps,
        })

    inv.vms = _parse_qemu(_text(runner, ["ps", "-eo", "pid=,args="]))
    for vm in inv.vms:
        vm["bridge"] = _bridge_for_tap(runner, str(vm.get("tap") or ""))
    inv.containers = _parse_containers(
        _text(runner, ["docker", "ps", "--format", "{{json .}}"])
    )
    inv.resources = {**inv.resources, **_parse_resources(_text(runner, ["free", "-m"]))}
    cpus = _text(runner, ["nproc"]).strip()
    if cpus.isdigit():
        inv.resources["vcpus"] = int(cpus)
    return inv


def _ssh_inventory(alias: str, ssh_runner: SshRunner) -> Inventory | None:
    def text(argv: list[str]) -> str:
        try:
            proc = ssh_runner(alias, argv, timeout=8)
        except Exception:
            return ""
        return proc.stdout if proc.returncode == 0 else ""

    hostname = text(["hostname"]).strip()
    if not hostname:
        return None
    inv = Inventory(hostname=hostname)
    inv.kernel = text(["uname", "-sr"]).strip()
    inv.addresses = _parse_addresses(text(["ip", "-4", "-o", "addr", "show"]))
    macs = _parse_macs(text(["ip", "-o", "link", "show"]))
    for entry in inv.addresses:
        entry["mac"] = macs.get(str(entry["iface"]), "")
    inv.default_iface, inv.gateway = _parse_default_route(text(["ip", "-4", "route", "show", "default"]))
    inv.containers = _parse_containers(text(["docker", "ps", "--format", "{{json .}}"]))
    inv.resources = _parse_resources(text(["free", "-m"]))
    cpus = text(["nproc"]).strip()
    if cpus.isdigit():
        inv.resources["vcpus"] = int(cpus)
    return inv


def ssh_aliases(tree: Tree | None, config: Path | None = None) -> list[str]:
    aliases: list[str] = []
    if tree is not None:
        for node in tree.nodes.values():
            alias = node.ssh_alias
            if alias and alias not in aliases:
                aliases.append(alias)
    path = (config or Path("~/.ssh/config")).expanduser()
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        match = re.match(r"^\s*Host\s+(.+)$", line, re.IGNORECASE)
        if not match:
            continue
        for alias in match.group(1).split():
            if alias.startswith(("!", "*", "?")) or alias in aliases:
                continue
            aliases.append(alias)
    return aliases


def _primary_network(inv: Inventory) -> dict[str, Any] | None:
    if not inv.addresses:
        return None
    iface = inv.default_iface or inv.addresses[0]["iface"]
    return next((item for item in inv.addresses if item["iface"] == iface), inv.addresses[0])


def _network_id(entry: dict[str, Any]) -> str:
    return _safe(str(entry["iface"]), "net")


def _bridge_id(iface: str) -> str:
    return iface if iface.startswith("br-") else _safe(iface, "br")


def build_spec(
    local: Inventory,
    remotes: list[tuple[str, Inventory]],
) -> dict[str, Any]:
    """Convertit les inventaires en spécification `render`/`new`."""
    nodes: list[dict[str, Any]] = []
    networks: dict[str, dict[str, Any]] = {}

    for entry in local.addresses:
        cidr = str(entry["network"])
        if cidr in networks:
            continue
        network = {
            "id": _network_id(entry),
            "kind": "network",
            "title": f"Réseau {cidr}",
            "cidr": cidr,
            "addresses": [{
                "iface": entry["iface"],
                "address": entry["address"],
                "network": _network_id(entry),
            }],
            "children": [],
        }
        if local.gateway and entry["iface"] == local.default_iface:
            network["gateway"] = local.gateway
        networks[cidr] = network
        nodes.append(network)

    bridge_by_iface: dict[str, dict[str, Any]] = {}
    for bridge in local.bridges:
        network = networks.get(str(bridge["network"]))
        if network is None:
            continue
        node = {
            "id": _bridge_id(str(bridge["iface"])),
            "kind": "bridge",
            "title": f"Bridge {bridge['iface']}",
            "addresses": [{
                "iface": bridge["iface"],
                "address": bridge["address"],
                "network": network["id"],
            }],
            "runtime": {"type": "linux-bridge", "taps": bridge["taps"]},
            "children": [],
        }
        if bridge.get("gateway"):
            node["gateway"] = bridge["gateway"]
        network["children"].append(node)
        bridge_by_iface[str(bridge["iface"])] = node
        nodes.append(node)

    primary = _primary_network(local)
    primary_network = networks.get(str(primary["network"])) if primary else None
    if primary_network is None:
        raise PersistError(
            "no_network",
            "Aucune interface réseau IPv4 exploitable n'a été trouvée.",
            "Vérifier `ip -4 -o addr show` sur la machine analysée.",
        )

    host = {
        "id": _safe(local.hostname, "host"),
        "kind": "host",
        "title": f"Hôte {local.hostname}",
        "provider": "local",
        "addresses": [{
            "iface": primary["iface"],
            "address": primary["address"],
            "network": primary_network["id"],
            "mac": primary.get("mac", ""),
        }],
        "runtime": {"kernel": local.kernel} if local.kernel else {},
        "health": {"type": "command", "argv": ["true"]},
        "children": [],
    }
    if local.resources:
        host["resources"] = dict(local.resources)
    primary_network["children"].append(host)
    nodes.append(host)

    vm_by_name: dict[str, dict[str, Any]] = {}
    for vm in local.vms:
        name = str(vm["name"])
        parent = bridge_by_iface.get(str(vm.get("bridge") or "")) or primary_network
        node = {
            "id": _safe(name, "vm"),
            "kind": "vm",
            "title": f"VM {name}",
            "runtime": {
                "start": [str(item) for item in vm["argv"]],
                "pid_file": vm["pid_file"],
                "qmp": vm["qmp"],
                "tap": vm["tap"],
                "disk": vm["disk"],
            },
            "health": {"type": "qemu"},
            "children": [],
        }
        if vm.get("vcpus") or vm.get("ram_mib"):
            node["resources"] = {
                key: int(vm[key]) for key in ("vcpus", "ram_mib") if vm.get(key)
            }
        if vm.get("mac"):
            node["details"] = {"mac": vm["mac"]}
        parent["children"].append(node)
        vm_by_name[name] = node
        nodes.append(node)

    for container in local.containers:
        _append_app(
            host,
            container,
            alias="local",
            network=primary_network,
            ip=str(primary["address"]).split("/")[0],
        )

    for alias, remote in remotes:
        remote_primary = _primary_network(remote)
        if remote_primary is None:
            continue
        remote_network = networks.get(str(remote_primary["network"]))
        if remote_network is None:
            remote_network = {
                "id": _network_id(remote_primary),
                "kind": "network",
                "title": f"Réseau {remote_primary['network']}",
                "cidr": str(remote_primary["network"]),
                "addresses": [{
                    "iface": remote_primary["iface"],
                    "address": remote_primary["address"],
                    "network": _network_id(remote_primary),
                }],
                "children": [],
            }
            networks[str(remote_primary["network"])] = remote_network
            nodes.append(remote_network)

        parent = vm_by_name.get(alias) or vm_by_name.get(remote.hostname)
        if parent is None:
            host_id = _safe(alias, "host")
            parent = {
                "id": host_id,
                "kind": "host",
                "title": f"Hôte {alias}",
                "provider": "ssh",
                "addresses": [{
                    "iface": remote_primary["iface"],
                    "address": remote_primary["address"],
                    "network": remote_network["id"],
                    "mac": remote_primary.get("mac", ""),
                }],
                "management": {"ssh_alias": alias},
                "runtime": {"kernel": remote.kernel} if remote.kernel else {},
                "health": {"type": "ssh", "ssh_alias": alias, "argv": ["true"]},
                "children": [],
            }
            if remote.resources:
                parent["resources"] = dict(remote.resources)
            remote_network["children"].append(parent)
            nodes.append(parent)

        for container in remote.containers:
            _append_app(
                parent,
                container,
                alias=alias,
                network=remote_network,
                ip=str(remote_primary["address"]).split("/")[0],
            )

    return {
        "schema": 1,
        "root": {"id": "home", "title": "Environnement découvert"},
        "nodes": nodes,
    }


def _append_app(
    parent: dict[str, Any],
    container: dict[str, Any],
    alias: str,
    network: dict[str, Any],
    ip: str,
) -> None:
    name = str(container["name"])
    ports = container.get("ports") or []
    port = int(ports[0]["container"]) if ports else None
    host_port = int(ports[0]["host"]) if ports else None
    node: dict[str, Any] = {
        "id": _safe(name, "app"),
        "kind": "app",
        "title": f"Application {name}",
        "runtime": {
            "type": "docker",
            "image": container.get("image", ""),
            "container": name,
        },
        "management": {"ssh_alias": alias, "container": name},
        "health": {"type": "docker", "ssh_alias": alias, "container": name},
        "children": [],
    }
    if port is not None:
        node["addresses"] = [{
            "address": ip,
            "network": network["id"],
            "port": port,
        }]
        if host_port is not None and host_port != port:
            node["runtime"]["bind"] = f"{ip}:{host_port}->{port}"
    parent["children"].append(node)


def _flatten(nodes: list[dict[str, Any]], parent: str | None) -> list[tuple[dict[str, Any], str | None]]:
    flat: list[tuple[dict[str, Any], str | None]] = []
    for raw in nodes:
        item = dict(raw)
        children = item.pop("children", []) or []
        item["parent"] = parent
        flat.append((item, parent))
        flat.extend(_flatten(children, str(item["id"])))
    return flat


def discover(
    runner: Runner = _run,
    ssh_runner: SshRunner = providers.ssh_run,
    tree: Tree | None = None,
    with_ssh: bool = True,
) -> tuple[Inventory, list[tuple[str, Inventory]]]:
    local = _local_inventory(runner)
    remotes: list[tuple[str, Inventory]] = []
    if with_ssh:
        for alias in ssh_aliases(tree):
            remote = _ssh_inventory(alias, ssh_runner)
            if remote is not None:
                remotes.append((alias, remote))
    return local, remotes


def persist_out(
    out: Path,
    runner: Runner = _run,
    ssh_runner: SshRunner = providers.ssh_run,
    with_ssh: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    local, remotes = discover(runner, ssh_runner, tree=None, with_ssh=with_ssh)
    spec = build_spec(local, remotes)
    return scaffold.render_spec(spec, out, dry_run=dry_run)


def _signature(node: Node) -> list[str]:
    values: list[str] = []
    runtime = node.raw.get("runtime") if isinstance(node.raw.get("runtime"), dict) else {}
    management = node.raw.get("management") if isinstance(node.raw.get("management"), dict) else {}
    for entry in node.addresses:
        for key in ("mac", "iface", "address"):
            if entry.get(key):
                values.append(f"{key}:{str(entry[key]).split('/')[0].lower()}")
    for key in ("container", "tap", "disk"):
        if runtime.get(key):
            values.append(f"{key}:{str(runtime[key]).lower()}")
    if management.get("container"):
        values.append(f"container:{str(management['container']).lower()}")
    return values


def _match(tree: Tree, item: dict[str, Any]) -> Node | None:
    if item.get("id") in tree.nodes:
        return tree.nodes[str(item["id"])]
    wanted: set[str] = set()
    for entry in item.get("addresses") or []:
        for key in ("mac", "iface"):
            if entry.get(key):
                wanted.add(f"{key}:{str(entry[key]).split('/')[0].lower()}")
    runtime = item.get("runtime") or {}
    for key in ("container", "tap", "disk"):
        if runtime.get(key):
            wanted.add(f"{key}:{str(runtime[key]).lower()}")
    if not wanted:
        return None
    for node in tree.nodes.values():
        if wanted & set(_signature(node)):
            return node
    return None


def _rewrite_vm_argv(argv: list[str], node: Node) -> list[str]:
    runtime = node.raw.get("runtime") if isinstance(node.raw.get("runtime"), dict) else {}
    rewritten: list[str] = []
    index = 0
    while index < len(argv):
        value = argv[index]
        if value in ("-name", "-pidfile", "-qmp") and index + 1 < len(argv):
            if value == "-name":
                rewritten.extend([value, node.id])
            elif value == "-pidfile" and runtime.get("pid_file"):
                rewritten.extend([value, str(runtime["pid_file"])])
            elif value == "-qmp" and runtime.get("qmp"):
                qmp = str(runtime["qmp"])
                if qmp.startswith("unix:"):
                    qmp = qmp[5:].split(",", 1)[0]
                rewritten.extend([value, f"unix:{qmp},server=on,wait=off"])
            else:
                rewritten.extend([value, argv[index + 1]])
            index += 2
            continue
        if value.startswith("tap,") and runtime.get("tap"):
            value = re.sub(r"ifname=[^,]+", f"ifname={runtime['tap']}", value)
        rewritten.append(value)
        index += 1
    return rewritten


def _merge(existing: Node, item: dict[str, Any], tree: Tree) -> tuple[dict[str, Any], list[str]]:
    raw = json.loads(json.dumps(existing.raw))
    changes: list[str] = []
    raw.setdefault("uuid", existing.uuid or new_uuid())

    if item.get("addresses"):
        raw["addresses"] = item["addresses"]
        changes.append("addresses")
    if item.get("cidr"):
        raw["cidr"] = item["cidr"]
        changes.append("cidr")
    if item.get("gateway"):
        raw["gateway"] = item["gateway"]
        changes.append("gateway")
    if item.get("resources"):
        raw["resources"] = item["resources"]
        changes.append("resources")
    if item.get("management"):
        management = raw.get("management") if isinstance(raw.get("management"), dict) else {}
        raw["management"] = {**management, **item["management"]}
        changes.append("management")
    if item.get("health"):
        raw["health"] = item["health"]
        changes.append("health")
    if item.get("runtime"):
        runtime = raw.get("runtime") if isinstance(raw.get("runtime"), dict) else {}
        incoming = dict(item["runtime"])
        if existing.kind == "vm" and incoming.get("start"):
            incoming["start"] = _rewrite_vm_argv([str(part) for part in incoming["start"]], existing)
        raw["runtime"] = {**runtime, **incoming}
        raw["runtime"].pop("installer", None)
        changes.append("runtime")
    if item.get("details"):
        details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
        raw["details"] = {**details, **item["details"]}
        changes.append("details")
    raw.pop("facts_from", None)
    return raw, changes


def _write_node(tree: Tree, node: Node, raw: dict[str, Any]) -> None:
    (node.path / "node.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    descriptor = tree.descriptors.get(node.kind) or {}
    effective = dict(raw)
    for key in ("provider", "verbs", "risk"):
        if key not in effective and descriptor.get(key):
            effective[key] = descriptor[key]
    (node.path / "AGENT.md").write_text(
        scaffold.card_text(effective, descriptor), encoding="utf-8"
    )


def _parent_for(tree: Tree, spec_id: str | None, mapping: dict[str, str]) -> Node:
    if spec_id:
        actual = mapping.get(spec_id, spec_id)
        if actual in tree.nodes:
            return tree.nodes[actual]
    return tree.root_node


def sync_spec(tree: Tree, spec: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    """Met à jour l'arbre en place ; ne supprime jamais de nœud."""
    current = tree
    flat = _flatten(spec.get("nodes") or [], "home")
    mapping: dict[str, str] = {"home": current.root_node.id}
    report: dict[str, list[str]] = {"updated": [], "created": [], "skipped": []}

    for item, parent_spec in flat:
        existing = _match(current, item)
        if existing is not None:
            mapping[str(item["id"])] = existing.id
            raw, changes = _merge(existing, item, current)
            if changes:
                report["updated"].append(existing.id)
                if not dry_run:
                    _write_node(current, existing, raw)
            else:
                report["skipped"].append(existing.id)
            continue

        parent = _parent_for(current, parent_spec, mapping)
        try:
            result = scaffold.create_node(
                current,
                parent.id,
                item,
                dry_run=dry_run,
            )
        except (scaffold.SpecError, Exception) as error:  # noqa: BLE001 - une ressource isolée ne bloque pas le reste
            report["skipped"].append(f"{item.get('id')}: {error}")
            continue
        created_id = str(result["node"]["id"])
        mapping[str(item["id"])] = created_id
        report["created"].append(created_id)
        if not dry_run:
            current = Tree.load(current.root)

    if not dry_run:
        report["tree"] = str(current.root)
    return report
