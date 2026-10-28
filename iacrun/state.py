"""État désiré et observé d'iac-run.

- `state/desired.json` : ce qu'iac-run doit restaurer après un reboot.
- `state/observed.json` : dernière observation connue.
- `state/audit.jsonl` : journal des actions (v1).

Le répertoire d'état est surchargeable par `IACRUN_STATE_DIR` (tests, CI).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import providers
from .model import Node
from .tree import Tree

REPO_ROOT = Path(__file__).resolve().parent.parent
POWER_KINDS = {"vm", "container", "app"}
POWER_PROVIDERS = {"qemu", "docker"}
POWER_ORDER = {"bridge": 0, "vm": 1, "container": 2, "app": 3}


def state_dir() -> Path:
    return Path(os.environ.get("IACRUN_STATE_DIR", str(REPO_ROOT / "state"))).expanduser()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _read(name: str, default: dict[str, Any]) -> dict[str, Any]:
    path = state_dir() / name
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _write(name: str, data: dict[str, Any]) -> None:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def is_power_capable(node: Node) -> bool:
    return (
        node.kind in POWER_KINDS
        and node.provider in POWER_PROVIDERS
        and "start" in node.verbs
    )


def load_desired() -> dict[str, Any]:
    return _read("desired.json", {"version": 1, "updated_at": None, "nodes": {}})


def save_desired(data: dict[str, Any]) -> dict[str, Any]:
    data["version"] = 1
    data["updated_at"] = _now()
    _write("desired.json", data)
    return data


def set_power(node_id: str, power: str) -> dict[str, Any]:
    if power not in ("running", "stopped"):
        raise ValueError(f"power invalide : {power}")
    data = load_desired()
    data.setdefault("nodes", {})[node_id] = {"power": power}
    return save_desired(data)


def clear_power(node_id: str | None = None) -> dict[str, Any]:
    data = load_desired()
    if node_id:
        data.setdefault("nodes", {}).pop(node_id, None)
    else:
        data["nodes"] = {}
    return save_desired(data)


def observe(tree: Tree) -> dict[str, Any]:
    nodes: dict[str, Any] = {}
    for node in sorted(tree.nodes.values(), key=lambda item: item.id):
        if not is_power_capable(node):
            continue
        # `runtime_state` distingue « conteneur vivant » de « API qui répond » :
        # un conteneur actif mais muet est `degraded`, pas `stopped`.
        result = providers.runtime_state(node)
        nodes[node.id] = {
            "state": result["state"],
            "detail": result["detail"],
            "provider": result["provider"],
        }
    return {"version": 1, "updated_at": _now(), "nodes": nodes}


def save_observed(tree: Tree) -> dict[str, Any]:
    data = observe(tree)
    _write("observed.json", data)
    return data


def load_observed() -> dict[str, Any]:
    return _read("observed.json", {"version": 1, "updated_at": None, "nodes": {}})


def audit(
    actor: str,
    verb: str,
    target: str | None,
    args: dict[str, Any],
    result: str,
    duration_ms: int,
    detail: str = "",
) -> None:
    """Ajoute une ligne au journal append-only `state/audit.jsonl`.

    Aucun secret, aucune sortie complète de commande : seulement l'acteur, le
    verbe, la cible et le résultat.
    """
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": _now(),
        "actor": actor,
        "verb": verb,
        "target": target,
        "args": args,
        "result": result,
        "duration_ms": duration_ms,
    }
    if detail:
        entry["detail"] = detail[:300]
    with (directory / "audit.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def plan(tree: Tree) -> list[dict[str, str]]:
    desired = load_desired().get("nodes", {})
    items: list[dict[str, str]] = []
    for node_id in sorted(desired):
        target = str(desired[node_id].get("power", ""))
        node = tree.nodes.get(node_id)
        if node is None:
            items.append({
                "node": node_id, "target": target, "current": "missing",
                "action": "error", "detail": "nœud inconnu",
            })
            continue
        if not is_power_capable(node):
            items.append({
                "node": node_id, "target": target, "current": "n/a",
                "action": "error", "detail": "nœud non pilotable",
            })
            continue
        current = providers.runtime_state(node)
        running = current["state"] in ("running", "up", "degraded")
        if target == "running":
            action = "noop" if running else "start"
        else:
            action = "noop" if not running else "stop"
        items.append({
            "node": node_id,
            "target": target,
            "current": current["state"],
            "action": action,
            "detail": current["detail"],
        })
    items.sort(key=lambda item: (POWER_ORDER.get(tree.nodes[item["node"]].kind, 99) if item["node"] in tree.nodes else 99, item["node"]))
    return items
