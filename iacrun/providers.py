"""Providers d'observation : local, qemu, ssh, docker, http.

Aucun shell piloté par un modèle : les commandes sont des argv construits ici.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .model import Node


def _elapsed(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _tail(text: str, limit: int = 300) -> str:
    text = " ".join(text.strip().split())
    return text[:limit] if text else ""


def _run(argv: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _http_health(url: str, timeout: float, expect: int | None = None) -> dict[str, Any]:
    start = time.monotonic()
    request = urllib.request.Request(url, headers={"User-Agent": "iac-run/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            code = int(response.status)
        ok = (code == expect) if expect is not None else (code < 400)
        return {"ok": ok, "type": "http", "detail": f"HTTP {code}", "latency_ms": _elapsed(start)}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "type": "http", "detail": f"HTTP {exc.code}", "latency_ms": _elapsed(start)}
    except Exception as exc:  # timeout, DNS, connexion refusée
        return {
            "ok": False,
            "type": "http",
            "detail": f"{type(exc).__name__}: {exc}",
            "latency_ms": _elapsed(start),
        }


def _command_health(argv: list[str], timeout: float) -> dict[str, Any]:
    start = time.monotonic()
    try:
        proc = _run(argv, timeout=timeout)
    except FileNotFoundError:
        return {"ok": False, "type": "command", "detail": f"commande absente : {argv[0]}", "latency_ms": _elapsed(start)}
    except subprocess.TimeoutExpired:
        return {"ok": False, "type": "command", "detail": "timeout", "latency_ms": _elapsed(start)}
    detail = _tail(proc.stdout) or _tail(proc.stderr) or f"exit {proc.returncode}"
    return {"ok": proc.returncode == 0, "type": "command", "detail": detail, "latency_ms": _elapsed(start)}


def _ssh(
    alias: str,
    argv: list[str],
    timeout: float = 5.0,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    if shutil.which("ssh") is None:
        raise FileNotFoundError("ssh")
    command = " ".join(shlex.quote(str(arg)) for arg in argv)
    return subprocess.run(
        [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=2",
            "-o", "StrictHostKeyChecking=yes",
            "-o", "LogLevel=ERROR",
            alias,
            command,
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        input=stdin,
    )


def ssh_run(
    alias: str,
    argv: list[str],
    timeout: float = 5.0,
    stdin: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Exécute un argv fermé sur une VM, via un alias SSH validé.

    Utilisé par `power.py`. Les commandes restent des argv
    construits par le code, jamais une chaîne issue d'un modèle.
    """
    return _ssh(alias, argv, timeout=timeout, stdin=stdin)


def _management(node: Node) -> dict[str, Any]:
    value = node.raw.get("management")
    return value if isinstance(value, dict) else {}


def health(node: Node) -> dict[str, Any]:
    spec = node.health
    kind = spec.get("type")
    timeout = float(spec.get("timeout_s", 5))

    if kind == "http":
        url = str(spec.get("url", ""))
        if not url:
            return {"ok": False, "type": "http", "detail": "health.url absent", "latency_ms": 0}
        return _http_health(url, timeout, spec.get("expect"))

    if kind == "command":
        argv = spec.get("argv")
        if not isinstance(argv, list) or not argv:
            run = spec.get("run")
            argv = ["sh", "-c", str(run)] if run else None
        if not argv:
            return {"ok": False, "type": "command", "detail": "health.argv absent", "latency_ms": 0}
        return _command_health([str(arg) for arg in argv], timeout)

    if kind == "ssh":
        alias = str(spec.get("ssh_alias") or _management(node).get("ssh_alias") or "")
        argv = spec.get("argv") or ["true"]
        if not alias:
            return {"ok": False, "type": "ssh", "detail": "alias SSH absent", "latency_ms": 0}
        start = time.monotonic()
        try:
            proc = _ssh(alias, [str(arg) for arg in argv], timeout=timeout)
        except Exception as exc:
            return {"ok": False, "type": "ssh", "detail": f"{type(exc).__name__}: {exc}", "latency_ms": _elapsed(start)}
        detail = _tail(proc.stdout) or _tail(proc.stderr) or f"exit {proc.returncode}"
        return {"ok": proc.returncode == 0, "type": "ssh", "detail": detail, "latency_ms": _elapsed(start)}

    if kind == "docker":
        alias = str(spec.get("ssh_alias") or _management(node).get("ssh_alias") or "")
        container = str(spec.get("container") or _management(node).get("container") or "")
        if not alias or not container:
            return {"ok": False, "type": "docker", "detail": "alias ou conteneur absent", "latency_ms": 0}
        start = time.monotonic()
        try:
            proc = _ssh(
                alias,
                ["docker", "inspect", "--format", "{{.State.Status}}", container],
                timeout=timeout,
            )
        except Exception as exc:
            return {"ok": False, "type": "docker", "detail": f"{type(exc).__name__}: {exc}", "latency_ms": _elapsed(start)}
        status = proc.stdout.strip()
        ok = proc.returncode == 0 and status in ("running", "restarting")
        detail = status or _tail(proc.stderr) or f"exit {proc.returncode}"
        return {"ok": ok, "type": "docker", "detail": detail, "latency_ms": _elapsed(start)}

    return {"ok": False, "type": str(kind or "none"), "detail": "aucun test de santé", "latency_ms": 0}


