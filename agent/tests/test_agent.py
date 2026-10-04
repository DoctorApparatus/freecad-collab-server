"""End-to-end test of the AI participant against a real server and fake model APIs.

FreeCAD is replaced by the in-memory document from the plugin test; the model
is replaced by local HTTP servers speaking the Anthropic and OpenAI formats.
"""
import http.server
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugin" / "CollabSync"))
sys.path.insert(0, str(ROOT / "plugin" / "tests"))
sys.path.insert(0, str(ROOT / "agent"))

import test_sync  # noqa: E402
from test_sync import BIN, FakeDoc, FakeObj  # noqa: E402

from collab_agent import tools  # noqa: E402
from collab_sync import datasheet as ds  # noqa: E402
from collab_agent.agent import Agent, Config  # noqa: E402
from collab_sync.api import Api  # noqa: E402
from collab_sync.session import Session  # noqa: E402


# Attribute access to properties, like real FreeCAD objects.
def _getattr(self, name):
    props = self.__dict__.get("_props", {})
    if name in props:
        return props[name][1]
    raise AttributeError(name)


def _setattr(self, name, value):
    if "_props" in self.__dict__ and name in self._props and not name.startswith("_"):
        self.set(name, value)
    else:
        object.__setattr__(self, name, value)


def _set_expression(self, path, expr):
    eng = [e for e in (self._props.get("ExpressionEngine", (None, []))[1] or []) if e[0] != path]
    if expr:
        eng.append([path, expr])
    self._props["ExpressionEngine"] = ("App::PropertyExpressionEngine", eng)
    self.Document._changed(self, "ExpressionEngine")


FakeObj.__getattr__ = _getattr
FakeObj.__setattr__ = _setattr
FakeObj.setExpression = _set_expression
FakeDoc.Objects = property(lambda self: list(self.objects.values()))


class FakeSheet(FakeObj):
    """Just enough of Spreadsheet::Sheet for the datasheet module; cells sync as properties."""

    def __init__(self, doc, type_id, name):
        super().__init__(doc, type_id, name)
        self._props["cells"] = ("Spreadsheet::PropertySheet", {})
        self._props["aliases"] = ("App::PropertyMap", {})

    def _put(self, prop, key, value):
        d = dict(self._props[prop][1])
        d[key] = value
        self._props[prop] = (self._props[prop][0], d)
        self.Document._changed(self, prop)

    def set(self, cell, content):  # Spreadsheet API, not FakeObj.set
        self._put("cells", cell, content)

    def setAlias(self, cell, alias):
        self._put("aliases", cell, alias)

    def getAlias(self, cell):
        return self._props["aliases"][1].get(cell, "")

    def getContents(self, cell):
        return self._props["cells"][1].get(cell, "")

    def getCellFromAlias(self, alias):
        return next((c for c, a in self._props["aliases"][1].items() if a == alias), None)

    def get(self, alias):
        return self.evalExpression(self.getContents(self.getCellFromAlias(alias)).lstrip("="))

    def evalExpression(self, expr):
        expr = re.sub(r"Datasheet\.(\w+)", lambda m: str(self.get(m.group(1))), expr)
        aliases = set(self._props["aliases"][1].values())
        expr = re.sub(r"\b([a-z_]\w*)\b", lambda m: str(self.get(m.group(1))) if m.group(1) in aliases
                      else m.group(1), expr)
        expr = re.sub(r"\s*\b(mm|deg)\b", "", expr)
        return float(eval(expr, {"__builtins__": {}}))  # noqa: S307 - test helper

    def setStyle(self, *a):
        pass

    def setColumnWidth(self, *a):
        pass

    def recompute(self):
        pass


_orig_add = FakeDoc.addObject


def _add_object(self, type_id, name):
    if type_id != "Spreadsheet::Sheet":
        return _orig_add(self, type_id, name)
    final = name if name not in self.objects else name + "001"
    obj = FakeSheet(self, type_id, final)
    self.objects[final] = obj
    if self.session:
        self.session.on_created(obj)
    return obj


