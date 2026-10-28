"""Démarrage, arrêt et restauration des ressources.

Deux familles d'actions :

- **VM** (`provider: qemu`) : via `runtime.start` (argv déclaré dans le nœud),
  arrêt gracieux par QMP si `runtime.stop` est absent, sinon `sudo -n`.
  L'arrêt brutal (`--force`) envoie SIGTERM, attend, puis SIGKILL et nettoie
  le pidfile et la socket QMP.
- **Application Docker** (`provider: docker`) : via SSH et des argv fermés
  (`docker start`, `docker stop`, `docker kill`).

Aucune commande n'est construite à partir d'une chaîne de modèle.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import time
from pathlib import Path
from typing import Any

from . import providers, state
from .model import Node
from .tree import Tree

REPO_ROOT = Path(__file__).resolve().parent.parent
BRUTAL_GRACE_S = 8.0
START_WAIT_S = 45.0
STOP_TIMEOUT_S = 220.0


class PowerError(Exception):
    """Échec d'une opération de puissance, avec code stable."""

    def __init__(
        self,
        code: str,
        message: str,
        hint: str | None = None,
        exit_code: int = 5,
        target: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.exit_code = exit_code
        self.target = target


def _tail(text: str, limit: int = 400) -> str:
    values = [line for line in text.strip().splitlines() if line.strip()]
    return " | ".join(values[-6:])[:limit]


def _argv(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if not isinstance(value, str) or not value.strip():
        raise PowerError("invalid_runtime", "Commande runtime absente.", exit_code=2)
    return [os.path.expanduser(part) for part in shlex.split(value)]


# Marqueurs de refus d'authentification. `sudo` est localisé : la version
# française répond « il est nécessaire de saisir un mot de passe », pas
# « password is required ». On reconnaît donc les deux familles.
_PASSWORD_MARKERS = (
    "password is required",
    "a terminal is required",
    "no tty present",
    "not allowed",
    "not permitted",
    "mot de passe est requis",
    "saisir un mot de passe",
    "mot de passe",
    "passwort",
    "contraseña",
)


def _is_password_error(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _PASSWORD_MARKERS)


def _sudo(argv: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """Exécute `sudo -n` sans pré-vérification séparée.

    Tester `sudo -n true` en amont obligerait l'allowlist à contenir `true`,
    une commande inutile à l'exploitation. On lance donc directement la
    commande attendue : une règle exacte suffit, et le refus est classé par
    `_check_sudo_result`.
    """
    try:
        return subprocess.run(
            ["sudo", "-n", *argv], capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError as exc:
        raise PowerError("missing_command", f"Commande absente : {exc.filename}", exit_code=5) from exc
    except subprocess.TimeoutExpired as exc:
        raise PowerError(
            "timeout",
            f"Commande expirée après {int(timeout)} s : {' '.join(argv)}",
            "Vérifier l'état de la VM ; `--force` permet un arrêt brutal.",
            exit_code=8,
        ) from exc


def _check_sudo_result(proc: subprocess.CompletedProcess[str], argv: list[str]) -> None:
    if proc.returncode == 0:
        return
    stderr = (proc.stderr or "") + (proc.stdout or "")
    if _is_password_error(stderr):
        raise PowerError(
            "permission_denied",
            f"sudo a refusé la commande : {' '.join(argv)}",
            "Ajouter une règle sudoers exacte et sans mot de passe pour ce "
            "script (voir docs/security.md) ; `sudo -n true` n'est pas requis.",
            exit_code=6,
        )
    raise PowerError(
        "command_failed",
        f"Échec de {' '.join(argv)} (code {proc.returncode}) : {_tail(stderr)}",
        "Lire les journaux de l'installeur et `modops status`.",
        exit_code=5,
    )


def _runtime(node: Node) -> dict[str, Any]:
    value = node.raw.get("runtime")
    return value if isinstance(value, dict) else {}


def _management(node: Node) -> dict[str, Any]:
    value = node.raw.get("management")
    return value if isinstance(value, dict) else {}


def _running(status_data: dict[str, Any]) -> bool:
    """Vrai si la ressource est encore vivante, même API muette.

    Sert aux décisions d'arrêt (`degraded` doit encore être arrêté) et au
    court-circuit « déjà actif ».
    """
    return status_data.get("state") in ("running", "up", "degraded")


def _healthy(status_data: dict[str, Any]) -> bool:
    """Vrai si la ressource est vivante **et** fonctionnelle.

    `degraded` (conteneur vivant, API muette) n'est pas un démarrage réussi :
    l'appelant ne doit pas annoncer `OK`.
    """
    return status_data.get("state") in ("running", "up")


def _wait_healthy(node: Node, timeout: float = START_WAIT_S) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = providers.runtime_state(node)
    while time.monotonic() < deadline:
        if _healthy(last):
            return last
        time.sleep(2)
        last = providers.runtime_state(node)
    return last


# --------------------------------------------------------------------------
# VM (QEMU)
# --------------------------------------------------------------------------


def _vm_start_command(node: Node) -> list[str]:
    runtime = _runtime(node)
    value = runtime.get("start")
    if not value:
        raise PowerError(
            "missing_runtime",
            f"runtime.start absent pour {node.id}.",
            "Déclarer la commande de démarrage dans node.json.",
            exit_code=5,
            target=node.id,
        )
    return _argv(value)


def _vm_stop_command(node: Node) -> list[str]:
    runtime = _runtime(node)
    value = runtime.get("stop")
    if not value:
        raise PowerError(
            "missing_runtime",
            f"runtime.stop absent pour {node.id}.",
            "Déclarer la commande d'arrêt dans node.json.",
            exit_code=5,
            target=node.id,
        )
    return _argv(value)


def _ensure_vm_dirs(node: Node) -> None:
    """Crée les répertoires des pidfile/QMP avant de lancer QEMU."""
    runtime = _runtime(node)
    parents: set[str] = set()
    for key in ("pid_file", "qmp"):
        value = runtime.get(key)
        if value:
            parents.add(str(Path(str(value)).parent))
    for parent in sorted(parents):
        if parent in ("", "."):
            continue
        path = Path(parent)
        try:
            present = path.exists()
        except OSError:
            present = False
        if not present:
            argv = ["install", "-d", "-m", "0750", parent]
            _check_sudo_result(_sudo(argv, timeout=10), argv)


def _start_vm(node: Node, dry_run: bool) -> dict[str, Any]:
    argv = _vm_start_command(node)
    started = time.monotonic()
    if dry_run:
        return {"dry_run": True, "command": ["sudo", "-n", *argv]}
    _ensure_vm_dirs(node)
    proc = _sudo(argv, timeout=380)
    _check_sudo_result(proc, argv)
    return {
        "command": ["sudo", "-n", *argv],
        "stdout": _tail(proc.stdout),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def _vm_pid(node: Node) -> int | None:
    pid_file = _runtime(node).get("pid_file")
    if not pid_file:
        return None
    path = Path(str(pid_file))
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        pass
    proc = _sudo(["cat", str(path)], timeout=10)
    if proc.returncode != 0:
        return None
    try:
        return int(proc.stdout.strip())
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    proc = _sudo(["kill", "-0", str(pid)], timeout=10)
    return proc.returncode == 0


def _stop_vm_brutal(node: Node) -> dict[str, Any]:
    runtime = _runtime(node)
    pid_file = str(runtime.get("pid_file") or "")
    qmp = str(runtime.get("qmp") or "")
    commands: list[list[str]] = []
    started = time.monotonic()

    pid = _vm_pid(node)
    if pid is None:
        _cleanup_runtime_files(node)
        return {"command": ["cleanup"], "duration_ms": int((time.monotonic() - started) * 1000)}

    term = ["kill", "-TERM", str(pid)]
    _check_sudo_result(_sudo(term, timeout=10), term)
    commands.append(["sudo", "-n", *term])

    deadline = time.monotonic() + BRUTAL_GRACE_S
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(0.5)

    if _pid_alive(pid):
        kill = ["kill", "-KILL", str(pid)]
        _check_sudo_result(_sudo(kill, timeout=10), kill)
        commands.append(["sudo", "-n", *kill])
        time.sleep(0.5)

    cleaned = _cleanup_runtime_files(node)
    if cleaned:
        commands.append(["sudo", "-n", "rm", "-f", "--", pid_file, qmp])
    return {
        "command": commands,
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def _cleanup_runtime_files(node: Node) -> bool:
    runtime = _runtime(node)
    pid_file = runtime.get("pid_file")
    qmp = runtime.get("qmp")
    targets = [str(item) for item in (pid_file, qmp) if item]
    if not targets:
        return False
    proc = _sudo(["rm", "-f", "--", *targets], timeout=10)
    return proc.returncode == 0


def _qmp_path(node: Node) -> str:
    value = str(_runtime(node).get("qmp") or "")
    if value.startswith("unix:"):
        value = value[5:].split(",", 1)[0]
    return value


def _qmp_stop(node: Node) -> dict[str, Any] | None:
    """Arrêt gracieux par QMP ; `None` si la socket ou le PID manque."""
    qmp = _qmp_path(node)
    pid = _vm_pid(node)
    if not qmp or pid is None:
        return None
    if not Path(qmp).exists():
        return None
    started = time.monotonic()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(10)
            client.connect(qmp)
            stream = client.makefile("rwb", buffering=0)
            stream.readline()  # greeting QMP
            for command in ("qmp_capabilities", "system_powerdown"):
                stream.write(json.dumps({"execute": command}).encode("utf-8") + b"\r\n")
                while True:
                    line = stream.readline()
                    if not line:
                        raise OSError("QMP fermé")
                    response = json.loads(line)
                    if "error" in response:
                        raise OSError(str(response["error"]))
                    if "return" in response:
                        break
    except (OSError, ValueError, json.JSONDecodeError):
        return None

    deadline = time.monotonic() + STOP_TIMEOUT_S
    while time.monotonic() < deadline and _pid_alive(pid):
        time.sleep(1)
    if _pid_alive(pid):
        return None
    _cleanup_runtime_files(node)
    return {
        "mode": "graceful",
        "command": ["qmp", "system_powerdown"],
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


def _stop_vm(node: Node, force: bool, dry_run: bool) -> dict[str, Any]:
    if force:
        if dry_run:
            return {
                "dry_run": True,
                "mode": "brutal",
                "command": ["sudo", "-n", "kill", "-TERM", "<pid>", "puis", "-KILL"],
            }
        return {"mode": "brutal", **_stop_vm_brutal(node)}

    runtime = _runtime(node)
    if not runtime.get("stop"):
        if _qmp_path(node) and runtime.get("pid_file"):
            if dry_run:
                return {"dry_run": True, "mode": "graceful", "command": ["qmp", "system_powerdown"]}
            outcome = _qmp_stop(node)
            if outcome is not None:
                return outcome
        if dry_run:
            return {
                "dry_run": True,
                "mode": "brutal",
                "command": ["sudo", "-n", "kill", "-TERM", "<pid>", "puis", "-KILL"],
            }
        return {"mode": "brutal", **_stop_vm_brutal(node)}

    argv = _vm_stop_command(node)
    started = time.monotonic()
    if dry_run:
        return {"dry_run": True, "mode": "graceful", "command": ["sudo", "-n", *argv]}
    proc = _sudo(argv, timeout=STOP_TIMEOUT_S)
    _check_sudo_result(proc, argv)
    return {
        "mode": "graceful",
        "command": ["sudo", "-n", *argv],
        "stdout": _tail(proc.stdout),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


# --------------------------------------------------------------------------
# Applications Docker
# --------------------------------------------------------------------------


def _docker_alias(node: Node) -> str:
    alias = str(
        _management(node).get("ssh_alias")
        or _runtime(node).get("ssh_alias")
        or ""
    )
    if not alias:
        raise PowerError(
            "missing_management",
            f"Alias SSH absent pour {node.id}.",
            "Déclarer management.ssh_alias dans node.json.",
            exit_code=5,
            target=node.id,
        )
    return alias


def _docker_container(node: Node) -> str:
    container = str(
        _management(node).get("container") or _runtime(node).get("container") or ""
    )
    if not container:
        raise PowerError(
            "missing_runtime",
            f"Conteneur absent pour {node.id}.",
            "Déclarer runtime.container dans node.json.",
            exit_code=5,
            target=node.id,
        )
    return container


def _docker(node: Node, argv: list[str], timeout: float = 60.0) -> dict[str, Any]:
    alias = _docker_alias(node)
    started = time.monotonic()
    try:
        proc = providers.ssh_run(alias, argv, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise PowerError(
            "timeout",
            f"SSH {alias} expiré après {int(timeout)} s.",
            "Vérifier la VM et le réseau.",
            exit_code=8,
            target=node.id,
        ) from exc
    except FileNotFoundError as exc:
        raise PowerError("missing_command", "ssh absent sur l'hôte.", exit_code=5) from exc
    if proc.returncode != 0:
        raise PowerError(
            "command_failed",
            f"Échec sur {alias} : {' '.join(argv)} (code {proc.returncode}) : {_tail(proc.stderr)}",
            "Vérifier `docker ps` et les journaux du conteneur.",
            exit_code=5,
            target=node.id,
        )
    return {
        "command": ["ssh", alias, *argv],
        "stdout": _tail(proc.stdout),
        "duration_ms": int((time.monotonic() - started) * 1000),
    }


# --------------------------------------------------------------------------
# API publique
# --------------------------------------------------------------------------


def start_node(tree: Tree, node: Node, dry_run: bool = False) -> dict[str, Any]:
    before = providers.runtime_state(node)
    if _running(before) and not dry_run:
        return {
            "node": node.id,
            "action": "start",
            "changed": False,
            "ok": True,
            "before": before,
            "after": before,
            "detail": "déjà actif",
        }

    if node.provider == "qemu":
        outcome = _start_vm(node, dry_run)
    elif node.provider == "docker":
        container = _docker_container(node)
        outcome = (
            {"dry_run": True, "command": ["ssh", _docker_alias(node), "docker", "start", container]}
            if dry_run
            else _docker(node, ["docker", "start", container])
        )
    else:
        raise PowerError(
            "unsupported",
            f"start non supporté pour le provider {node.provider}.",
            "Seuls qemu et docker sont pilotables en v1.",
            exit_code=3,
            target=node.id,
        )

    after = before if dry_run else _wait_healthy(node)
    ok = True if dry_run else _healthy(after)
    result: dict[str, Any] = {
        "node": node.id,
        "action": "start",
        "dry_run": dry_run,
        "changed": True,
        "ok": ok,
        "before": before,
        "after": after,
        "outcome": outcome,
    }
    if not ok and after.get("state") == "degraded":
        result["detail"] = "conteneur vivant mais API muette ; vérifier les journaux"
    return result


def stop_node(tree: Tree, node: Node, force: bool = False, dry_run: bool = False) -> dict[str, Any]:
    before = providers.runtime_state(node)
    if not _running(before) and not dry_run:
        return {
            "node": node.id,
            "action": "stop",
            "force": force,
            "changed": False,
            "ok": True,
            "before": before,
            "after": before,
            "detail": "déjà arrêté",
        }

    if node.provider == "qemu":
        outcome = _stop_vm(node, force, dry_run)
    elif node.provider == "docker":
        container = _docker_container(node)
        argv = ["docker", "kill", container] if force else ["docker", "stop", "--time", "15", container]
        outcome = (
            {"dry_run": True, "command": ["ssh", _docker_alias(node), *argv]}
            if dry_run
            else _docker(node, argv, timeout=90)
        )
    else:
        raise PowerError(
            "unsupported",
            f"stop non supporté pour le provider {node.provider}.",
            "Seuls qemu et docker sont pilotables en v1.",
            exit_code=3,
            target=node.id,
        )

    after = before
    if not dry_run:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            after = providers.runtime_state(node)
            if not _running(after):
                break
            time.sleep(1)
    return {
        "node": node.id,
        "action": "stop",
        "dry_run": dry_run,
        "force": force,
        "changed": True,
        "ok": True if dry_run else not _running(after),
        "before": before,
        "after": after,
        "outcome": outcome,
    }


def start_tree(tree: Tree, dry_run: bool = False) -> list[dict[str, Any]]:
    """Démarre tous les nœuds pilotables, des VMs vers les applications."""
    order = {"bridge": 0, "vm": 1, "container": 2, "app": 3}
    nodes = [node for node in tree.nodes.values() if state.is_power_capable(node)]
    nodes.sort(key=lambda node: (order.get(node.kind, 9), len(node.path.parts), node.id))

    results: list[dict[str, Any]] = []
    for node in nodes:
        try:
            results.append(start_node(tree, node, dry_run=dry_run))
        except PowerError as error:
            results.append({
                "node": node.id,
                "action": "start",
                "ok": False,
                "error": error.code,
                "detail": error.message,
            })
    if not dry_run:
        state.save_observed(tree)
    return results


def restore(tree: Tree, dry_run: bool = False) -> list[dict[str, Any]]:
    """Rejoue l'état désiré : arrêts d'abord, puis démarrages.

    L'ordre est : applications avant VMs à l'arrêt ; bridge, VMs puis
    applications au démarrage. Un échec n'interrompt pas la restauration : il
    est collecté et signalé.
    """
    items = state.plan(tree)
    order = {"app": 0, "container": 1, "vm": 2, "bridge": 3}
    to_stop = sorted(
        [item for item in items if item["action"] == "stop"],
        key=lambda item: order.get(tree.nodes[item["node"]].kind, 9),
    )
    to_start = sorted(
        [item for item in items if item["action"] == "start"],
        key=lambda item: {"bridge": 0, "vm": 1, "container": 2, "app": 3}.get(
            tree.nodes[item["node"]].kind, 9
        ),
    )

    results: list[dict[str, Any]] = []
    for item in to_stop:
        node = tree.nodes[item["node"]]
        try:
            results.append(stop_node(tree, node, dry_run=dry_run))
        except PowerError as error:
            results.append({"node": node.id, "action": "stop", "ok": False, "error": error.code, "detail": error.message})
    for item in to_start:
        node = tree.nodes[item["node"]]
        try:
            results.append(start_node(tree, node, dry_run=dry_run))
        except PowerError as error:
            results.append({"node": node.id, "action": "start", "ok": False, "error": error.code, "detail": error.message})

    if not dry_run:
        state.save_observed(tree)
    return results