def _pid_from_file(path: Path) -> tuple[int | None, str | None]:
    """Lit un pidfile, y compris quand il appartient à root.

    Un pidfile peut être dans un répertoire protégé par root. On tente alors
    la lecture privilégiée via l'allowlist sudo configurée pour le déploiement.

    Retourne `(pid, erreur)` ; `(None, None)` signifie « fichier absent ».
    """
    try:
        present = path.exists()
    except OSError:
        present = True  # on ne peut pas savoir : tenter la lecture privilégiée
    if not present:
        return None, None
    try:
        return int(path.read_text(encoding="utf-8").strip()), None
    except PermissionError:
        pass
    except (OSError, ValueError):
        return None, f"pidfile illisible : {path}"
    try:
        proc = _run(["sudo", "-n", "cat", str(path)], timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        return None, f"pidfile illisible (droits root) : {path}"
    if proc.returncode != 0:
        detail = f"{proc.stderr or ''} {proc.stdout or ''}".lower()
        # Course normale : `stop.sh` supprime le pidfile entre `exists()` et
        # `cat`. Ce n'est pas une erreur de droits.
        if "no such file" in detail or "aucun fichier" in detail:
            return None, None
        try:
            if not path.exists():
                return None, None
        except OSError:
            pass
        return None, f"pidfile illisible (droits root) : {path}"
    try:
        return int(proc.stdout.strip()), None
    except ValueError:
        return None, f"pidfile illisible : {path}"


def _qemu_status(node: Node) -> dict[str, Any]:
    runtime = node.raw.get("runtime", {})
    pid_file = runtime.get("pid_file")
    if not pid_file:
        return {"state": "unknown", "detail": "pid_file absent", "provider": "qemu"}
    path = Path(str(pid_file))
    pid, error = _pid_from_file(path)
    if error:
        return {"state": "unknown", "detail": error, "provider": "qemu"}
    if pid is None:
        probe = health(node)
        if probe["ok"]:
            return {"state": "running", "detail": "pidfile absent mais santé OK", "provider": "qemu"}
        return {"state": "stopped", "detail": "pidfile absent", "provider": "qemu"}
    proc = Path(f"/proc/{pid}")
    if not proc.exists():
        return {"state": "stale", "detail": f"pid {pid} absent", "provider": "qemu", "pid": pid}
    try:
        cmdline = (proc / "cmdline").read_bytes().replace(b"\x00", b" ").decode(errors="replace")
    except OSError:
        cmdline = ""
    if "qemu-system" not in cmdline:
        return {"state": "stale", "detail": f"pid {pid} n'est pas QEMU", "provider": "qemu", "pid": pid}
    return {"state": "running", "detail": f"pid {pid}", "provider": "qemu", "pid": pid}


def status(node: Node) -> dict[str, Any]:
    if node.kind == "site":
        return {"state": "up", "detail": "arborescence chargée", "provider": "local"}

    if node.provider == "qemu":
        return _qemu_status(node)

    if node.provider == "docker":
        # `status` répond « le conteneur tourne-t-il ? », pas seulement
        # « l'API répond-elle ? ». Voir `runtime_state`.
        return runtime_state(node)

    if node.provider in ("http", "ssh"):
        probe = health(node)
        state = "up" if probe["ok"] else "down"
        if "HTTP 5" in probe["detail"]:
            state = "degraded"
        return {"state": state, "detail": probe["detail"], "provider": node.provider}

    if node.provider == "local":
        if not node.health:
            return {"state": "unknown", "detail": "aucun test de santé", "provider": "local"}
        probe = health(node)
        return {
            "state": "up" if probe["ok"] else "down",
            "detail": probe["detail"],
            "provider": "local",
        }

    return {"state": "unknown", "detail": f"provider {node.provider}", "provider": node.provider}


def runtime_state(node: Node) -> dict[str, Any]:
    """État de **puissance** d'un nœud, distinct de la santé applicative.

    Pour Docker, on interroge d'abord la santé (rapide). Si elle échoue, on
    demande à `docker inspect` si le conteneur tourne encore : un conteneur
    actif avec une API muette est `degraded`, pas `stopped`.
    """
    if node.provider != "docker":
        return status(node)

    alias = str(_management(node).get("ssh_alias") or "")
    container = str(
        _management(node).get("container")
        or node.raw.get("runtime", {}).get("container")
        or ""
    )
    probe = health(node)
    if probe["ok"]:
        return {"state": "running", "detail": probe["detail"], "provider": "docker"}
    if not alias or not container:
        return {"state": "unknown", "detail": "alias SSH ou conteneur absent", "provider": "docker"}

    start = time.monotonic()
    try:
        proc = _ssh(
            alias,
            ["docker", "inspect", "--format", "{{.State.Status}}", container],
            timeout=4,
        )
    except Exception as exc:
        return {
            "state": "unreachable",
            "detail": f"{type(exc).__name__}: {exc}",
            "provider": "docker",
        }
    if proc.returncode != 0:
        detail = _tail(proc.stderr) or "docker inspect en échec"
        if proc.returncode == 255 or "Connection" in detail or "timed out" in detail:
            return {
                "state": "unreachable",
                "detail": detail,
                "provider": "docker",
                "latency_ms": _elapsed(start),
            }
        state = "stopped" if "No such object" in detail else "unknown"
        return {
            "state": state,
            "detail": detail,
            "provider": "docker",
            "latency_ms": _elapsed(start),
        }
    value = proc.stdout.strip()
    mapping = {
        "running": "running",
        "restarting": "running",
        "paused": "paused",
        "exited": "stopped",
        "created": "stopped",
        "dead": "stopped",
        "removing": "stopped",
    }
    state = mapping.get(value, "unknown")
    detail = f"conteneur {value}"
    if state == "running":
        # Le conteneur tourne mais l'API ne répond pas : le processus est là,
        # le service n'est pas fonctionnel. C'est `degraded`, pas `stopped`.
        state = "degraded"
        detail += f" ; santé : {probe['detail']}"
    return {"state": state, "detail": detail, "provider": "docker", "latency_ms": _elapsed(start)}


def logs(node: Node, lines: int = 40) -> dict[str, Any]:
    lines = max(1, min(int(lines), 500))

    if node.provider == "docker":
        alias = str(_management(node).get("ssh_alias") or "")
        container = str(_management(node).get("container") or node.raw.get("runtime", {}).get("container") or "")
        if not alias or not container:
            return {"ok": False, "detail": "alias SSH ou conteneur absent"}
        try:
            proc = _ssh(alias, ["docker", "logs", "--tail", str(lines), container], timeout=10)
        except Exception as exc:
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        return {"ok": proc.returncode == 0, "detail": (proc.stdout + proc.stderr).strip()[-8000:]}

    if node.provider == "ssh":
        alias = str(node.health.get("ssh_alias") or _management(node).get("ssh_alias") or "")
        unit = str(node.raw.get("runtime", {}).get("unit") or "")
        if not alias or not unit:
            return {"ok": False, "detail": "alias SSH ou unité systemd absent"}
        try:
            proc = _ssh(alias, ["journalctl", "-u", unit, "-n", str(lines), "--no-pager"], timeout=10)
        except Exception as exc:
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
        return {"ok": proc.returncode == 0, "detail": (proc.stdout + proc.stderr).strip()[-8000:]}

    return {
        "ok": False,
        "detail": f"logs non supportés pour le provider {node.provider}",
        "hint": "Utiliser le provider ssh/docker ou lire les journaux de l'hôte.",
    }