FakeDoc.addObject = _add_object


class FakeModel(http.server.BaseHTTPRequestHandler):
    """Scripted model: first call asks for a tool, second call answers."""
    requests = []

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeModel.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        if self.path == "/v1/messages":
            msgs = body["messages"]
            n = sum(1 for m in msgs if isinstance(m["content"], list) and m["content"]
                    and m["content"][0].get("type") == "tool_result")
            policy = any(isinstance(m["content"], str) and "Policy check failed" in m["content"] for m in msgs)
            tool = lambda i, name, args: {"type": "tool_use", "id": f"tu_{i}", "name": name, "input": args}  # noqa
            if n == 0:   # tries a plain number: must be rejected
                blocks = [tool(1, "set_properties", {"name": "Box", "properties": {"Length": 25}})]
            elif n == 1:
                blocks = [tool(2, "set_parameters", {"parameters": [
                    {"name": "box_length", "value": "25 mm", "description": "Box length"}]})]
            elif n == 2:
                blocks = [tool(3, "set_properties", {"name": "Box", "properties": {"Length": "box_length"}})]
            elif n == 3 and not policy:  # forgets Width and the generic label: the policy check catches it
                blocks = [{"type": "text", "text": "Done."}]
            elif n == 3:
                blocks = [tool(4, "bind_to_parameter", {"object": "Box", "property": "Width",
                                                        "parameter": "box_width", "description": "Box width"}),
                          tool(5, "rename_object", {"name": "Box", "label": "Test block"})]
            else:
                blocks = [{"type": "text", "text": "Box is now 25 mm long, driven by box_length."}]
            out = {"content": blocks, "stop_reason": "tool_use" if blocks[0]["type"] == "tool_use" else "end_turn"}
        else:
            done = any(m["role"] == "tool" for m in body["messages"])
            msg = ({"role": "assistant", "content": "Width set to 7 mm."} if done else
                   {"role": "assistant", "content": None, "tool_calls": [
                       {"id": "call_1", "type": "function",
                        "function": {"name": "set_properties",
                                     "arguments": json.dumps({"name": "Box", "properties": {"Width": 7}})}}]})
            out = {"choices": [{"message": msg, "finish_reason": "stop" if done else "tool_calls"}]}
        data = json.dumps(out).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class AgentTest(test_sync.SyncTest):
    test_end_to_end = None  # covered by plugin/tests/test_sync.py

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        out = subprocess.run([BIN, "create-user", "designbot", "--agent"], env=cls.env, check=True,
                             capture_output=True, text=True).stdout
        cls.tokens["designbot"] = out.split("token:")[1].strip()
        cls.model = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeModel)
        threading.Thread(target=cls.model.serve_forever, daemon=True).start()
        cls.model_url = f"http://127.0.0.1:{cls.model.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.model.shutdown()
        cls.model.server_close()
        super().tearDownClass()

    def make_agent(self, provider, base_url, policy=True):
        os.environ.update({
            "COLLAB_AGENT_DEBUG": "1",
            "COLLAB_AGENT_DATASHEET": "Datasheet" if policy else "off",
            "COLLAB_AGENT_ENFORCE_NAMES": "1" if policy else "0",
            "COLLAB_SERVER_URL": self.base, "COLLAB_AGENT_TOKEN": self.tokens["designbot"],
            "COLLAB_AGENT_PROVIDER": provider, "COLLAB_AGENT_MODEL": "test-model",
            "COLLAB_AGENT_API_KEY": "sk-test", "COLLAB_AGENT_BASE_URL": base_url,
        })
        cfg = Config()
        self.assertEqual(cfg.validate(), [])
        api = Api(self.base, cfg.token)

        def make_session(d):
            doc = FakeDoc()
            s = Session(api, d["id"], d["name"], doc, 0, opener=None, log=lambda *a: None)
            doc.session = s
            return s
        return Agent(cfg, api, "designbot", make_session, lambda lvl, m: self.logs.append(("agent", lvl, m)))

    def pump_all(self, sessions, agent, until, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for s in sessions:
                s.tick()
            agent.step()
            if until():
                return
            time.sleep(0.02)
        self.fail(f"condition not reached; logs: {self.logs[-10:]}")

    def test_agent_edits_and_replies(self):
        self.logs = []
        alice_api = self.api("alice")
        doc_id = alice_api.create_document("Gearbox")["id"]
        alice_api.set_member(doc_id, "designbot", "editor")
        a = self.join("alice", doc_id, FakeDoc(), 0)
        self.pump([a], lambda: a.live)
        box = a.doc.addObject("Part::Box", "Box")
        box.set("Length", 10)
        box.set("Width", 4)
        self.pump([a], lambda: a.idle)

        for provider, url, prop, reply in (
            ("anthropic", self.model_url, "Length", "Box is now 25 mm long, driven by box_length."),
            ("openai", self.model_url + "/v1", "Width", "Width set to 7 mm."),
        ):
            FakeModel.requests.clear()
            agent = self.make_agent(provider, url, policy=(provider == "anthropic"))
            self.pump_all([a], agent, lambda: any(s.live for s in agent.sessions.values()))
            bot = next(iter(agent.sessions.values()))
            self.pump_all([a], agent, lambda: bot.doc.getObject("Box") is not None)

            # Not addressed: ignored.
            a.send_chat("lunch at noon?")
            self.pump_all([a], agent, lambda: len(a.chat) and a.chat[-1]["text"] == "lunch at noon?")
            for _ in range(20):
                a.tick(); agent.step(); time.sleep(0.01)
            self.assertEqual(FakeModel.requests, [])

            a.send_chat(f"@designbot please change the box {prop.lower()}")
            self.pump_all([a], agent, lambda: a.chat and a.chat[-1]["user_name"] == "designbot"
                          and a.idle and bot.idle)
            self.assertEqual(a.chat[-1]["text"], reply)
            self.assertEqual(a.chat[-1]["user_kind"], "agent")
            box_a = a.doc.getObject("Box")
            if provider == "anthropic":
                self.assertEqual(len(FakeModel.requests), 6)
                # The plain number was refused with guidance.
                first_result = FakeModel.requests[1][2]["messages"][-1]["content"][0]
                self.assertTrue(first_result["is_error"])
                self.assertIn("plain number", first_result["content"])
                # The end-of-turn policy check pushed the model to fix Width and the label.
                self.assertTrue(any(isinstance(m["content"], str) and "Policy check failed" in m["content"]
                                    for m in FakeModel.requests[4][2]["messages"]))
                # Alice's copy: values come from her synced Datasheet, label set by the agent.
                engine = dict(map(tuple, box_a.get("ExpressionEngine")))
                self.assertEqual(engine, {"Length": "Datasheet.box_length", "Width": "Datasheet.box_width"})
                self.assertEqual(box_a.Label, "Test block")
                params = {p["name"]: p["value"] for p in ds.list_parameters(a.doc)}
                self.assertEqual(params, {"box_length": "25 mm", "box_width": "4 mm"})
            else:
                self.assertEqual(len(FakeModel.requests), 2)
                self.assertEqual(box_a.get("Width"), 7)
            path, headers, body = FakeModel.requests[0]
            self.assertTrue(headers.get("user-agent", "").startswith("freecad-collab-agent/"))
            if provider == "anthropic":
                self.assertEqual(headers.get("x-api-key"), "sk-test")
                self.assertEqual(headers.get("anthropic-version"), "2023-06-01")
                self.assertIn("Box", body["messages"][0]["content"])  # model overview in context
                self.assertIn("datasheet", body["system"])
                self.assertTrue(any(t["name"] == "create_sketch" for t in body["tools"]))
                self.assertFalse(any(t["name"] == "run_python" for t in body["tools"]))
                self.assertEqual(FakeModel.requests[1][2]["messages"][-1]["content"][0]["tool_use_id"], "tu_1")
            else:
                self.assertEqual(headers.get("authorization"), "Bearer sk-test")
                self.assertEqual(FakeModel.requests[1][2]["messages"][-1]["tool_call_id"], "call_1")
            agent.shutdown()
            self.pump([a], lambda: len(a.participants) == 1)

        errors = [m for m in self.logs if m[1] == "error"]
        self.assertFalse(errors, errors)

    def test_viewer_agent_gets_read_only_tools(self):
        specs = {t["name"] for t in tools.tool_specs(allow_python=True, can_edit=False)}
        self.assertEqual(specs, {"get_document_overview", "get_object", "get_parameters",
                                 "export_parameters_csv", "audit_model"})
        specs = {t["name"] for t in tools.tool_specs(allow_python=True, can_edit=True)}
        self.assertIn("run_python", specs)


class PolicyUnitTest(unittest.TestCase):
    def ctx(self):
        doc = FakeDoc()
        return tools.ToolContext(doc, True, False, [], tools.Policy("Datasheet", True))

    def test_dimensions_must_be_parameters(self):
        c = self.ctx()
        with self.assertRaisesRegex(tools.ToolError, "plain number"):
            tools._dim(c, 12.5, "x")
        with self.assertRaisesRegex(tools.ToolError, "unknown parameter"):
            tools._dim(c, "wall", "x")
        ds.set_parameters(c.doc, [{"name": "wall", "value": "2,5 mm"}, {"name": "inner", "value": "=wall * 4"}])
        self.assertEqual(tools._dim(c, "wall", "x"), ("Datasheet.wall", 2.5))
        self.assertEqual(tools._dim(c, "inner + 2 * wall", "x"), ("Datasheet.inner + 2 * Datasheet.wall", 15.0))
        self.assertEqual(tools._dim(c, 0, "x"), (None, 0.0))
        with self.assertRaisesRegex(tools.ToolError, "plain value"):
            tools._dim(c, "3 mm", "x")

    def test_names(self):
        c = self.ctx()
        for bad in ("Box001", "pad", "Sketch2", "base plate", ""):
            with self.assertRaises(tools.ToolError, msg=bad):
                tools._check_new_name(c, bad, "Box")
        self.assertEqual(tools._check_new_name(c, "BasePlate_Sketch", "Sketch"), "BasePlate_Sketch")
        self.assertEqual(tools._humanize("MountingHole_Pocket"), "Mounting hole pocket")

    def test_csv_round_trip(self):
        c = self.ctx()
        text = "Name;Wert;Einheit;Beschreibung\nplate_length;40;mm;Länge\nhole_d;3,4;mm;M3\nbad name;1;;\n"
        written, errors = ds.import_csv(c.doc, text)
        self.assertEqual(written, ["plate_length", "hole_d"])
        self.assertIn("bad name", errors)
        out = ds.export_csv(c.doc)
        self.assertEqual(out.splitlines(), ["name,value,description", "plate_length,40 mm,Länge", "hole_d,3.4 mm,M3"])
        self.assertEqual(ds.parse_csv("a,1 mm,x\n")[0], {"name": "a", "value": "1 mm", "description": "x"})

    def test_audit_flags_literal_dimensions_and_generic_labels(self):
        c = self.ctx()
        box = c.doc.addObject("Part::Box", "Box")
        box.set("Length", 10)
        issues = tools.audit(c)
        self.assertEqual({i["issue"] for i in issues}, {"generic label", "dimension not from datasheet"})
        tools.t_bind_to_parameter(c, "Box", "Length", "block_length")
        tools.t_rename_object(c, "Box", "Spacer block")
        self.assertEqual(tools.audit(c), [])
        self.assertEqual(ds.list_parameters(c.doc)[0]["value"], "10 mm")


if __name__ == "__main__":
    unittest.main(verbosity=2)
