"""End-to-end test of the plugin's sync core against a real server.

FreeCAD is replaced by a tiny in-memory document that implements the parts of
the App API the session uses. Run via `just plugin-test` (builds and starts the
server automatically).
"""

import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugin" / "CollabSync"))

from collab_sync import session as session_mod  # noqa: E402
from collab_sync.api import Api  # noqa: E402
from collab_sync.session import Session  # noqa: E402

BIN = ROOT / "target" / "debug" / "freecad-collab-server"
ZIP = b"PK\x03\x04"


class FakeObj:
    def __init__(self, doc, type_id, name):
        self.Document, self.TypeId, self.Name = doc, type_id, name
        self._label = name
        self._props = {"Placement": ("App::PropertyPlacement", [0, 0, 0])}

    @property
    def Label(self):
        return self._label

    @Label.setter
    def Label(self, v):
        self._label = v
        self.Document._changed(self, "Label")

    @property
    def PropertiesList(self):
        return list(self._props)

    def getTypeIdOfProperty(self, p):
        return self._props[p][0]

    def getPropertyStatus(self, p):
        return []

    def addProperty(self, type_id, name, group="Base", doc=""):
        self._props[name] = (type_id, None)

    def removeProperty(self, name):
        self._props.pop(name, None)

    def dumpPropertyContent(self, p):
        return json.dumps(self._props[p][1]).encode()

    def restorePropertyContent(self, p, data):
        self._props[p] = (self._props[p][0], json.loads(data))
        self.Document._changed(self, p)

    def set(self, p, value, type_id="App::PropertyLength"):
        t = self._props.get(p, (type_id, None))[0]
        self._props[p] = (t, value)
        self.Document._changed(self, p)

    def get(self, p):
        return self._props[p][1]


