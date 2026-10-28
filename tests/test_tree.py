"""Tests de l'arborescence et du CLI (stdlib uniquement)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from iacrun import move, power, providers, scaffold, state  # noqa: E402
from iacrun import model  # noqa: E402
from iacrun.cli import IacError, _ask, _context  # noqa: E402
from iacrun.scaffold import SpecError  # noqa: E402
from iacrun.tree import Tree, TreeError  # noqa: E402


class TreeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tree = Tree.load(ROOT / "tree" / "home")

    def test_example_tree_is_declarative(self) -> None:
        for node in self.tree.nodes.values():
            self.assertNotIn("runtime", node.raw)

    def test_root(self) -> None:
        self.assertEqual(self.tree.root_node.id, "home")
        self.assertEqual(self.tree.root_node.kind, "site")
        self.assertIsNone(self.tree.root_node.parent)

    def test_unique_ids(self) -> None:
        ids = [node.id for node in self.tree.nodes.values()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_expected_nodes(self) -> None:
        expected = {
            "home", "net-external", "host-example", "app-cli", "net-example", "br-example",
            "vm-compute", "dev-accelerator", "app-model-api", "vm-services",
            "app-dashboard",
        }
        self.assertEqual(set(self.tree.nodes), expected)

    def test_parents(self) -> None:
        children = {node.id: self.tree.children(node.id) for node in self.tree.nodes.values()}
        self.assertEqual(
            [n.id for n in children["net-example"]],
            ["br-example", "vm-compute", "vm-services"],
        )
        self.assertEqual(children["br-example"], [])
        self.assertEqual([n.id for n in children["host-example"]], ["app-cli"])
        self.assertEqual([n.id for n in children["vm-compute"]], ["dev-accelerator", "app-model-api"])
        self.assertEqual([n.id for n in children["vm-services"]], ["app-dashboard"])

    def test_resolve(self) -> None:
        self.assertEqual(self.tree.resolve("vm-services").id, "vm-services")
        self.assertEqual(self.tree.resolve("services").id, "vm-services")
        self.assertEqual(self.tree.resolve("dashboard").id, "app-dashboard")
        self.assertEqual(self.tree.resolve("compute").id, "vm-compute")
        path = self.tree.nodes["vm-services"].path / "AGENT.md"
        self.assertEqual(self.tree.resolve(str(path)).id, "vm-services")

    def test_kind_descriptors_cover_all_kinds(self) -> None:
        self.assertEqual(set(self.tree.descriptors), set(model.KINDS))

    def test_app_inherits_kind_descriptor(self) -> None:
        app = self.tree.nodes["app-dashboard"]
        self.assertNotIn("provider", app.raw)
        self.assertEqual(app.provider, "docker")
        self.assertEqual(app.risk, "medium")
        self.assertIn("start", app.verbs)
        self.assertEqual(app.effective["provider"], "docker")

        cli_helper = self.tree.nodes["app-cli"]
        self.assertEqual(cli_helper.provider, "local")
        self.assertEqual(cli_helper.risk, "low")
        self.assertNotIn("start", cli_helper.verbs)

    def test_resolve_ambiguous(self) -> None:
        with self.assertRaises(TreeError):
            self.tree.resolve("inexistant")

    def test_render(self) -> None:
        text = self.tree.render()
        self.assertIn("home [site]", text)
        self.assertIn("vm-services [vm 192.0.2.11/24]", text)
        self.assertIn("app-dashboard [app 192.0.2.11:8081]", text)

    def test_validate(self) -> None:
        issues = self.tree.validate()
        errors = [issue for issue in issues if issue["level"] == "error"]
        self.assertEqual(errors, [], errors)

    def test_validate_flags_manual_app_move(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = json.loads((ROOT / "examples" / "net-lab.json").read_text(encoding="utf-8"))
            scaffold.render_spec(spec, Path(tmp))
            home = Path(tmp) / "home"
            source = home / "net-lab" / "vm-lab-web" / "app-lab-dashboard"
            moved = home / "net-lab" / "vm-lab-compute" / "app-lab-dashboard"
            source.rename(moved)
            data = json.loads((moved / "node.json").read_text(encoding="utf-8"))
            data["parent"] = "vm-lab-compute"
            (moved / "node.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            issues = Tree.load(home).validate()
            codes = {issue["code"] for issue in issues if issue.get("node") == "app-lab-dashboard"}
            self.assertIn("app_address_outside_parent", codes)
            self.assertIn("management_alias_mismatch", codes)

    def test_ask_net(self) -> None:
        data, human, target, code = _ask(self.tree, "liste les réseaux")
        self.assertEqual(target, "home")
        self.assertEqual(code, 0)
        self.assertIn("networks", data)
        self.assertIn("net-example", human)

    def test_ask_lists_vms(self) -> None:
        data, human, target, code = _ask(self.tree, "liste les VMs")
        self.assertEqual((target, code), ("home", 0))
        self.assertEqual(data["routed"], "ls")
        self.assertEqual(data["kind"], "vm")
        self.assertEqual(data["count"], 2)
        self.assertIn("vm-services", human)
        self.assertNotIn("app-model-api", human)

    def test_ask_lists_nodes_as_tree(self) -> None:
        data, human, target, code = _ask(self.tree, "liste les nœuds")
        self.assertEqual((target, code), ("home", 0))
        self.assertEqual(data["routed"], "tree")
        self.assertIn("home [site]", human)

    def test_ask_counts_nodes(self) -> None:
        data, _human, _target, code = _ask(self.tree, "combien de nœuds")
        self.assertEqual(code, 0)
        self.assertEqual(data["count"], len(self.tree.nodes) - 1)

    def test_ask_move_is_not_a_silent_show(self) -> None:
        with self.assertRaises(IacError) as context:
            _ask(self.tree, "déplace dashboard sur la vm compute")
        self.assertEqual(context.exception.code, "explicit_verb_required")
        self.assertEqual(context.exception.target, "app-dashboard")
        self.assertIn("modops move app-dashboard", context.exception.hint)

    def test_ask_start_requires_explicit_verb(self) -> None:
        with self.assertRaises(IacError) as context:
            _ask(self.tree, "démarre services")
        self.assertEqual(context.exception.code, "explicit_verb_required")
        self.assertEqual(context.exception.target, "vm-services")
        self.assertEqual(context.exception.exit_code, 5)
        self.assertIn("modops start vm-services", context.exception.hint)


class StateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"IACRUN_STATE_DIR": self.tmp.name})
        self.env.start()
        self.tree = Tree.load(ROOT / "tree" / "home")

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_set_and_clear(self) -> None:
        state.set_power("vm-services", "running")
        desired = state.load_desired()
        self.assertEqual(desired["nodes"]["vm-services"]["power"], "running")
        state.clear_power("vm-services")
        self.assertEqual(state.load_desired()["nodes"], {})

    def test_plan_matches_desired(self) -> None:
        state.set_power("vm-services", "running")
        state.set_power("app-dashboard", "stopped")

        def fake_status(node: object) -> dict[str, str]:
            node_id = getattr(node, "id")
            if node_id == "vm-services":
                return {"state": "stopped", "detail": "pidfile absent", "provider": "qemu"}
            return {"state": "up", "detail": "HTTP 200", "provider": "http"}

        with mock.patch("iacrun.providers.runtime_state", side_effect=fake_status):
            items = state.plan(self.tree)

        actions = {item["node"]: item["action"] for item in items}
        self.assertEqual(actions, {"vm-services": "start", "app-dashboard": "stop"})
        self.assertEqual(items[0]["node"], "vm-services")

    def test_audit_appends_jsonl(self) -> None:
        state.audit("test", "stop", "vm-services", {"force": True}, "ok", 12)
        lines = (Path(self.tmp.name) / "audit.jsonl").read_text(encoding="utf-8").splitlines()
        entry = json.loads(lines[0])
        self.assertEqual(entry["verb"], "stop")
        self.assertEqual(entry["target"], "vm-services")
        self.assertTrue(entry["args"]["force"])


class PowerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tree = Tree.load(ROOT / "tree" / "home")

    def test_start_vm_dry_run(self) -> None:
        node = self.tree.nodes["vm-services"]
        with mock.patch("iacrun.providers.runtime_state", return_value={"state": "stopped", "detail": "x", "provider": "qemu"}):
            result = power.start_node(self.tree, node, dry_run=True)
        command = " ".join(result["outcome"]["command"])
        self.assertIn("/usr/bin/true", command)
        self.assertTrue(result["dry_run"])

    def test_stop_vm_brutal_dry_run(self) -> None:
        node = self.tree.nodes["vm-compute"]
        with mock.patch("iacrun.providers.runtime_state", return_value={"state": "running", "detail": "pid 1", "provider": "qemu"}):
            result = power.stop_node(self.tree, node, force=True, dry_run=True)
        self.assertEqual(result["outcome"]["mode"], "brutal")
        self.assertTrue(result["dry_run"])

    def test_stop_docker_dry_run(self) -> None:
        node = self.tree.nodes["app-dashboard"]
        with mock.patch("iacrun.providers.runtime_state", return_value={"state": "up", "detail": "running", "provider": "docker"}):
            result = power.stop_node(self.tree, node, dry_run=True)
        command = " ".join(result["outcome"]["command"])
        self.assertIn("docker stop", command)
        self.assertIn("dashboard", command)

class RuntimeStateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tree = Tree.load(ROOT / "tree" / "home")

    def test_docker_running_but_api_silent_is_degraded(self) -> None:
        node = self.tree.nodes["app-model-api"]
        with mock.patch(
            "iacrun.providers.health",
            return_value={"ok": False, "type": "http", "detail": "timed out", "latency_ms": 3},
        ), mock.patch(
            "iacrun.providers._ssh",
            return_value=mock.Mock(returncode=0, stdout="running\n", stderr=""),
        ):
            result = providers.runtime_state(node)
        self.assertEqual(result["state"], "degraded")
        self.assertIn("timed out", result["detail"])

    def test_docker_exited_is_stopped(self) -> None:
        node = self.tree.nodes["app-dashboard"]
        with mock.patch(
            "iacrun.providers.health",
            return_value={"ok": False, "type": "http", "detail": "connexion refusée", "latency_ms": 1},
        ), mock.patch(
            "iacrun.providers._ssh",
            return_value=mock.Mock(returncode=0, stdout="exited\n", stderr=""),
        ):
            result = providers.runtime_state(node)
        self.assertEqual(result["state"], "stopped")

    def test_docker_unreachable_host(self) -> None:
        node = self.tree.nodes["app-model-api"]
        with mock.patch(
            "iacrun.providers.health",
            return_value={"ok": False, "type": "http", "detail": "timeout", "latency_ms": 1},
        ), mock.patch(
            "iacrun.providers._ssh",
            return_value=mock.Mock(
                returncode=255,
                stdout="",
                stderr="ssh: connect to host 192.0.2.10 port 22: Connection timed out",
            ),
        ):
            result = providers.runtime_state(node)
        self.assertEqual(result["state"], "unreachable")

    def test_docker_ssh_raises(self) -> None:
        node = self.tree.nodes["app-model-api"]
        with mock.patch(
            "iacrun.providers.health",
            return_value={"ok": False, "type": "http", "detail": "timeout", "latency_ms": 1},
        ), mock.patch(
            "iacrun.providers._ssh",
            side_effect=OSError("ssh absent"),
        ):
            result = providers.runtime_state(node)
        self.assertEqual(result["state"], "unreachable")


class PidFileTest(unittest.TestCase):
    """Un pidfile protégé doit être lu via sudo."""

    def _pidfile(self) -> Path:
        handle = tempfile.NamedTemporaryFile(delete=False)
        handle.write(b"4242\n")
        handle.close()
        os.chmod(handle.name, 0o000)
        self.addCleanup(os.unlink, handle.name)
        return Path(handle.name)

    def test_unreadable_pidfile_uses_sudo_cat(self) -> None:
        path = self._pidfile()
        with mock.patch("iacrun.providers._run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="4242\n", stderr="")
            pid, error = providers._pid_from_file(path)
        self.assertEqual(pid, 4242)
        self.assertIsNone(error)
        self.assertEqual(run.call_args.args[0][:3], ["sudo", "-n", "cat"])

    def test_sudo_refused_yields_unknown_not_traceback(self) -> None:
        path = self._pidfile()
        with mock.patch("iacrun.providers._run") as run:
            run.return_value = mock.Mock(returncode=1, stdout="", stderr="mot de passe requis")
            pid, error = providers._pid_from_file(path)
        self.assertIsNone(pid)
        self.assertIn("droits root", error or "")

    def test_missing_pidfile_is_not_an_error(self) -> None:
        pid, error = providers._pid_from_file(Path("/tmp/iacrun-absent.pid"))
        self.assertIsNone(pid)
        self.assertIsNone(error)

    def test_pidfile_removed_during_stop_is_not_a_rights_error(self) -> None:
        path = self._pidfile()
        with mock.patch("iacrun.providers._run") as run:
            run.return_value = mock.Mock(
                returncode=1,
                stdout="",
                stderr="cat: /run/modops/example-compute/qemu.pid: Aucun fichier ou dossier de ce type",
            )
            pid, error = providers._pid_from_file(path)
        self.assertIsNone(pid)
        self.assertIsNone(error)


class ContextTest(unittest.TestCase):
    """Aucun placeholder ne doit rester dans le prompt opérateur."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tree = Tree.load(ROOT / "tree" / "home")

    def test_no_placeholder_left_and_request_rendered(self) -> None:
        node = self.tree.nodes["app-model-api"]
        prompt, chars, tokens, budget = _context(
            self.tree, node, "aucune", "redémarre le conteneur model-service"
        )
        self.assertNotIn("{{", prompt)
        self.assertIn("redémarre le conteneur model-service", prompt)
        self.assertLessEqual(tokens, budget)
        self.assertEqual(chars, len(prompt))

    def test_all_nodes_within_budget(self) -> None:
        for node in self.tree.nodes.values():
            _, _, tokens, budget = _context(self.tree, node, "aucune")
            self.assertLessEqual(tokens, budget, node.id)


