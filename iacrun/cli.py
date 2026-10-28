"""CLI `modops` — API homogène, lecture seule en v0."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any

from . import move as mover
from . import power, providers, scaffold, state
from .model import Node
from .tree import Tree, TreeError

DEFAULT_ROOT = Path(__file__).resolve().parent.parent / "tree" / "home"
REPO_ROOT = Path(__file__).resolve().parent.parent


def _actor() -> str:
    try:
        return getpass.getuser()
    except Exception:  # environnement sans passwd
        return "unknown"


class IacError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        hint: str | None = None,
        exit_code: int = 9,
        target: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.exit_code = exit_code
        self.target = target


def _extract_globals(argv: list[str]) -> tuple[list[str], bool, str | None]:
    json_mode = False
    root: str | None = None
    rest: list[str] = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--json":
            json_mode = True
        elif arg == "--root":
            if index + 1 >= len(argv):
                raise IacError("usage", "--root attend une valeur.", exit_code=2)
            root = argv[index + 1]
            index += 1
        elif arg.startswith("--root="):
            root = arg.split("=", 1)[1]
        else:
            rest.append(arg)
        index += 1
    return rest, json_mode, root


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="modops",
        description="Administration d'une infra virtualisée via une arborescence.",
    )
    sub = parser.add_subparsers(dest="verb", required=True)

    p = sub.add_parser("ls", help="liste les enfants d'un nœud")
    p.add_argument("path", nargs="?")
    p.add_argument("--depth", type=int, default=1)

    p = sub.add_parser("show", help="affiche un nœud")
    p.add_argument("path")
    p.add_argument("--card", action="store_true")

    p = sub.add_parser("path", help="imprime le chemin canonique")
    p.add_argument("id")

    p = sub.add_parser("status", help="état observé")
    p.add_argument("path", nargs="?")
    p.add_argument("--recursive", action="store_true", help="inclut les descendants du nœud")

    p = sub.add_parser("health", help="test de santé")
    p.add_argument("path", nargs="?")

    p = sub.add_parser("logs", help="journaux récents")
    p.add_argument("path")
    p.add_argument("-n", "--lines", type=int, default=40)

    p = sub.add_parser("tree", help="rendu ASCII")
    p.add_argument("path", nargs="?")

    p = sub.add_parser("index", help="index compact pour le coordinateur")

    p = sub.add_parser("context", help="contexte exact d'un opérateur de nœud")
    p.add_argument("path")
    p.add_argument("--observation", default="aucune (utiliser status/health)")
    p.add_argument("--request", default="(aucune demande : contexte seul)")

    p = sub.add_parser("doctor", help="valide l'arborescence")

    p = sub.add_parser("new", help="crée un nœud sous un parent")
    p.add_argument("parent")
    p.add_argument("--spec", type=Path, required=True, help="spécification JSON du nœud (- pour stdin)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("render", help="matérialise une infra depuis une spécification")
    p.add_argument("spec", type=Path, help="fichier JSON (- pour stdin)")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("ask", help="route une demande en langage naturel")
    p.add_argument("text", nargs="+")

    p = sub.add_parser("move", help="déplace un nœud vers un autre parent (déclaratif)")
    p.add_argument("path")
    p.add_argument("--to", required=True)
    p.add_argument("--address")
    p.add_argument("--port", type=int)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")

    for name in ("start", "stop", "restart"):
        p = sub.add_parser(name, help=f"{name} une ressource")
        p.add_argument("path")
        p.add_argument("--force", action="store_true")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--yes", action="store_true")

    state_parser = sub.add_parser("state", help="état désiré, observé et plan")
    state_sub = state_parser.add_subparsers(dest="state_action", required=True)
    state_sub.add_parser("show", help="état désiré + observé + plan")
    state_sub.add_parser("save", help="observe et enregistre l'état")
    reset = state_sub.add_parser("reset", help="efface l'état désiré")
    reset.add_argument("path", nargs="?")
    state_sub.add_parser("plan", help="diff entre désiré et observé")
    set_power = state_sub.add_parser("set", help="déclare la puissance désirée")
    set_power.add_argument("path")
    set_power.add_argument("--power", choices=("running", "stopped"), required=True)

    p = sub.add_parser("restore", help="rejoue l'état désiré")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")

    net = sub.add_parser("net", help="opérations réseau")
    net_sub = net.add_subparsers(dest="net_action", required=True)
    net_sub.add_parser("list", help="liste réseaux et bridges")
    create = net_sub.add_parser("create", help="crée un réseau (v1)")
    create.add_argument("id")
    create.add_argument("--cidr", required=True)
    create.add_argument("--gateway")
    create.add_argument("--bridge")
    create.add_argument("--dry-run", action="store_true")
    create.add_argument("--yes", action="store_true")
    for name in ("attach", "detach", "move"):
        p = net_sub.add_parser(name, help=f"{name} un membre (v1)")
        p.add_argument("path")
        if name == "move":
            p.add_argument("--to", required=True)
        else:
            p.add_argument("--network", required=True)
        p.add_argument("--address")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--yes", action="store_true")

    p = sub.add_parser("connect", help="déclare un lien entre services (v1)")
    p.add_argument("path")
    p.add_argument("--to", required=True)
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--protocol", default="http")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")

    p = sub.add_parser("disconnect", help="retire un lien déclaré (v1)")
    p.add_argument("path")
    p.add_argument("--to", required=True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--yes", action="store_true")

    return parser


def _endpoint(node: Node) -> str:
    return node.endpoint


def _human_ls(tree: Tree, node: Node, depth: int) -> str:
    rows: list[str] = []
    if depth <= 1:
        children = tree.children(node.id)
        for child in children:
            rows.append(
                f"{child.id:<22} {child.kind:<10} {_endpoint(child):<22} {child.summary}"
            )
    else:
        for child, level in tree.children_recursive(node.id, depth):
            indent = "  " * (level - 1)
            rows.append(
                f"{indent}{child.id:<22} {child.kind:<10} {_endpoint(child):<22} {child.summary}"
            )
    return "\n".join(rows) if rows else "(aucun enfant)"


def _link_text(link: dict[str, Any], by_uuid: dict[str, Node]) -> str:
    label = link.get("label")
    if not label:
        target = by_uuid.get(str(link.get("to")))
        label = target.id if target else str(link.get("to"))
    if link.get("port"):
        return f"{label}:{link['port']}"
    return str(label)


def _human_show(tree: Tree, node: Node, card: bool) -> str:
    lines = [
        f"id:        {node.id}",
        f"uuid:      {node.uuid or '—'}",
        f"title:     {node.title}",
        f"kind:      {node.kind}",
        f"provider:  {node.provider}",
        f"risk:      {node.risk}",
        f"path:      {node.path}",
        f"parent:    {node.parent or '—'}",
        f"endpoint:  {_endpoint(node) or '—'}",
        f"verbs:     {', '.join(node.verbs) or '—'}",
    ]
    children = tree.children(node.id)
    if children:
        lines.append(f"children:  {', '.join(child.id for child in children)}")
    links = node.raw.get("links", [])
    if links:
        by_uuid = {candidate.uuid: candidate for candidate in tree.nodes.values() if candidate.uuid}
        rendered = ", ".join(
            _link_text(link, by_uuid) for link in links
        )
        lines.append(f"links:     {rendered}")
    if node.health:
        spec = node.health
        detail = spec.get("url") or " ".join(str(arg) for arg in spec.get("argv", [])) or spec.get("container") or ""
        lines.append(f"health:    {spec.get('type')} {detail}".rstrip())
    lines.append(f"card:      {node.card_path}")
    if card:
        lines.append("")
        lines.append(node.card().rstrip())
    return "\n".join(lines)


def _human_net(tree: Tree) -> str:
    rows: list[str] = []
    for node in sorted(tree.nodes.values(), key=lambda n: n.id):
        if node.kind == "network":
            rows.append(f"{node.id:<22} network  {node.raw.get('cidr', ''):<18} gw {node.raw.get('gateway', '—')}")
    for node in sorted(tree.nodes.values(), key=lambda n: n.id):
        if node.kind == "bridge":
            taps = ", ".join(node.raw.get("runtime", {}).get("taps", []))
            rows.append(f"{node.id:<22} bridge   {_endpoint(node):<18} taps {taps or '—'}")
    return "\n".join(rows) if rows else "(aucun réseau)"


def _net_data(tree: Tree) -> dict[str, Any]:
    networks = []
    for node in sorted(tree.nodes.values(), key=lambda n: n.id):
        if node.kind != "network":
            continue
        members = [
            other.id
            for other in tree.nodes.values()
            if any(str(address.get("network")) == node.id for address in other.addresses)
        ]
        networks.append({
            "id": node.id,
            "cidr": node.raw.get("cidr"),
            "gateway": node.raw.get("gateway"),
            "dns": node.raw.get("dns", []),
            "members": sorted(members),
        })
    bridges = []
    for node in sorted(tree.nodes.values(), key=lambda n: n.id):
        if node.kind != "bridge":
            continue
        bridges.append({
            "id": node.id,
            "address": _endpoint(node),
            "taps": node.raw.get("runtime", {}).get("taps", []),
            "members": [child.id for child in tree.children(node.id)],
        })
    return {"networks": networks, "bridges": bridges}


def _index_data(tree: Tree) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for node in sorted(tree.nodes.values(), key=lambda n: (len(n.path.parts), n.id)):
        rows.append({
            "id": node.id,
            "kind": node.kind,
            "parent": node.parent or "",
            "endpoint": node.endpoint,
            "summary": node.summary,
        })
    return rows


def _context(
    tree: Tree,
    node: Node,
    observation: str,
    request: str = "(aucune demande : contexte seul)",
) -> tuple[str, int, int, int]:
    template = (REPO_ROOT / "agent" / "prompts" / "node-operator.md").read_text(encoding="utf-8")
    children = "\n".join(
        f"- `{child.id}` ({child.kind}) : {child.summary}"
        for child in tree.children(node.id)
    ) or "- aucun"
    prompt = (
        template
        .replace("{{NODE_ID}}", node.id)
        .replace("{{NODE_CARD}}", node.card().strip())
        .replace("{{NODE_JSON}}", json.dumps(node.raw, ensure_ascii=False, indent=2))
        .replace("{{CHILDREN}}", children)
        .replace("{{VERBS}}", ", ".join(f"`{verb}`" for verb in node.verbs) or "aucun")
        .replace("{{OBSERVATION}}", observation)
        .replace("{{REQUEST}}", request)
    )
    budget = int(node.raw.get("ai", {}).get("context_budget_tokens", 1800))
    chars = len(prompt)
    tokens = (chars + 3) // 4
    return prompt, chars, tokens, budget


def _not_implemented(node: Node, verb: str, plan: str) -> None:
    raise IacError(
        "not_implemented",
        f"Le verbe {verb} n'est pas implémenté dans cette version.",
        hint=plan,
        exit_code=3,
        target=node.id,
    )


def _read(tree: Tree, verb: str, node: Node) -> tuple[dict[str, Any], str]:
    if verb == "path":
        return {"path": str(node.path)}, str(node.path)
    if verb == "status":
        data = providers.status(node)
        return data, f"{node.id}: {data['state']} — {data['detail']}"
    if verb == "health":
        data = providers.health(node)
        verdict = "up" if data["ok"] else "down"
        return data, f"{node.id}: {verdict} ({data['type']}) — {data['detail']} ({data['latency_ms']} ms)"
    if verb == "show":
        data = {
            "node": node.effective,
            "path": str(node.path),
            "card": node.card(),
            "children": [
                {"id": child.id, "kind": child.kind, "summary": child.summary}
                for child in tree.children(node.id)
            ],
        }
        return data, _human_show(tree, node, card=False)
    raise IacError("usage", f"Verbe de lecture inconnu : {verb}", exit_code=2, target=node.id)


def _status_recursive(tree: Tree, node: Node) -> tuple[dict[str, Any], str]:
    """État observé du nœud et de tous ses descendants."""
    desired = state.load_desired().get("nodes") or {}
    nodes = [node] + [child for child, _level in tree.children_recursive(node.id, 64)]
    rows: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for current in nodes:
        observed: dict[str, Any] = {}
        try:
            observed = providers.status(current)
            observed_state = str(observed.get("state") or "unknown")
            detail = str(observed.get("detail") or "")
        except Exception as error:  # un provider indisponible ne bloque pas le reste
            observed_state = "error"
            detail = str(error)
        counts[observed_state] = counts.get(observed_state, 0) + 1
        pid = observed.get("pid")
        if not isinstance(pid, int):
            match = re.search(r"\bpid (\d+)\b", detail)
            pid = int(match.group(1)) if match else None
        rows.append(
            {
                "id": current.id,
                "kind": current.kind,
                "title": current.title,
                "endpoint": current.endpoint or None,
                "declared": str((desired.get(current.id) or {}).get("power") or "—"),
                "state": observed_state,
                "pid": pid,
                "port": _node_port(current),
                "detail": detail,
                "provider": current.provider,
            }
        )
    lines = [
        f"{'NODE':<20} {'KIND':<10} {'ENDPOINT':<18} {'DÉCLARÉ':<9} "
        f"{'RÉEL':<10} {'PID':<6} {'PORT':<5} DÉTAIL"
    ]
    for row in rows:
        lines.append(
            f"{row['id']:<20} {row['kind']:<10} {str(row['endpoint'] or '—'):<18} "
            f"{row['declared']:<9} {row['state']:<10} "
            f"{str(row['pid'] if row['pid'] is not None else '—'):<6} "
            f"{str(row['port'] if row['port'] is not None else '—'):<5} {row['detail']}"
        )
    summary = ", ".join(f"{count} {name}" for name, count in sorted(counts.items()))
    lines += ["", f"{len(rows)} nœuds — {summary}"]
    down = sum(counts.get(name, 0) for name in ("down", "stopped", "error"))
    data = {
        "node": node.id,
        "recursive": True,
        "nodes": rows,
        "counts": counts,
        "down": down,
    }
    return data, "\n".join(lines)


def _node_port(node: Node) -> int | None:
    for address in node.addresses:
        if address.get("port"):
            try:
                return int(address["port"])
            except (TypeError, ValueError):
                continue
    runtime = node.raw.get("runtime")
    if isinstance(runtime, dict):
        match = re.search(r":(\d+)->", str(runtime.get("bind") or ""))
        if match:
            return int(match.group(1))
    return None


def _read_spec(path: Path, what: str) -> dict[str, Any]:
    """Lit une spécification JSON depuis un fichier ou stdin (`-`)."""
    if str(path) == "-":
        label = "stdin"
        text = sys.stdin.read()
    else:
        expanded = path.expanduser()
        label = str(expanded)
        if not expanded.is_file():
            raise IacError("not_found", f"Spécification {what} introuvable : {expanded}.", exit_code=2)
        text = expanded.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as error:
        raise IacError("invalid_spec", f"JSON invalide dans {label} : {error}.", exit_code=2)
    if not isinstance(data, dict):
        raise IacError("invalid_spec", f"La spécification {what} doit être un objet JSON.", exit_code=2)
    return data


def _ask_confirmation(
    label: str,
    args: argparse.Namespace,
    required_id: str | None = None,
    target: str | None = None,
) -> None:
    """Confirme une action mutante.

    - `--dry-run` : aucune confirmation ;
    - `--yes` : confirmation explicite non interactive ;
    - terminal : saisie de l'id pour `risk: high`, sinon o/N ;
    - sinon : refus `confirmation_required`.
    """
    if getattr(args, "dry_run", False) or getattr(args, "yes", False):
        return
    if not sys.stdin.isatty():
        raise IacError(
            "confirmation_required",
            f"{label} exige --yes en mode non interactif.",
            "Utiliser --dry-run pour voir le plan, puis --yes pour appliquer.",
            exit_code=5,
            target=target,
        )
    if required_id:
        answer = input(f"Taper {required_id} pour confirmer « {label} » : ")
        confirmed = answer.strip() == required_id
    else:
        answer = input(f"Confirmer « {label} » ? [o/N] ")
        confirmed = answer.strip().lower() in ("o", "oui", "y", "yes")
    if not confirmed:
        raise IacError("cancelled", "Action annulée.", exit_code=5, target=target)


def _human_plan(items: list[dict[str, str]]) -> str:
    if not items:
        return "plan vide — aucun état désiré (voir `modops state set`)."
    lines = [f"{'NODE':<22} {'CIBLE':<9} {'ACTUEL':<10} ACTION"]
    for item in items:
        lines.append(
            f"{item['node']:<22} {item['target']:<9} {item['current']:<10} {item['action']} — {item['detail']}"
        )
    return "\n".join(lines)


def _human_mutation(result: dict[str, Any]) -> str:
    node = result.get("node", "?")
    action = result.get("action", "?")
    if result.get("dry_run"):
        command = result.get("outcome", {}).get("command") or result.get("command")
        if isinstance(command, list):
            command = " ".join(str(part) for part in command)
        return f"DRY-RUN {action} {node} : {command}"
    if result.get("error"):
        return f"ÉCHEC {action} {node} [{result['error']}] {result.get('detail', '')}"
    if result.get("changed") is False:
        return f"{node} : {action} sans effet ({result.get('detail', '')})"
    verdict = "OK" if result.get("ok") else "ÉCHEC"
    after = result.get("after") or {}
    return f"{verdict} {action} {node} : {after.get('state', '?')} — {after.get('detail', '')}"


def _dispatch(tree: Tree, args: argparse.Namespace) -> tuple[dict[str, Any], str, str | None, int]:
    verb: str = args.verb
    target: str | None = None

    if verb == "ls":
        node = tree.resolve(args.path) if args.path else tree.default_node()
        target = node.id
        depth = max(1, min(int(args.depth), 8))
        children = [
            {"id": child.id, "kind": child.kind, "endpoint": child.endpoint, "summary": child.summary}
            for child, _level in (
                tree.children_recursive(node.id, depth)
                if depth > 1
                else [(child, 1) for child in tree.children(node.id)]
            )
        ]
        return {"node": node.id, "depth": depth, "children": children}, _human_ls(tree, node, depth), target, 0

    if verb == "show":
        node = tree.resolve(args.path)
        target = node.id
        data = {
            "node": node.effective,
            "path": str(node.path),
            "card": node.card(),
            "children": [
                {"id": child.id, "kind": child.kind, "summary": child.summary}
                for child in tree.children(node.id)
            ],
        }
        return data, _human_show(tree, node, card=args.card), target, 0

    if verb == "path":
        node = tree.resolve(args.id)
        target = node.id
        data, human = _read(tree, verb, node)
        return data, human, target, 0

    if verb in ("status", "health"):
        node = tree.resolve(args.path) if getattr(args, "path", None) else tree.default_node()
        target = node.id
        if verb == "status" and getattr(args, "recursive", False):
            data, human = _status_recursive(tree, node)
            return data, human, target, 0
        data, human = _read(tree, verb, node)
        exit_code = 0
        if verb == "health" and not data.get("ok"):
            exit_code = 7
        return data, human, target, exit_code

    if verb == "logs":
        node = tree.resolve(args.path)
        target = node.id
        data = providers.logs(node, args.lines)
        return data, data.get("detail", ""), target, 0 if data.get("ok") else 3

    if verb == "tree":
        node = tree.resolve(args.path) if args.path else tree.root_node
        target = node.id
        text = tree.render(node.id)
        return {"node": node.id, "text": text}, text, target, 0

    if verb == "index":
        rows = _index_data(tree)
        human = "\n".join(
            f"{row['id']:<22} {row['kind']:<10} {row['endpoint']:<22} {row['summary']}"
            for row in rows
        )
        return {"nodes": rows}, human, tree.root_node.id, 0

    if verb == "context":
        node = tree.resolve(args.path)
        prompt, chars, tokens, budget = _context(tree, node, args.observation, args.request)
        human = (
            prompt.rstrip()
            + f"\n\n---\n~{tokens} tokens ({chars} caractères), budget {budget} tokens."
        )
        data = {
            "node": node.id,
            "prompt": prompt,
            "chars": chars,
            "tokens_estimate": tokens,
            "budget": budget,
            "within_budget": tokens <= budget,
        }
        return data, human, node.id, 0 if tokens <= budget else 5

    if verb == "new":
        loaded = _read_spec(args.spec, "de nœud")
        nested = loaded.pop("node", None)
        spec: dict[str, Any] = {}
        if nested is not None:
            if not isinstance(nested, dict):
                raise IacError("invalid_spec", "\"node\" doit être un objet JSON.", exit_code=2)
            spec.update(nested)
        spec.update(loaded)
        missing = [key for key in ("id", "kind") if not spec.get(key)]
        if missing:
            raise IacError(
                "usage",
                " --".join([""] + missing).strip() + " requis dans la spécification.",
                exit_code=2,
            )
        node_id = str(spec["id"])
        kind = str(spec["kind"])

        _ask_confirmation(f"créer {kind} {node_id} sous {args.parent}", args, target=node_id)
        started = time.monotonic()
        try:
            result = scaffold.create_node(tree, args.parent, spec, dry_run=args.dry_run)
        except scaffold.SpecError as error:
            if not args.dry_run:
                state.audit(_actor(), verb, node_id, {"parent": args.parent, "kind": kind},
                            "error", int((time.monotonic() - started) * 1000), error.code)
            raise IacError(error.code, error.message, error.hint, error.exit_code, node_id)
        if not args.dry_run:
            state.audit(_actor(), verb, node_id,
                        {"parent": args.parent, "kind": kind}, "ok",
                        int((time.monotonic() - started) * 1000))
        lines = [
            ("DRY-RUN " if args.dry_run else "") + f"{kind} {node_id} sous {args.parent}",
            *[f"  {name}" for name in result["files"]],
        ]
        return result, "\n".join(lines), node_id, 0

    if verb == "render":
        spec_data = _read_spec(args.spec, "d'arborescence")
        label = "stdin" if str(args.spec) == "-" else str(args.spec.expanduser())
        _ask_confirmation(f"générer {label} dans {args.out}", args, target=str(args.out))
        started = time.monotonic()
        try:
            result = scaffold.render_spec(spec_data, args.out, dry_run=args.dry_run)
        except scaffold.SpecError as error:
            if not args.dry_run:
                state.audit(_actor(), verb, str(args.out), {"spec": label},
                            "error", int((time.monotonic() - started) * 1000), error.code)
            raise IacError(error.code, error.message, error.hint, error.exit_code)
        if not args.dry_run:
            state.audit(_actor(), verb, str(args.out), {"spec": label}, "ok",
                        int((time.monotonic() - started) * 1000))
        lines = [
            ("DRY-RUN " if args.dry_run else "")
            + f"{len(result['nodes'])} nœuds dans {result['root']}",
            *[f"  {node}" for node in result["nodes"]],
        ]
        return result, "\n".join(lines), result["root"], 0

    if verb == "doctor":
        issues = tree.validate()
        errors = [issue for issue in issues if issue["level"] == "error"]
        warnings = [issue for issue in issues if issue["level"] == "warning"]
        lines = []
        for issue in issues:
            prefix = "ERROR" if issue["level"] == "error" else "WARN "
            node = f" [{issue['node']}]" if issue.get("node") else ""
            lines.append(f"{prefix} {issue['code']}{node}: {issue['message']}")
        if not issues:
            lines.append("OK — arborescence valide.")
        else:
            lines.append(f"{len(errors)} erreur(s), {len(warnings)} avertissement(s).")
        data = {
            "issues": issues,
            "errors": len(errors),
            "warnings": len(warnings),
        }
        return data, "\n".join(lines), tree.root_node.id, 1 if errors else 0

    if verb == "net" and args.net_action == "list":
        data = _net_data(tree)
        return data, _human_net(tree), tree.root_node.id, 0

    if verb == "net":
        node = tree.resolve(args.path) if getattr(args, "path", None) else tree.root_node
        _not_implemented(node, f"net.{args.net_action}", "Implémentation prévue en v1 (voir docs/specification.md §6.5).")

    if verb == "move":
        node = tree.resolve(args.path)
        parent = tree.resolve(args.to)
        _ask_confirmation(f"déplacer {node.id} sous {parent.id}", args, target=node.id)
        started = time.monotonic()
        try:
            result = mover.move_node(
                tree,
                node,
                parent,
                address=args.address,
                port=args.port,
                dry_run=args.dry_run,
            )
        except mover.MoveError as error:
            if not args.dry_run:
                state.audit(
                    _actor(), verb, node.id,
                    {"from": node.parent, "to": parent.id}, "error",
                    int((time.monotonic() - started) * 1000), error.code,
                )
            raise IacError(error.code, error.message, error.hint, error.exit_code, node.id)
        if not args.dry_run:
            state.audit(
                _actor(), verb, node.id,
                {"from": result["from"], "to": result["to"],
                 "address": args.address, "port": args.port},
                "ok", int((time.monotonic() - started) * 1000),
            )
        lines = [
            ("DRY-RUN " if args.dry_run else "")
            + f"déplacer {result['node']} : {result['from']} → {result['to']}",
        ]
        if result.get("address_before") or result.get("address_after"):
            lines.append(
                f"  adresse : {result.get('address_before') or '—'}"
                f" → {result.get('address_after') or '—'}"
            )
        if result.get("changes"):
            lines.append("  champs : " + ", ".join(result["changes"]))
        for warning in result.get("warnings") or []:
            lines.append(f"  avertissement : {warning}")
        lines.append("  données non migrées ; étapes suivantes :")
        for step in result.get("next") or []:
            lines.append(f"    {step}")
        return result, "\n".join(lines), node.id, 0

    if verb in ("start", "stop", "restart"):
        if verb == "start" and args.path == "tree":
            _ask_confirmation("start tree", args, target=tree.root_node.id)
            started = time.monotonic()
            results = power.start_tree(tree, dry_run=args.dry_run)
            ok = all(item.get("ok") for item in results)
            if not args.dry_run:
                for item in results:
                    if item.get("ok"):
                        state.set_power(str(item["node"]), "running")
                state.audit(
                    _actor(), "start", "tree", {"nodes": len(results)},
                    "ok" if ok else "failed", int((time.monotonic() - started) * 1000),
                )
            human = "\n".join(_human_mutation(item) for item in results)
            return {"nodes": results, "ok": ok}, human, tree.root_node.id, 0 if args.dry_run or ok else 7

        node = tree.resolve(args.path)
        _ask_confirmation(
            f"{verb} {node.id}",
            args,
            required_id=node.id if node.risk == "high" else None,
            target=node.id,
        )
        started = time.monotonic()
        try:
            if verb == "start":
                result = power.start_node(tree, node, dry_run=args.dry_run)
            elif verb == "stop":
                result = power.stop_node(tree, node, force=args.force, dry_run=args.dry_run)
            else:
                first = power.stop_node(tree, node, force=args.force, dry_run=args.dry_run)
                second = power.start_node(tree, node, dry_run=args.dry_run)
                result = {
                    "node": node.id,
                    "action": "restart",
                    "dry_run": args.dry_run,
                    "steps": [first, second],
                    "ok": True if args.dry_run else bool(second.get("ok")),
                }
        except power.PowerError as error:
            if not args.dry_run:
                state.audit(
                    _actor(), verb, node.id, {"force": bool(getattr(args, "force", False))},
                    "error", int((time.monotonic() - started) * 1000), error.code,
                )
            raise IacError(error.code, error.message, error.hint, error.exit_code, node.id)
        if not args.dry_run:
            state.audit(
                _actor(), verb, node.id, {"force": bool(getattr(args, "force", False))},
                "ok" if result.get("ok") else "failed",
                int((time.monotonic() - started) * 1000),
            )
            if result.get("ok"):
                state.set_power(
                    node.id, "running" if verb in ("start", "restart") else "stopped"
                )
        if verb == "restart":
            human = "\n".join(_human_mutation(step) for step in result["steps"])
        else:
            human = _human_mutation(result)
        exit_code = 0 if args.dry_run or result.get("ok") else 7
        return result, human, node.id, exit_code

    if verb == "state":
        action = args.state_action
        if action == "set":
            node = tree.resolve(args.path)
            if not state.is_power_capable(node):
                raise IacError(
                    "unsupported",
                    f"{node.id} n'est pas pilotable (kind {node.kind}, verbe start absent).",
                    "Déclarer `start` et un provider qemu/docker pour ce nœud.",
                    exit_code=3,
                    target=node.id,
                )
            desired = state.set_power(node.id, args.power)
            human = (
                f"{node.id} : puissance désirée = {args.power}\n"
                "Appliquer avec `modops restore --yes`."
            )
            return {"node": node.id, "power": args.power, "desired": desired}, human, node.id, 0

        if action == "save":
            observed = state.save_observed(tree)
            lines = [f"{'NODE':<22} {'ÉTAT':<10} DÉTAIL"]
            for node_id, item in sorted(observed["nodes"].items()):
                lines.append(f"{node_id:<22} {item['state']:<10} {item['detail']}")
            return {"observed": observed}, "\n".join(lines), tree.root_node.id, 0

        if action == "reset":
            node = tree.resolve(args.path) if args.path else None
            desired = state.clear_power(node.id if node else None)
            human = (
                f"état désiré effacé : {node.id}"
                if node
                else "état désiré effacé (tous les nœuds)"
            )
            return {"desired": desired}, human, tree.root_node.id, 0

        desired = state.load_desired()
        observed = state.load_observed()
        items = state.plan(tree)
        if action == "plan":
            return {"plan": items}, _human_plan(items), tree.root_node.id, 0

        lines = ["État désiré :"]
        wanted = desired.get("nodes", {})
        if wanted:
            for node_id in sorted(wanted):
                lines.append(f"  {node_id:<22} {wanted[node_id].get('power', '?')}")
        else:
            lines.append("  (vide — `modops state set <nœud> --power running`)")
        lines.append("")
        lines.append("Dernière observation :")
        seen = observed.get("nodes", {})
        if seen:
            for node_id in sorted(seen):
                lines.append(f"  {node_id:<22} {seen[node_id].get('state', '?'):<10} {seen[node_id].get('detail', '')}")
        else:
            lines.append("  (aucune — `modops state save`)")
        lines.append("")
        lines.append("Plan :")
        lines.append(_human_plan(items))
        data = {"desired": desired, "observed": observed, "plan": items}
        return data, "\n".join(lines), tree.root_node.id, 0

    if verb == "restore":
        items = state.plan(tree)
        actionable = [item for item in items if item["action"] in ("start", "stop")]
        if not actionable:
            human = "Rien à restaurer.\n" + _human_plan(items)
            return {"plan": items, "results": []}, human, tree.root_node.id, 0
        _ask_confirmation(
            f"restore ({len(actionable)} action(s))",
            args,
            target=tree.root_node.id,
        )
        started = time.monotonic()
        results = power.restore(tree, dry_run=args.dry_run)
        ok = all(result.get("ok", True) for result in results)
        if not args.dry_run:
            state.audit(
                _actor(), "restore", tree.root_node.id, {"actions": len(actionable)},
                "ok" if ok else "failed", int((time.monotonic() - started) * 1000),
            )
        human = "\n".join(_human_mutation(result) for result in results)
        return (
            {"plan": items, "results": results, "ok": ok},
            human,
            tree.root_node.id,
            0 if ok or args.dry_run else 5,
        )

    if verb in ("connect", "disconnect"):
        node = tree.resolve(args.path)
        _not_implemented(node, verb, f"Plan : {verb} {node.id} --to {args.to} — voir docs/specification.md §6.7.")

    if verb == "ask":
        return _ask(tree, " ".join(args.text))

    raise IacError("usage", f"Verbe inconnu : {verb}", exit_code=2)


def _normalize(text: str) -> str:
    text = text.lower().replace("œ", "oe").replace("æ", "ae")
    text = unicodedata.normalize("NFKD", text)
    return "".join(char for char in text if not unicodedata.combining(char))


def _find_mentions(tree: Tree, text: str) -> list[Node]:
    found: dict[str, Node] = {}
    best_len = 0
    for node in tree.nodes.values():
        for token in (node.id, *node.aliases):
            pattern = rf"(?<![a-z0-9-]){re.escape(token.lower())}(?![a-z0-9-])"
            if re.search(pattern, text):
                if len(token) > best_len:
                    found = {node.id: node}
                    best_len = len(token)
                elif len(token) == best_len:
                    found[node.id] = node
    return list(found.values())


_ASK_INVENTORY = (
    ("vm", r"\b(vms?|virtual machines?)\b"),
    ("app", r"\b(apps?|applications?|services?)\b"),
    ("container", r"\b(conteneurs?|containers?)\b"),
    ("device", r"\b(devices?|peripheriques?)\b"),
    ("bridge", r"\b(bridges?|ponts?)\b"),
)

_ASK_LISTING = re.compile(
    r"\b(liste|listes|liste-moi|montre|montre-moi|affiche|quels|quelles|"
    r"combien|inventaire|recense)\b"
)


def _inventory_rows(tree: Tree, kind: str | None) -> list[Node]:
    rows = [
        node
        for node, _level in tree.children_recursive(tree.root_node.id, 16)
        if kind is None or node.kind == kind
    ]
    return sorted(rows, key=lambda node: node.id)


def _ask_inventory(
    tree: Tree, kind: str | None, count_only: bool, live_state: bool = False
) -> tuple[dict[str, Any], str]:
    rows = _inventory_rows(tree, kind)
    label = kind or "nœud"
    if count_only:
        human = f"{len(rows)} {label}(s)."
        if rows:
            human += " " + ", ".join(node.id for node in rows) + "."
        return {"routed": "ls", "kind": kind, "count": len(rows), "nodes": [node.id for node in rows]}, human

    states: dict[str, str] = {}
    if live_state:
        for node in rows:
            try:
                observed, _human = _read(tree, "status", node)
                states[node.id] = str(observed.get("state", "unknown"))
            except IacError:
                states[node.id] = "unknown"

    columns = f"{'NODE':<22} {'KIND':<10} "
    if live_state:
        columns += f"{'STATE':<10} "
    columns += f"{'ENDPOINT':<22} SUMMARY"
    lines = [columns]
    for node in rows:
        line = f"{node.id:<22} {node.kind:<10} "
        if live_state:
            line += f"{states.get(node.id, 'unknown'):<10} "
        line += f"{_endpoint(node) or '—':<22} {node.summary}"
        lines.append(line)
    data = {
        "routed": "ls",
        "kind": kind,
        "count": len(rows),
        "nodes": [
            {
                "id": node.id,
                "kind": node.kind,
                "endpoint": _endpoint(node),
                "state": states.get(node.id),
                "summary": node.summary,
            }
            for node in rows
        ],
    }
    return data, "\n".join(lines) if rows else "(aucun nœud)"


def _ask(tree: Tree, text: str) -> tuple[dict[str, Any], str, str | None, int]:
    normalized = _normalize(text)
    mentions = _find_mentions(tree, normalized)
    if len(mentions) > 1:
        ids = ", ".join(node.id for node in mentions)
        raise IacError(
            "ambiguous",
            f"La demande mentionne plusieurs nœuds : {ids}.",
            hint="Préciser un seul nœud.",
            exit_code=4,
        )
    node = mentions[0] if mentions else None

    if re.search(
        r"\b(cree|creer|creez|crees|ajoute|ajouter|ajoutez|nouveau|nouvelle|"
        r"provisionne|provisionner|genere|generer)\b",
        normalized,
    ):
        parent = node or tree.default_node()
        raise IacError(
            "scaffold_required",
            f"Création demandée sous {parent.id} : le routeur déterministe ne génère pas de nœud.",
            hint=(
                "Utiliser `modops new` ou le prompt agent/prompts/scaffolder.md "
                "(voir docs/agent-workflow.md)."
            ),
            exit_code=3,
            target=parent.id,
        )

    if re.search(r"\b(reseau|reseaux|network|net)\b", normalized):
        if re.search(r"\b(liste|montre|quels|quelles|affiche|etat|statut)\b", normalized) or not node:
            data = _net_data(tree)
            return (
                {"routed": "net.list", **data},
                _human_net(tree),
                tree.root_node.id,
                0,
            )

    if node is None and _ASK_LISTING.search(normalized):
        wants_state = bool(
            re.search(
                r"\b(tourne\w*|running|allume\w*|eteint\w*|arrete\w*|actifs?|up|down)\b",
                normalized,
            )
        )
        for kind, pattern in _ASK_INVENTORY:
            if re.search(pattern, normalized):
                data, human = _ask_inventory(
                    tree,
                    kind,
                    bool(re.search(r"\bcombien\b", normalized)),
                    live_state=wants_state,
                )
                return data, human, tree.root_node.id, 0
        if re.search(r"\bcombien\b", normalized):
            data, human = _ask_inventory(tree, None, True)
            return data, human, tree.root_node.id, 0
        if re.search(
            r"\b(arborescence|topologie|infra|infrastructure|tree|noeuds?|nodes?|tout)\b",
            normalized,
        ):
            text_rendered = tree.render(tree.root_node.id)
            return (
                {"routed": "tree", "node": tree.root_node.id, "text": text_rendered},
                text_rendered,
                tree.root_node.id,
                0,
            )

    actions = [
        ("restart", r"\b(redemarre|redemarrer|restart)\b"),
        ("logs", r"\b(journal|journaux|logs|log)\b"),
        ("health", r"\b(sante|health|repond|ping|joignable|up|down|fonctionne|accessible)\b"),
        ("status", r"\b(etat|statut|status|tourne|marche|allume)\b"),
        ("start", r"\b(demarre|demarrer|start|lance)\b"),
        ("stop", r"\b(arrete|arreter|stop|coupe|eteins)\b"),
        ("path", r"\b(chemin|path|ou est|ou se trouve|adresse|ip)\b"),
        ("show", r"\b(montre|affiche|fiche|detail|decris|describe)\b"),
        ("connect", r"\b(connecte|connecter|connect)\b"),
        ("disconnect", r"\b(deconnecte|deconnecter|disconnect)\b"),
        ("move", r"\b(deplace|deplacer|deplaces|migre|migrer|migres|bouge|bouger|relocalise|relocaliser)\b"),
    ]
    action = None
    for name, pattern in actions:
        if re.search(pattern, normalized):
            action = name
            break

    if action is None and node is not None:
        action = "show"

    if action is None:
        raise IacError(
            "unrouted",
            "Demande non routée par le routeur déterministe.",
            hint="Le coordinateur LLM n'est pas activé en v0. Utiliser un verbe modops explicite.",
            exit_code=3,
        )

    if action == "path":
        if node is None:
            raise IacError("not_found", "Aucun nœud mentionné.", exit_code=4)
        data, human = _read(tree, "path", node)
        return {"routed": "path", **data}, human, node.id, 0

    if action in ("show", "status", "health", "logs"):
        wants_recursive = action == "status" and bool(
            re.search(
                r"\b(global|globaux|tous|toutes|recursif|recursive|ensemble)\b",
                normalized,
            )
        )
        if node is None and wants_recursive:
            node = tree.default_node()
        if node is None:
            raise IacError("not_found", "Aucun nœud mentionné.", exit_code=4)
        if action == "logs":
            data = providers.logs(node, 40)
            return {"routed": "logs", **data}, data.get("detail", ""), node.id, 0 if data.get("ok") else 3
        if wants_recursive:
            data, human = _status_recursive(tree, node)
            return (
                {"routed": "status", "node": node.id, "recursive": True, "result": data},
                human,
                node.id,
                0,
            )
        data, human = _read(tree, action, node)
        exit_code = 0
        if action == "health" and not data.get("ok"):
            exit_code = 7
        return {"routed": action, "node": node.id, "result": data}, human, node.id, exit_code

    if node is None:
        raise IacError("not_found", "Aucun nœud mentionné.", exit_code=4)

    if action in ("start", "stop", "restart", "move"):
        if action == "move":
            hint = (
                f"Exécuter : ./bin/modops move {node.id} --to <parent> --dry-run, "
                "puis --yes."
            )
        else:
            hint = (
                f"Exécuter : ./bin/modops {action} {node.id} --dry-run, "
                f"puis ./bin/modops {action} {node.id} --yes."
            )
        raise IacError(
            "explicit_verb_required",
            f"« {action} {node.id} » est une action : le routeur ne l'exécute pas.",
            hint=hint,
            exit_code=5,
            target=node.id,
        )

    _not_implemented(node, action, f"Plan : {action} {node.id} — voir docs/specification.md.")


def _emit_success(
    json_mode: bool,
    verb: str,
    target: str | None,
    data: dict[str, Any],
    human: str,
    duration_ms: int,
    exit_code: int,
) -> int:
    payload = {
        "ok": exit_code == 0,
        "verb": verb,
        "target": target,
        "data": data,
        "warnings": [],
        "duration_ms": duration_ms,
    }
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    elif human:
        print(human)
    return exit_code


def _emit_error(json_mode: bool, verb: str | None, error: IacError) -> int:
    payload = {
        "ok": False,
        "verb": verb,
        "target": error.target,
        "error": {
            "code": error.code,
            "message": error.message,
            "hint": error.hint,
        },
        "duration_ms": 0,
    }
    if json_mode:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"ERREUR [{error.code}] {error.message}", file=sys.stderr)
        if error.hint:
            print(f"  → {error.hint}", file=sys.stderr)
    return error.exit_code


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    verb: str | None = None
    json_mode = False
    try:
        raw, json_mode, root_ref = _extract_globals(raw)
        root = Path(root_ref).expanduser() if root_ref else Path(
            os.environ.get("IACRUN_ROOT", str(DEFAULT_ROOT))
        )
        tree = Tree.load(root)
        parser = build_parser()
        args = parser.parse_args(raw)
        verb = args.verb
        if verb == "net":
            verb = f"net.{args.net_action}"
        if verb == "state":
            verb = f"state.{args.state_action}"
        started = time.monotonic()
        data, human, target, exit_code = _dispatch(tree, args)
        return _emit_success(
            json_mode,
            verb or "?",
            target,
            data,
            human,
            int((time.monotonic() - started) * 1000),
            exit_code,
        )
    except IacError as error:
        return _emit_error(json_mode, verb, error)
    except power.PowerError as error:
        return _emit_error(
            json_mode,
            verb,
            IacError(error.code, error.message, error.hint, error.exit_code, error.target),
        )
    except TreeError as error:
        return _emit_error(
            json_mode,
            verb,
            IacError(error.code, error.message, error.hint, error.exit_code),
        )
    except KeyboardInterrupt:
        print("Interrompu.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