class FakeDoc:
    count = 0

    def __init__(self, state=None):
        FakeDoc.count += 1
        self.Name = f"Doc{FakeDoc.count}"
        self.Label = self.Name
        self.objects = {}
        self.session = None
        for name, o in (state or {}).items():
            obj = FakeObj(self, o["type"], name)
            obj._label = o["label"]
            obj._props = {k: tuple(v) for k, v in o["props"].items()}
            self.objects[name] = obj

    def _changed(self, obj, prop):
        if self.session:
            self.session.on_changed(obj, prop)

    def getObject(self, name):
        return self.objects.get(name)

    def addObject(self, type_id, name):
        final = name if name not in self.objects else name + "001"
        obj = FakeObj(self, type_id, final)
        self.objects[final] = obj
        if self.session:
            self.session.on_created(obj)
        return obj

    def removeObject(self, name):
        obj = self.objects.pop(name)
        if self.session:
            self.session.on_deleted(obj)

    def recompute(self):
        pass

    def state(self):
        return {n: (o.TypeId, o.Label, sorted(o._props.items())) for n, o in sorted(self.objects.items())}

    def dump(self):
        data = {n: {"type": o.TypeId, "label": o.Label, "props": o._props} for n, o in self.objects.items()}
        return ZIP + json.dumps(data).encode()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class SyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        port = free_port()
        cls.env = dict(
            os.environ,
            COLLAB_DATABASE_URL=f"sqlite://{cls.tmp.name}/t.db",
            COLLAB_BIND_ADDR=f"127.0.0.1:{port}",
            RUST_LOG="warn",
        )
        cls.base = f"http://127.0.0.1:{port}"
        cls.tokens = {}
        for name in ("alice", "bob", "carol"):
            out = subprocess.run([BIN, "create-user", name], env=cls.env, check=True,
                                 capture_output=True, text=True).stdout
            cls.tokens[name] = out.split("token:")[1].strip()
        cls.server = subprocess.Popen([BIN, "serve"], env=cls.env)
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.1).close()
                break
            except OSError:
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.server.terminate()
        cls.server.wait(10)
        cls.tmp.cleanup()

    def api(self, who):
        return Api(self.base, self.tokens[who])

    def opener_for(self, api):
        def opener(doc_id, name):
            snap = api.get_snapshot(doc_id)
            if snap is None:
                return FakeDoc(), 0
            seq, data = snap
            return FakeDoc(json.loads(data[len(ZIP):])), seq
        return opener

    def join(self, who, doc_id, doc=None, seq=None):
        api = self.api(who)
        if doc is None:
            doc, seq = self.opener_for(api)(doc_id, "shared")
        s = Session(api, doc_id, "shared", doc, seq, opener=self.opener_for(api),
                    saver=lambda d: d.dump(), log=lambda lvl, m: self.logs.append((who, lvl, m)))
        doc.session = s
        orig_reset = s._reset_sync_state

        def reset(seq):  # keep FakeDoc.session in sync across resyncs
            orig_reset(seq)
            s.doc.session = s
        s._reset_sync_state = reset
        s.start()
        return s

    def pump(self, sessions, until, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for s in sessions:
                s.tick()
            if until():
                return
            time.sleep(0.02)
        self.fail("condition not reached; states: " + " | ".join(
            f"{s.status} {s.doc.state()}" for s in sessions))

    def settled(self, *sessions):
        return all(s.idle for s in sessions) and len({repr(s.doc.state()) for s in sessions}) == 1

    def test_end_to_end(self):
        self.logs = []
        alice_api = self.api("alice")
        remote = alice_api.create_document("Bracket")
        doc_id = remote["id"]
        alice_api.set_member(doc_id, "bob", "editor")
        alice_api.set_member(doc_id, "carol", "viewer")

        a_doc = FakeDoc()
        alice_api.put_snapshot(doc_id, 0, a_doc.dump())
        a = self.join("alice", doc_id, a_doc, 0)
        b = self.join("bob", doc_id)
        self.pump([a, b], lambda: a.live and b.live)

        # 1. create + property propagates
        box = a.doc.addObject("Part::Box", "Box")
        box.set("Length", 10)
        self.pump([a, b], lambda: self.settled(a, b) and b.doc.getObject("Box") is not None)
        self.assertEqual(b.doc.getObject("Box").get("Length"), 10)

        # 2. same property concurrently -> converge on one value
        a.doc.getObject("Box").set("Length", 20)
        b.doc.getObject("Box").set("Length", 30)
        self.pump([a, b], lambda: self.settled(a, b))
        self.assertIn(a.doc.getObject("Box").get("Length"), (20, 30))
        self.assertTrue(any("rebasing" in m for _, _, m in self.logs), "conflict path not exercised")

        # 3. different properties concurrently -> both kept
        a.doc.getObject("Box").set("Width", 5)
        b.doc.getObject("Box").set("Height", 7)
        self.pump([a, b], lambda: self.settled(a, b)
                  and a.doc.getObject("Box")._props.get("Height", (0, 0))[1] == 7
                  and b.doc.getObject("Box")._props.get("Width", (0, 0))[1] == 5)

        # 4. labels, nested creation in one flush, deletion
        a.doc.getObject("Box").Label = "Base plate"
        cyl = b.doc.addObject("Part::Cylinder", "Cylinder")
        cyl.set("Radius", 3)
        self.pump([a, b], lambda: self.settled(a, b) and a.doc.getObject("Cylinder") is not None)
        self.assertEqual(b.doc.getObject("Box").Label, "Base plate")
        b.doc.removeObject("Cylinder")
        self.pump([a, b], lambda: self.settled(a, b) and a.doc.getObject("Cylinder") is None)

        # 5. snapshot + late joiner (viewer) gets identical state
        self.assertTrue(a.upload_snapshot())
        self.pump([a, b], lambda: a.upload is None)
        c = self.join("carol", doc_id)
        self.pump([a, b, c], lambda: c.live and self.settled(a, b, c))

        # viewers' local edits are not shared
        c.doc.getObject("Box").set("Length", 999)
        for _ in range(20):
            for s in (a, b, c):
                s.tick()
            time.sleep(0.02)
        self.assertNotEqual(a.doc.getObject("Box").get("Length"), 999)

        # 6. connection drop: edits made while offline are sent after reconnect
        session_mod.MAX_BACKOFF = 1
        a.conn.ws.sock.shutdown(socket.SHUT_RDWR)
        self.pump([a], lambda: not a.live)
        a.doc.getObject("Box").set("Length", 42)
        self.pump([a, b], lambda: a.live and self.settled(a, b)
                  and b.doc.getObject("Box").get("Length") == 42, timeout=15)

        for s in (a, b, c):
            s.stop()
        self.assertFalse([m for m in self.logs if m[1] == "error"], self.logs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