class SudoClassificationTest(unittest.TestCase):
    """`sudo` est localisé : le refus doit être classé même en français."""

    def _result(self, stderr: str, code: int = 1) -> mock.Mock:
        return mock.Mock(returncode=code, stdout="", stderr=stderr)

    def test_french_password_prompt_is_permission_denied(self) -> None:
        proc = self._result(
            "sudo: il est nécessaire de saisir un mot de passe",
        )
        with self.assertRaises(power.PowerError) as caught:
            power._check_sudo_result(proc, ["/home/example-user/example-infra/vm/start.sh"])
        self.assertEqual(caught.exception.code, "permission_denied")
        self.assertEqual(caught.exception.exit_code, 6)

    def test_english_password_prompt_is_permission_denied(self) -> None:
        proc = self._result("sudo: a terminal is required to read the password")
        with self.assertRaises(power.PowerError) as caught:
            power._check_sudo_result(proc, ["/bin/true"])
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_other_failure_stays_command_failed(self) -> None:
        proc = self._result("qemu-system-x86_64: /dev/kvm: Permission denied", code=1)
        with self.assertRaises(power.PowerError) as caught:
            power._check_sudo_result(proc, ["/home/example-user/example-infra/vm/start.sh"])
        self.assertEqual(caught.exception.code, "command_failed")
        self.assertEqual(caught.exception.exit_code, 5)


