"""Chargement, résolution et validation de l'arborescence."""

from __future__ import annotations

import ipaddress
import json
import os
import re
from pathlib import Path
from typing import Any

from .model import (
    KINDS,
    PROVIDERS,
    RISKS,
    VERBS,
    Node,
    children_kinds,
    is_uuid,
    load_descriptors,
)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
SECRET_RE = re.compile(
    r"(?i)(BEGIN [A-Z ]*PRIVATE KEY|password\s*[:=]\s*\S|token\s*[:=]\s*\S|secret\s*[:=]\s*\S)"
)
KIND_ORDER = {
    "site": 0, "network": 1, "host": 2, "bridge": 3,
    "vm": 4, "container": 5, "device": 6, "app": 7,
}


class TreeError(Exception):
    """Erreur de résolution ou de chargement, avec code de sortie."""

    def __init__(self, code: str, message: str, hint: str | None = None, exit_code: int = 4):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.exit_code = exit_code


class Tree:
    def __init__(
        self,
        root: Path,
        nodes: dict[str, Node],
        by_dir: dict[Path, Node],
        stray: list[Path],
        descriptors: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.root = root
        self.nodes = nodes
        self.by_dir = by_dir
        self.stray = stray
        self.descriptors = descriptors or {}

    @classmethod
    def load(cls, root: Path) -> "Tree":
        root = root.expanduser().resolve()
        if not (root / "node.json").is_file():
            raise TreeError(
                "invalid_root",
                f"Racine invalide : {root} (node.json absent).",
                "Définir IACRUN_ROOT ou --root vers tree/home.",
                exit_code=2,
            )
        try:
            descriptors = load_descriptors(root)
        except ValueError as exc:
            raise TreeError("invalid_descriptor", str(exc), exit_code=2) from exc

        nodes: dict[str, Node] = {}
        by_dir: dict[Path, Node] = {}
        stray: list[Path] = []

        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                d for d in dirnames if not d.startswith(".") and d != "__pycache__"
            )
            directory = Path(dirpath)
            if "node.json" not in filenames:
                if "AGENT.md" in filenames:
                    stray.append(directory)
                continue
            try:
                node = Node.load(directory / "node.json", descriptors)
            except (OSError, json.JSONDecodeError, KeyError) as exc:
                raise TreeError(
                    "invalid_node",
                    f"Impossible de charger {directory / 'node.json'} : {exc}",
                    exit_code=2,
                ) from exc
            if node.id in nodes:
                raise TreeError(
                    "duplicate_id",
                    f"id dupliqué : {node.id} ({nodes[node.id].path} et {node.path}).",
                    exit_code=2,
                )
            nodes[node.id] = node
            by_dir[directory.resolve()] = node

        if not nodes:
            raise TreeError("empty_tree", f"Aucun nœud sous {root}.", exit_code=2)

        tree = cls(root, nodes, by_dir, stray, descriptors)
        tree._check_parents()
        return tree

    def _check_parents(self) -> None:
        roots = [n for n in self.nodes.values() if n.path == self.root]
        if len(roots) != 1:
            raise TreeError(
                "invalid_root",
                f"La racine {self.root} doit contenir exactement un nœud.",
                exit_code=2,
            )
        for node in self.nodes.values():
            if node.path == self.root:
                if node.parent is not None:
                    raise TreeError(
                        "invalid_parent",
                        f"Le nœud racine {node.id} doit avoir parent=null.",
                        exit_code=2,
                    )
                continue
            parent_node = self.parent_of(node)
            if parent_node is None:
                raise TreeError(
                    "orphan",
                    f"{node.id} n'a pas de nœud parent.",
                    f"Placer {node.id} sous un répertoire contenant node.json.",
                    exit_code=2,
                )
            if node.parent != parent_node.id:
                raise TreeError(
                    "invalid_parent",
                    f"{node.id}.parent={node.parent!r}, attendu {parent_node.id!r}.",
                    exit_code=2,
                )

    @property
    def root_node(self) -> Node:
        for node in self.nodes.values():
            if node.path == self.root:
                return node
        raise TreeError("invalid_root", "Nœud racine introuvable.", exit_code=2)

    def parent_of(self, node: Node) -> Node | None:
        current = node.path.parent
        while True:
            if current in self.by_dir:
                return self.by_dir[current]
            if current == self.root or current.parent == current:
                return None
            current = current.parent

    def children(self, node_id: str) -> list[Node]:
        node = self.nodes[node_id]
        kids = [
            candidate
            for candidate in self.nodes.values()
            if candidate.id != node.id and self.parent_of(candidate) == node
        ]
        return sorted(kids, key=lambda n: (KIND_ORDER.get(n.kind, 99), n.id))

    def children_recursive(self, node_id: str, depth: int = 1) -> list[tuple[Node, int]]:
        out: list[tuple[Node, int]] = []

        def walk(current: str, level: int) -> None:
            if level > depth:
                return
            for child in self.children(current):
                out.append((child, level))
                walk(child.id, level + 1)

        walk(node_id, 1)
        return out

    def resolve(self, ref: str | None) -> Node:
        if ref is None or str(ref).strip() == "":
            raise TreeError("not_found", "Cible vide.", exit_code=2)
        text = str(ref).strip().strip("'\"")

        # 1. Chemin existant (absolu, relatif, ou commençant par . / ~).
        candidate = Path(text).expanduser()
        looks_like_path = (
            candidate.is_absolute()
            or text.startswith(".")
            or "/" in text
            or text in (".", "..")
        )
        if looks_like_path:
            absolute = candidate if candidate.is_absolute() else (Path.cwd() / candidate)
            if absolute.exists():
                directory = self._node_dir(absolute)
                if directory is not None:
                    return self.by_dir[directory]

        # 2. id exact.
        if text in self.nodes:
            return self.nodes[text]

        # 3. alias exact.
        alias_matches = [n for n in self.nodes.values() if text in n.aliases]
        if len(alias_matches) == 1:
            return alias_matches[0]
        if len(alias_matches) > 1:
            raise self._ambiguous(text, alias_matches)

        # 4. suffixe unique d'id.
        suffix_matches = [n for n in self.nodes.values() if n.id.endswith(text)]
        if len(suffix_matches) == 1:
            return suffix_matches[0]
        if len(suffix_matches) > 1:
            raise self._ambiguous(text, suffix_matches)

        raise TreeError(
            "not_found",
            f"Cible introuvable : {text!r}.",
            "Utiliser `modops ls` ou `modops path <id>`.",
        )

    def _node_dir(self, path: Path) -> Path | None:
        current = path if path.is_dir() else path.parent
        current = current.resolve()
        while True:
            if (current / "node.json").is_file():
                return current if current in self.by_dir else None
            if current == self.root or current.parent == current:
                return None
            current = current.parent

    @staticmethod
    def _ambiguous(text: str, matches: list[Node]) -> TreeError:
        ids = ", ".join(sorted(n.id for n in matches))
        return TreeError(
            "ambiguous",
            f"Cible ambiguë : {text!r} correspond à {ids}.",
            "Préciser l'id complet.",
        )

    def default_node(self) -> Node:
        try:
            return self.resolve(str(Path.cwd()))
        except TreeError:
            return self.root_node

    def render(self, node_id: str | None = None, annotate: bool = True) -> str:
        node = self.nodes[node_id] if node_id else self.root_node
        lines = [self._label(node, annotate)]

        def walk(current: Node, prefix: str) -> None:
            kids = self.children(current.id)
            for index, child in enumerate(kids):
                last = index == len(kids) - 1
                lines.append(prefix + ("└── " if last else "├── ") + self._label(child, annotate))
                walk(child, prefix + ("    " if last else "│   "))

        walk(node, "")
        return "\n".join(lines)

    @staticmethod
    def _label(node: Node, annotate: bool) -> str:
        if not annotate:
            return node.id
        suffix = f" {node.endpoint}" if node.endpoint else ""
        return f"{node.id} [{node.kind}{suffix}]"

    def validate(self) -> list[dict[str, str]]:
        issues: list[dict[str, str]] = []

        def add(level: str, code: str, message: str, node_id: str | None = None) -> None:
            issue = {"level": level, "code": code, "message": message}
            if node_id:
                issue["node"] = node_id
            issues.append(issue)

        root = self.root_node
        if root.kind != "site":
            add("error", "root_kind", f"La racine doit être de kind site, pas {root.kind}.", root.id)

        uuid_map: dict[str, list[str]] = {}
        uuid_to_node: dict[str, Node] = {}
        for node in self.nodes.values():
            if is_uuid(node.uuid):
                uuid_map.setdefault(node.uuid, []).append(node.id)
                uuid_to_node[node.uuid] = node

        alias_map: dict[str, list[str]] = {}
        for node in self.nodes.values():
            if not is_uuid(node.uuid):
                add("error", "uuid", f"uuid absent ou invalide : {node.uuid!r}.", node.id)
            if not ID_RE.match(node.id):
                add("error", "invalid_id", f"id invalide : {node.id!r}.", node.id)
            if node.path.name != node.id:
                add(
                    "error",
                    "dir_name",
                    f"Le répertoire {node.path.name!r} ne correspond pas à l'id {node.id!r}.",
                    node.id,
                )
            if node.raw.get("schema") != 1:
                add("error", "schema", "schema doit valoir 1.", node.id)
            if node.kind not in KINDS:
                add("error", "kind", f"kind inconnu : {node.kind}.", node.id)
            if node.provider not in PROVIDERS:
                add("error", "provider", f"provider inconnu : {node.provider}.", node.id)
            if node.risk not in RISKS:
                add("error", "risk", f"risk inconnu : {node.risk}.", node.id)
            for verb in node.verbs:
                if verb not in VERBS:
                    add("error", "verb", f"verbe inconnu : {verb}.", node.id)
            for alias in node.aliases:
                alias_map.setdefault(alias, []).append(node.id)
                if not ID_RE.match(alias):
                    add("error", "alias", f"alias invalide : {alias!r}.", node.id)

            parent = self.parent_of(node) if node.path != self.root else None

            if not node.card_path.is_file():
                add("error", "missing_card", "AGENT.md absent.", node.id)
            else:
                text = node.card()
                lines = text.count("\n") + 1
                if lines > 45:
                    add("warning", "card_too_long", f"AGENT.md fait {lines} lignes (max 45).", node.id)
                if node.id not in text:
                    add("warning", "card_id", "AGENT.md ne mentionne pas l'id du nœud.", node.id)

            for address in node.addresses:
                value = str(address.get("address", ""))
                try:
                    if "/" in value:
                        ipaddress.ip_interface(value)
                    else:
                        ipaddress.ip_address(value)
                except ValueError:
                    add("error", "address", f"adresse invalide : {value!r}.", node.id)
                network = address.get("network")
                if network:
                    target = self.nodes.get(str(network))
                    if target is None or target.kind != "network":
                        add("error", "network_ref", f"réseau inconnu : {network!r}.", node.id)

            if parent and node.kind == "app":
                parent_ips = parent.ips
                app_ips = node.ips
                if parent_ips and app_ips and not (app_ips & parent_ips):
                    add(
                        "warning",
                        "app_address_outside_parent",
                        "adresse "
                        f"{', '.join(sorted(app_ips))} absente de {parent.id} "
                        f"({', '.join(sorted(parent_ips))}).",
                        node.id,
                    )
                app_alias = node.ssh_alias
                parent_alias = parent.ssh_alias
                if app_alias and parent_alias and app_alias != parent_alias:
                    add(
                        "warning",
                        "management_alias_mismatch",
                        f"ssh_alias {app_alias!r} diffère de {parent.id} ({parent_alias!r}).",
                        node.id,
                    )

            if node.kind == "network":
                cidr = node.raw.get("cidr")
                if not cidr:
                    add("error", "cidr", "Un nœud network doit déclarer cidr.", node.id)
                else:
                    try:
                        net = ipaddress.ip_network(str(cidr), strict=False)
                    except ValueError:
                        add("error", "cidr", f"CIDR invalide : {cidr!r}.", node.id)
                    else:
                        gateway = node.raw.get("gateway")
                        if gateway:
                            try:
                                if ipaddress.ip_address(str(gateway)) not in net:
                                    add("error", "gateway", f"Passerelle {gateway} hors de {cidr}.", node.id)
                            except ValueError:
                                add("error", "gateway", f"Passerelle invalide : {gateway!r}.", node.id)

            if node.health and node.health.get("type") not in ("http", "command", "ssh", "docker", "qemu"):
                add("error", "health_type", "health.type inconnu.", node.id)
            if node.kind in ("vm", "app", "bridge", "device", "host") and not node.health:
                add("warning", "no_health", "Aucun test de santé déclaré.", node.id)

            for link in node.raw.get("links", []):
                target = link.get("to")
                if not is_uuid(target):
                    add("error", "link_uuid", f"lien sans UUID valide : {target!r}.", node.id)
                elif target not in uuid_to_node:
                    add("error", "link", f"lien vers un UUID inconnu : {target!r}.", node.id)
                label = link.get("label")
                if label is not None and not isinstance(label, str):
                    add("error", "link_label", "links[].label doit être une chaîne.", node.id)
                models = link.get("models", [])
                if not isinstance(models, list) or any(
                    not isinstance(model, str) or not model.strip() for model in models
                ):
                    add("error", "link_models", "links[].models doit être une liste de noms non vides.", node.id)

            if parent and node.kind not in children_kinds(parent.kind, self.descriptors):
                add(
                    "error",
                    "containment",
                    f"{node.kind} ne peut pas être enfant de {parent.kind}.",
                    node.id,
                )

            for filename in ("node.json", "AGENT.md"):
                path = node.path / filename
                if path.is_file() and SECRET_RE.search(path.read_text(encoding="utf-8")):
                    add("error", "secret", f"Secret potentiel dans {filename}.", node.id)

        for alias, ids in alias_map.items():
            if len(ids) > 1:
                add("error", "alias_duplicate", f"alias {alias!r} défini par {', '.join(sorted(ids))}.")

        for value, ids in uuid_map.items():
            if len(ids) > 1:
                add("error", "uuid_duplicate", f"uuid {value!r} défini par {', '.join(sorted(ids))}.")

        for directory in self.stray:
            add(
                "error",
                "stray_card",
                f"AGENT.md sans node.json dans {directory.relative_to(self.root)}.",
            )

        return issues
