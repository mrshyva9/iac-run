"""Modèle de nœud iac-run. Aucune dépendance externe."""

from __future__ import annotations

import ipaddress
import json
import uuid as uuidlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent


KINDS = ("site", "network", "host", "bridge", "vm", "container", "app", "device")
PROVIDERS = ("local", "qemu", "ssh", "docker", "http", "none")
RISKS = ("low", "medium", "high")

VERBS = (
    "ls", "show", "path", "status", "health", "logs", "tree", "doctor", "ask",
    "start", "stop", "restart",
    "net.list", "net.create", "net.attach", "net.detach", "net.move",
    "connect", "disconnect",
)

# Contenance autorisée : parent -> kinds enfants.
ALLOWED_CHILDREN: dict[str, set[str]] = {
    "site": {"network"},
    "network": {"host", "bridge", "vm", "container"},
    "host": {"network", "vm", "container", "device", "app"},
    "bridge": {"vm", "container", "app"},
    "vm": {"app", "container", "device"},
    "container": {"app"},
    "app": set(),
    "device": set(),
}


@dataclass(frozen=True)
class Node:
    """Un nœud de l'arborescence, chargé depuis son node.json."""

    id: str
    kind: str
    title: str
    parent: str | None
    provider: str
    verbs: tuple[str, ...]
    risk: str
    path: Path
    raw: dict[str, Any]
    descriptor: dict[str, Any] = field(default_factory=dict)
    uuid: str = ""

    @classmethod
    def load(
        cls,
        node_file: Path,
        descriptors: dict[str, dict[str, Any]] | None = None,
    ) -> "Node":
        data = json.loads(node_file.read_text(encoding="utf-8"))
        kind = str(data["kind"])
        descriptor = (descriptors or {}).get(kind) or {}
        return cls(
            id=str(data["id"]),
            kind=kind,
            title=str(data["title"]),
            parent=data.get("parent"),
            provider=str(data.get("provider") or descriptor.get("provider") or "none"),
            verbs=tuple(data.get("verbs") or descriptor.get("verbs") or []),
            risk=str(data.get("risk") or descriptor.get("risk") or "low"),
            path=node_file.parent,
            raw=data,
            descriptor=descriptor,
            uuid=str(data.get("uuid") or ""),
        )

    @property
    def effective(self) -> dict[str, Any]:
        """Faits du nœud, complétés par son descripteur de kind."""
        data = dict(self.raw)
        for key in ("provider", "verbs", "risk"):
            if key not in data:
                value = self.descriptor.get(key)
                if value is not None:
                    data[key] = value
        return data

    @property
    def card_path(self) -> Path:
        return self.path / "AGENT.md"

    def card(self) -> str:
        try:
            return self.card_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""

    @property
    def summary(self) -> str:
        return str(self.raw.get("summary") or self.title)

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(self.raw.get("aliases", []))

    @property
    def addresses(self) -> list[dict[str, Any]]:
        return list(self.raw.get("addresses", []))

    @property
    def ips(self) -> set[str]:
        """Adresses IP du nœud, préfixe retiré."""
        values: set[str] = set()
        for address in self.addresses:
            value = str(address.get("address") or "")
            if not value:
                continue
            try:
                values.add(str(ipaddress.ip_interface(value).ip))
            except ValueError:
                continue
        return values

    @property
    def ssh_alias(self) -> str:
        """Alias SSH de management, sinon celui du runtime."""
        management = self.raw.get("management")
        if isinstance(management, dict) and management.get("ssh_alias"):
            return str(management["ssh_alias"])
        runtime = self.raw.get("runtime")
        if isinstance(runtime, dict) and runtime.get("ssh_alias"):
            return str(runtime["ssh_alias"])
        return ""

    @property
    def endpoint(self) -> str:
        if self.kind == "network" and self.raw.get("cidr"):
            return str(self.raw["cidr"])
        for address in self.addresses:
            value = str(address.get("address", ""))
            port = address.get("port")
            if value and port:
                return f"{value}:{port}"
            if value:
                return value
        return ""

    @property
    def health(self) -> dict[str, Any]:
        value = self.raw.get("health")
        return value if isinstance(value, dict) else {}

    def child_index(self, children: list["Node"]) -> list[dict[str, str]]:
        """Résumé compact pour le contexte d'un agent (id, kind, summary)."""
        return [
            {"id": child.id, "kind": child.kind, "summary": child.summary}
            for child in children
        ]


def load_descriptors(root: Path) -> dict[str, dict[str, Any]]:
    """Charge les descripteurs de kind, du dépôt puis du tree local.

    Un descripteur local (`<root>/../kinds`) surcharge le catalogue du dépôt.
    Les kinds sans descripteur retombent sur les constantes de ce module.
    """
    descriptors: dict[str, dict[str, Any]] = {}
    candidates = (REPO_ROOT / "tree" / "kinds", root.expanduser().resolve().parent / "kinds")
    for directory in candidates:
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError(f"descripteur illisible {path}: {exc}") from exc
            kind = str(data.get("kind") or path.stem)
            if kind != path.stem:
                raise ValueError(f"descripteur {path}: kind {kind!r} != {path.stem!r}")
            if not isinstance(data.get("verbs", []), list):
                raise ValueError(f"descripteur {path}: verbs doit être une liste")
            if not isinstance(data.get("children", []), list):
                raise ValueError(f"descripteur {path}: children doit être une liste")
            descriptors[kind] = data
    return descriptors


def children_kinds(
    kind: str,
    descriptors: dict[str, dict[str, Any]] | None = None,
) -> set[str]:
    """Kinds autorisés sous `kind`, selon son descripteur ou le fallback code."""
    descriptor = (descriptors or {}).get(kind) or {}
    if "children" in descriptor:
        return set(descriptor["children"])
    return set(ALLOWED_CHILDREN.get(kind, set()))


def new_uuid() -> str:
    """Identité stable d'un nœud, indépendante de son chemin et de son parent."""
    return str(uuidlib.uuid4())


def is_uuid(value: object) -> bool:
    """Vrai pour un UUID canonique (forme `8-4-4-4-12`)."""
    if not isinstance(value, str):
        return False
    try:
        return str(uuidlib.UUID(value)) == value
    except (ValueError, AttributeError):
        return False