class ScaffoldTest(unittest.TestCase):
    """Génération de nœuds depuis une spécification JSON."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def minimal_tree(self) -> Tree:
        spec = {
            "schema": 1,
            "root": {"id": "home"},
            "nodes": [{"id": "net-lab", "kind": "network", "cidr": "192.0.2.0/24"}],
        }
        out = self.tmp / "mini"
        scaffold.render_spec(spec, out)
        return Tree.load(out / "home")

    def test_render_example_creates_valid_tree(self) -> None:
        spec = json.loads((ROOT / "examples" / "net-lab.json").read_text(encoding="utf-8"))
        out = self.tmp / "demo"
        result = scaffold.render_spec(spec, out)
        tree = Tree.load(out / "home")
        self.assertEqual(
            set(result["nodes"]),
            {
                "home", "net-lab", "br-lab", "vm-lab-compute", "dev-lab-accelerator",
                "app-lab-model", "vm-lab-web", "app-lab-dashboard",
            },
        )
        errors = [issue for issue in tree.validate() if issue["level"] == "error"]
        self.assertEqual(errors, [])
        app = tree.nodes["app-lab-model"]
        self.assertEqual(app.raw["addresses"][0]["address"], "192.0.2.10")
        self.assertEqual(app.raw["addresses"][0]["port"], 8080)
        self.assertEqual(app.raw["addresses"][0]["network"], "net-lab")
        card = app.card()
        self.assertIn("`app-lab-model`", card)
        self.assertIn("## Mission", card)
        self.assertIn("## Escalade", card)
        self.assertLessEqual(len(card.splitlines()), 60)

    def test_render_dry_run_writes_nothing(self) -> None:
        spec = json.loads((ROOT / "examples" / "net-lab.json").read_text(encoding="utf-8"))
        out = self.tmp / "dry"
        result = scaffold.render_spec(spec, out, dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual(len(result["files"]), 16)
        self.assertFalse(out.exists())

    def test_render_refuses_non_empty_output(self) -> None:
        out = self.tmp / "busy"
        out.mkdir()
        (out / "keep.txt").write_text("x", encoding="utf-8")
        with self.assertRaises(SpecError) as caught:
            scaffold.render_spec(
                {"schema": 1, "root": {"id": "home"}, "nodes": [{"id": "net-a", "kind": "network", "cidr": "198.51.100.0/24"}]},
                out,
            )
        self.assertEqual(caught.exception.code, "output_busy")
        self.assertTrue((out / "keep.txt").is_file())

    def test_render_refuses_file_output(self) -> None:
        out = self.tmp / "file"
        out.write_text("x", encoding="utf-8")
        with self.assertRaises(SpecError) as caught:
            scaffold.render_spec(
                {"schema": 1, "root": {"id": "home"},
                 "nodes": [{"id": "net-a", "kind": "network", "cidr": "198.51.100.0/24"}]},
                out,
            )
        self.assertEqual(caught.exception.code, "output_busy")
        self.assertTrue(out.is_file())

    def test_render_rollback_on_invalid_tree(self) -> None:
        out = self.tmp / "bad"
        spec = {
            "schema": 1,
            "root": {"id": "home"},
            "nodes": [{
                "id": "net-lab", "kind": "network", "cidr": "192.0.2.0/24",
                "children": [
                    {"id": "vm-a", "kind": "vm", "aliases": ["calc"]},
                    {"id": "vm-b", "kind": "vm", "aliases": ["calc"]},
                ],
            }],
        }
        with self.assertRaises(SpecError) as caught:
            scaffold.render_spec(spec, out)
        self.assertEqual(caught.exception.code, "invalid_tree")
        self.assertFalse(out.exists())

    def test_render_rejects_nested_runtime_in_resources(self) -> None:
        spec = {
            "schema": 1,
            "root": {"id": "home"},
            "nodes": [{
                "id": "net-lab", "kind": "network", "cidr": "192.0.2.0/24",
                "children": [{
                    "id": "vm-a", "kind": "vm",
                    "resources": {"vcpus": 2, "runtime": {"type": "qemu"}},
                }],
            }],
        }
        with self.assertRaises(SpecError) as caught:
            scaffold.render_spec(spec, self.tmp / "nested")
        self.assertEqual(caught.exception.code, "bad_resources")
        self.assertFalse((self.tmp / "nested").exists())

    def test_render_rejects_missing_nodes(self) -> None:
        with self.assertRaises(SpecError) as caught:
            scaffold.render_spec(
                {"schema": 1, "root": {"id": "home"}, "nodes": []}, self.tmp / "x"
            )
        self.assertEqual(caught.exception.code, "missing_nodes")

    def test_render_rejects_duplicate_id(self) -> None:
        spec = {
            "schema": 1,
            "root": {"id": "home"},
            "nodes": [
                {"id": "net-a", "kind": "network", "cidr": "198.51.100.0/24"},
                {"id": "net-a", "kind": "network", "cidr": "203.0.113.0/24"},
            ],
        }
        with self.assertRaises(SpecError) as caught:
            scaffold.render_spec(spec, self.tmp / "dup")
        self.assertEqual(caught.exception.code, "duplicate_id")
        self.assertFalse((self.tmp / "dup").exists())

    def test_create_node_derives_defaults(self) -> None:
        tree = self.minimal_tree()
        result = scaffold.create_node(
            tree,
            "net-lab",
            {
                "id": "vm-ci", "kind": "vm", "title": "VM CI",
                "address": "192.0.2.14/24",
                "resources": {"vcpus": 2, "ram_mib": 4096},
            },
        )
        payload = result["node"]
        self.assertEqual(payload["provider"], "qemu")
        self.assertEqual(payload["risk"], "medium")
        self.assertIn("start", payload["verbs"])
        self.assertEqual(payload["parent"], "net-lab")
        self.assertEqual(payload["addresses"][0]["network"], "net-lab")
        self.assertEqual(payload["health"], {"type": "qemu"})
        self.assertEqual(result["files"], [
            str(tree.nodes["net-lab"].path / "vm-ci" / "node.json"),
            str(tree.nodes["net-lab"].path / "vm-ci" / "AGENT.md"),
        ])
        reloaded = Tree.load(tree.root)
        self.assertIn("vm-ci", reloaded.nodes)
        errors = [issue for issue in reloaded.validate() if issue["level"] == "error"]
        self.assertEqual(errors, [])
        card = reloaded.nodes["vm-ci"].card()
        self.assertIn("2 vCPU, 4096 Mio", card)

    def test_create_node_without_runtime_is_declarative(self) -> None:
        tree = self.minimal_tree()
        result = scaffold.create_node(tree, "net-lab", {"id": "vm-decl", "kind": "vm"})
        self.assertNotIn("runtime", result["node"])
        self.assertEqual(result["node"]["health"], {"type": "qemu"})

    def test_create_node_dry_run_writes_nothing(self) -> None:
        tree = self.minimal_tree()
        result = scaffold.create_node(
            tree, "net-lab", {"id": "vm-dry", "kind": "vm"}, dry_run=True
        )
        self.assertTrue(result["dry_run"])
        self.assertFalse((tree.nodes["net-lab"].path / "vm-dry").exists())

    def test_create_node_rejects_address_outside(self) -> None:
        tree = self.minimal_tree()
        with self.assertRaises(SpecError) as caught:
            scaffold.create_node(
                tree, "net-lab", {"id": "vm-out", "kind": "vm", "address": "192.0.2.10/24"}
            )
        self.assertEqual(caught.exception.code, "address_outside")
        self.assertFalse((tree.nodes["net-lab"].path / "vm-out").exists())

    def test_create_node_rejects_bad_port(self) -> None:
        tree = self.minimal_tree()
        with self.assertRaises(SpecError) as caught:
            scaffold.create_node(
                tree, "net-lab",
                {"id": "vm-bad", "kind": "vm", "address": "192.0.2.15", "port": "abc"},
            )
        self.assertEqual(caught.exception.code, "bad_int")

    def test_create_node_rejects_port_without_address(self) -> None:
        tree = self.minimal_tree()
        with self.assertRaises(SpecError) as caught:
            scaffold.create_node(tree, "net-lab", {"id": "vm-bad", "kind": "vm", "port": 80})
        self.assertEqual(caught.exception.code, "missing_address")

    def test_create_node_inherits_parent_address_for_port(self) -> None:
        tree = self.minimal_tree()
        scaffold.create_node(
            tree, "net-lab", {"id": "vm-svc", "kind": "vm", "address": "192.0.2.21/24"}
        )
        tree = Tree.load(tree.root)
        result = scaffold.create_node(tree, "vm-svc", {"id": "svc-web", "kind": "app", "port": 8080})
        address = result["node"]["addresses"][0]
        self.assertEqual(address["address"], "192.0.2.21")
        self.assertEqual(address["port"], 8080)
        self.assertEqual(address["network"], "net-lab")

    def test_create_node_rejects_bad_containment(self) -> None:
        tree = self.minimal_tree()
        with self.assertRaises(SpecError) as caught:
            scaffold.create_node(tree, "net-lab", {"id": "app-bad", "kind": "app"})
        self.assertEqual(caught.exception.code, "containment")

    def test_create_node_rolls_back_invalid_tree(self) -> None:
        tree = self.minimal_tree()
        scaffold.create_node(tree, "net-lab", {"id": "vm-a", "kind": "vm", "aliases": ["calc"]})
        tree = Tree.load(tree.root)
        with self.assertRaises(SpecError) as caught:
            scaffold.create_node(
                tree, "net-lab", {"id": "vm-b", "kind": "vm", "aliases": ["calc"]}
            )
        self.assertEqual(caught.exception.code, "invalid_tree")
        self.assertFalse((tree.nodes["net-lab"].path / "vm-b").exists())

    def test_create_node_rejects_bad_health(self) -> None:
        tree = self.minimal_tree()
        with self.assertRaises(SpecError) as caught:
            scaffold.create_node(
                tree, "net-lab", {"id": "vm-badh", "kind": "vm", "health": {"type": "bogus"}}
            )
        self.assertEqual(caught.exception.code, "bad_health")
        self.assertFalse((tree.nodes["net-lab"].path / "vm-badh").exists())

    def test_card_over_45_lines_warns(self) -> None:
        tree = self.minimal_tree()
        card = tree.nodes["net-lab"].card_path
        card.write_text(
            card.read_text(encoding="utf-8") + "\n".join(["complément"] * 25) + "\n",
            encoding="utf-8",
        )
        issues = Tree.load(tree.root).validate()
        codes = {issue["code"] for issue in issues if issue["node"] == "net-lab"}
        self.assertIn("card_too_long", codes)

    def test_card_keeps_port_inside_backticks(self) -> None:
        payload = scaffold.build_payload(
            {"id": "svc", "kind": "app", "address": "192.0.2.11", "port": 8081},
            "vm-services", "vm",
        )
        card = scaffold.card_text(payload)
        self.assertIn("`192.0.2.11:8081`", card)


class MoveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        spec = json.loads((ROOT / "examples" / "net-lab.json").read_text(encoding="utf-8"))
        scaffold.render_spec(spec, Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_move_app_rewrites_facts(self) -> None:
        tree = Tree.load(self.home)
        result = move.move_node(
            tree, tree.nodes["app-lab-dashboard"], tree.nodes["vm-lab-compute"], observe=False
        )
        moved = self.home / "net-lab" / "vm-lab-compute" / "app-lab-dashboard"
        self.assertTrue(moved.is_dir())
        self.assertFalse((self.home / "net-lab" / "vm-lab-web" / "app-lab-dashboard").exists())
        data = json.loads((moved / "node.json").read_text(encoding="utf-8"))
        self.assertEqual(data["parent"], "vm-lab-compute")
        self.assertEqual(data["addresses"][0]["address"], "192.0.2.10")
        self.assertEqual(data["addresses"][0]["port"], 8081)
        self.assertEqual(data["management"]["ssh_alias"], "lab-compute.invalid")
        self.assertEqual(data["health"]["url"], "http://192.0.2.10:8081/")
        self.assertEqual(result["address_after"], "192.0.2.10:8081")
        self.assertIn("parent", result["changes"])
        card = (moved / "AGENT.md").read_text(encoding="utf-8")
        self.assertIn("sous `vm-lab-compute`", card)
        self.assertLessEqual(len(card.splitlines()), 45)
        errors = [item for item in Tree.load(self.home).validate() if item["level"] == "error"]
        self.assertEqual(errors, [])

    def test_move_rewrites_bind_and_runtime(self) -> None:
        real_home = Path(self.tmp.name) / "real" / "home"
        shutil.copytree(ROOT / "tree" / "home", real_home)
        tree = Tree.load(real_home)
        move.move_node(
            tree, tree.nodes["app-dashboard"], tree.nodes["vm-compute"], observe=False
        )
        moved = real_home / "net-example" / "vm-compute" / "app-dashboard"
        data = json.loads((moved / "node.json").read_text(encoding="utf-8"))
        self.assertEqual(data["runtime"]["bind"], "192.0.2.10:8081->8080")
        self.assertEqual(data["health"]["url"], "http://192.0.2.10:8081/")
        self.assertEqual(data["management"]["ssh_alias"], "example-compute.invalid")

    def test_move_dry_run_writes_nothing(self) -> None:
        tree = Tree.load(self.home)
        source = self.home / "net-lab" / "vm-lab-web" / "app-lab-dashboard"
        before = (source / "node.json").read_text(encoding="utf-8")
        result = move.move_node(
            tree, tree.nodes["app-lab-dashboard"], tree.nodes["vm-lab-compute"], dry_run=True
        )
        self.assertTrue(result["dry_run"])
        self.assertTrue(source.is_dir())
        self.assertFalse((self.home / "net-lab" / "vm-lab-compute" / "app-lab-dashboard").exists())
        self.assertEqual((source / "node.json").read_text(encoding="utf-8"), before)

    def test_move_refuses_running_source(self) -> None:
        tree = Tree.load(self.home)
        with mock.patch.object(move.providers, "runtime_state", return_value={"state": "running"}):
            with self.assertRaises(move.MoveError) as context:
                move.move_node(tree, tree.nodes["app-lab-dashboard"], tree.nodes["vm-lab-compute"])
        self.assertEqual(context.exception.code, "source_running")
        self.assertTrue((self.home / "net-lab" / "vm-lab-web" / "app-lab-dashboard").exists())

    def test_move_refuses_same_parent(self) -> None:
        tree = Tree.load(self.home)
        with self.assertRaises(move.MoveError) as context:
            move.move_node(
                tree, tree.nodes["app-lab-dashboard"], tree.nodes["vm-lab-web"], observe=False
            )
        self.assertEqual(context.exception.code, "same_parent")

    def test_move_refuses_unsupported_kind(self) -> None:
        tree = Tree.load(self.home)
        with self.assertRaises(move.MoveError) as context:
            move.move_node(tree, tree.nodes["vm-lab-compute"], tree.nodes["net-lab"], observe=False)
        self.assertEqual(context.exception.code, "unsupported_kind")

    def test_move_refuses_containment(self) -> None:
        tree = Tree.load(self.home)
        with self.assertRaises(move.MoveError) as context:
            move.move_node(
                tree, tree.nodes["app-lab-dashboard"], tree.nodes["app-lab-model"], observe=False
            )
        self.assertEqual(context.exception.code, "containment")

    def test_move_rolls_back_on_invalid_tree(self) -> None:
        tree = Tree.load(self.home)
        source = self.home / "net-lab" / "vm-lab-web" / "app-lab-dashboard"
        original = {path.name: path.read_text(encoding="utf-8") for path in source.iterdir()}

        class FakeTree:
            def __init__(self, issues: list[dict[str, str]]) -> None:
                self.issues = issues

            def validate(self) -> list[dict[str, str]]:
                return self.issues

        with mock.patch.object(
            move.Tree,
            "load",
            side_effect=[
                FakeTree([]),
                FakeTree([{"level": "error", "code": "boom", "message": "boom"}]),
            ],
        ):
            with self.assertRaises(move.MoveError) as context:
                move.move_node(
                    tree, tree.nodes["app-lab-dashboard"], tree.nodes["vm-lab-compute"], observe=False
                )
        self.assertEqual(context.exception.code, "invalid_tree")
        self.assertTrue(source.is_dir())
        self.assertFalse((self.home / "net-lab" / "vm-lab-compute" / "app-lab-dashboard").exists())
        for name, text in original.items():
            self.assertEqual((source / name).read_text(encoding="utf-8"), text)


class CliTest(unittest.TestCase):
    def run_cli(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        stdin_text: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        merged = os.environ.copy()
        if env:
            merged.update(env)
        return subprocess.run(
            [sys.executable, "-m", "iacrun.cli", *args],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            env=merged,
            input=stdin_text,
        )

    def test_ls_json(self) -> None:
        proc = self.run_cli("ls", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["node"], "home")
        self.assertEqual(
            {child["id"] for child in payload["data"]["children"]},
            {"net-example", "net-external"},
        )

    def test_path(self) -> None:
        proc = self.run_cli("path", "app-model-api")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.strip().endswith("app-model-api"))

    def test_show_json(self) -> None:
        proc = self.run_cli("show", "vm-services", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["data"]["node"]["id"], "vm-services")
        self.assertIn("Mission", payload["data"]["card"])

    def test_move_dry_run_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = json.loads((ROOT / "examples" / "net-lab.json").read_text(encoding="utf-8"))
            scaffold.render_spec(spec, Path(tmp))
            proc = self.run_cli(
                "--root", str(Path(tmp) / "home"), "move", "app-lab-dashboard",
                "--to", "vm-lab-compute", "--dry-run", "--json",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertTrue(payload["data"]["dry_run"])
            self.assertEqual(payload["data"]["address_after"], "192.0.2.10:8081")
            self.assertTrue(
                (Path(tmp) / "home" / "net-lab" / "vm-lab-web" / "app-lab-dashboard").is_dir()
            )

    def test_move_requires_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = json.loads((ROOT / "examples" / "net-lab.json").read_text(encoding="utf-8"))
            scaffold.render_spec(spec, Path(tmp))
            proc = self.run_cli(
                "--root", str(Path(tmp) / "home"), "move", "app-lab-dashboard",
                "--to", "vm-lab-compute", "--json",
            )
            self.assertEqual(proc.returncode, 5, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["error"]["code"], "confirmation_required")

    def test_status_is_targeted_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = json.loads((ROOT / "examples" / "net-lab.json").read_text(encoding="utf-8"))
            scaffold.render_spec(spec, Path(tmp))
            proc = self.run_cli(
                "--root", str(Path(tmp) / "home"), "status", "net-lab", "--json"
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["data"]["state"], "unknown")
            self.assertNotIn("nodes", payload["data"])

    def test_status_recursive_covers_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = json.loads((ROOT / "examples" / "net-lab.json").read_text(encoding="utf-8"))
            scaffold.render_spec(spec, Path(tmp))
            for node_file in Path(tmp).rglob("node.json"):
                data = json.loads(node_file.read_text(encoding="utf-8"))
                data.pop("health", None)
                data.pop("runtime", None)
                data["provider"] = "none"
                node_file.write_text(json.dumps(data), encoding="utf-8")
            state_dir = Path(tmp) / "state"
            state_dir.mkdir()
            (state_dir / "desired.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "updated_at": None,
                        "nodes": {"vm-lab-web": {"power": "running"}},
                    }
                ),
                encoding="utf-8",
            )
            proc = self.run_cli(
                "--root", str(Path(tmp) / "home"), "status", "net-lab",
                "--recursive", "--json",
                env={"IACRUN_STATE_DIR": str(state_dir)},
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)["data"]
            self.assertTrue(payload["recursive"])
            ids = [row["id"] for row in payload["nodes"]]
            self.assertEqual(ids[0], "net-lab")
            self.assertIn("app-lab-dashboard", ids)
            self.assertNotIn("home", ids)
            self.assertEqual(sum(payload["counts"].values()), len(ids))
            self.assertEqual(
                {row["kind"] for row in payload["nodes"]},
                {"network", "bridge", "vm", "app", "device"},
            )
            rows = {row["id"]: row for row in payload["nodes"]}
            self.assertEqual(rows["vm-lab-web"]["declared"], "running")
            self.assertEqual(rows["vm-lab-web"]["endpoint"], "192.0.2.11/24")
            self.assertIsNone(rows["vm-lab-web"]["pid"])
            self.assertEqual(rows["app-lab-dashboard"]["port"], 8081)
            self.assertEqual(rows["app-lab-dashboard"]["declared"], "—")

    def test_ask_routes_recursive_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = json.loads((ROOT / "examples" / "net-lab.json").read_text(encoding="utf-8"))
            scaffold.render_spec(spec, Path(tmp))
            for node_file in Path(tmp).rglob("node.json"):
                data = json.loads(node_file.read_text(encoding="utf-8"))
                data.pop("health", None)
                data.pop("runtime", None)
                data["provider"] = "none"
                node_file.write_text(json.dumps(data), encoding="utf-8")
            proc = self.run_cli(
                "--root", str(Path(tmp) / "home"), "ask",
                "état global de vm-lab-web", "--json",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)["data"]
            self.assertEqual(payload["routed"], "status")
            self.assertTrue(payload["recursive"])
            self.assertIn("app-lab-dashboard", [row["id"] for row in payload["result"]["nodes"]])

    def test_doctor(self) -> None:
        proc = self.run_cli("doctor", "--no-installer", "--json")
        payload = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(payload["data"]["errors"], 0)

    def test_index(self) -> None:
        proc = self.run_cli("index", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        ids = [row["id"] for row in payload["data"]["nodes"]]
        self.assertIn("vm-services", ids)
        self.assertIn("app-dashboard", ids)

    def test_context_within_budget(self) -> None:
        proc = self.run_cli("context", "vm-services", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["data"]["within_budget"])
        self.assertIn("vm-services", payload["data"]["prompt"])
        self.assertLess(payload["data"]["tokens_estimate"], payload["data"]["budget"])

    def test_render_dry_run_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "infra"
            proc = self.run_cli(
                "render", "examples/net-lab.json", "--out", str(out),
                "--dry-run", "--yes", "--json",
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(len(payload["data"]["nodes"]), 8)
            self.assertTrue(payload["data"]["dry_run"])
            self.assertFalse(out.exists())

    def test_new_from_stdin_dry_run(self) -> None:
        spec = json.dumps({
            "id": "vm-cli-dry", "kind": "vm",
            "address": "192.0.2.14/24", "summary": "test",
        })
        with tempfile.TemporaryDirectory() as tmp:
            proc = self.run_cli(
                "new", "net-example", "--spec", "-", "--dry-run", "--yes", "--json",
                stdin_text=spec, env={"IACRUN_STATE_DIR": tmp},
            )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["data"]["node"]["provider"], "qemu")
        self.assertTrue(payload["data"]["dry_run"])
        self.assertFalse((ROOT / "tree" / "home" / "net-example" / "vm-cli-dry").exists())

    def test_new_rejects_address_outside_network(self) -> None:
        spec = json.dumps({"id": "vm-cli-bad", "kind": "vm", "address": "192.0.2.2/24"})
        with tempfile.TemporaryDirectory() as tmp:
            proc = self.run_cli(
                "new", "net-example", "--spec", "-", "--yes", "--json",
                stdin_text=spec, env={"IACRUN_STATE_DIR": tmp},
            )
        self.assertEqual(proc.returncode, 4)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"]["code"], "address_outside")
        self.assertFalse((ROOT / "tree" / "home" / "net-example" / "vm-cli-bad").exists())

    def test_new_app_inherits_descriptor(self) -> None:
        spec = json.dumps({
            "id": "app-cli-test", "kind": "app",
            "address": "192.0.2.11", "port": 8080,
            "management": {"ssh_alias": "lab-web.invalid", "container": "test"},
            "runtime": {"type": "docker", "container": "test"},
            "health": {"type": "http", "url": "http://192.0.2.11:8080/"},
        })
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "infra" / "home"
            scaffold.render_spec(
                json.loads((ROOT / "examples" / "net-lab.json").read_text(encoding="utf-8")),
                Path(tmp) / "infra",
            )
            env = {"IACRUN_ROOT": str(home), "IACRUN_STATE_DIR": str(Path(tmp) / "state")}
            proc = self.run_cli(
                "new", "vm-lab-web", "--spec", "-", "--yes", "--json",
                stdin_text=spec, env=env,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            node_file = home / "net-lab" / "vm-lab-web" / "app-cli-test" / "node.json"
            written = json.loads(node_file.read_text(encoding="utf-8"))
            for key in ("provider", "verbs", "risk"):
                self.assertNotIn(key, written)

            proc = self.run_cli("show", "app-cli-test", "--json", env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            shown = json.loads(proc.stdout)["data"]["node"]
            self.assertEqual(shown["provider"], "docker")
            self.assertIn("start", shown["verbs"])

    def test_new_rejects_removed_flags(self) -> None:
        proc = self.run_cli(
            "new", "net-example", "--id", "x", "--kind", "app", "--address", "192.0.2.1",
        )
        self.assertEqual(proc.returncode, 2)

    def test_ask_routes_read_intent(self) -> None:
        proc = self.run_cli("ask", "où tourne dashboard ?", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["data"]["routed"], "status")

    def test_ask_create_requires_scaffold(self) -> None:
        proc = self.run_cli("ask", "crée une VM dans net-example", "--json")
        self.assertEqual(proc.returncode, 3)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"]["code"], "scaffold_required")
        self.assertEqual(payload["target"], "net-example")

    def test_start_dry_run(self) -> None:
        proc = self.run_cli("start", "vm-services", "--dry-run", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["data"]["dry_run"])
        self.assertIn("/usr/bin/true", " ".join(payload["data"]["outcome"]["command"]))

    def test_start_requires_confirmation(self) -> None:
        proc = self.run_cli("start", "vm-services", "--json")
        self.assertEqual(proc.returncode, 5)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"]["code"], "confirmation_required")

    def test_state_set_and_plan(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {"IACRUN_STATE_DIR": tmp}
            proc = self.run_cli("state", "set", "vm-services", "--power", "running", env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("running", proc.stdout)
            proc = self.run_cli("state", "plan", "--json", env=env)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["data"]["plan"][0]["node"], "vm-services")

if __name__ == "__main__":
    unittest.main()
