"""Sync state machine for one shared document.

Deliberately free of FreeCAD/Qt imports: the host drives it by calling
`tick()` on the GUI thread (a QTimer in FreeCAD) and forwarding document
observer callbacks. All FreeCAD API calls happen inside `tick()` or the
observer hooks, i.e. on the GUI thread; networking runs in a background
thread that only exchanges plain dicts through a queue.
"""

import collections
import json
import queue
import threading
import time
import traceback
import uuid

from . import codec
from .wsclient import Connection

PROTOCOL_VERSION = 1
CLIENT_NAME = "FreeCAD CollabSync 0.1"
MAX_OPS_PER_SUBMIT = 500
MAX_SUBMIT_BYTES = 3 * 1024 * 1024
FLUSH_INTERVAL = 0.15
PRESENCE_INTERVAL = 0.3
RETRY_INTERNAL_AFTER = 2.0
SNAPSHOT_EVERY = 300
MAX_BACKOFF = 30
MAX_RESYNCS = 3
MAX_CHAT_KEPT = 200
MAX_CHAT_CHARS = 16000
RESYNC_WINDOW = 300

FATAL_CLOSE = {
    4002: "protocol error (plugin and server versions may not match)",
    4003: "your access to this document was removed",
    4004: "the document was deleted",
    4401: "authentication failed: check the server URL and token",
    4404: "document not found, or you are not a member",
}


class ResyncNeeded(Exception):
    pass


def write_key(op):
    kind = op["op"]
    if kind in ("add_object", "remove_object"):
        return (op["name"], "*")
    if kind == "set_label":
        return (op["name"], "Label")
    if kind == "set_expression":
        return (op["object"], "expr:" + op["path"])
    return (op["object"], op["property"])


def event_for(op):
    kind = op["op"]
    if kind == "add_object":
        return ("create", op["name"])
    if kind == "remove_object":
        return ("delete", op["name"])
    if kind == "set_label":
        return ("change", op["name"], "Label")
    if kind == "remove_property":
        return ("rmprop", op["object"], op["property"])
    if kind == "set_expression":
        return ("change", op["object"], "ExpressionEngine")
    return ("change", op["object"], op["property"])


def _chunks(ops):
    chunk, size = [], 0
    for op in ops:
        n = len(json.dumps(op, separators=(",", ":")))
        if chunk and (len(chunk) >= MAX_OPS_PER_SUBMIT or size + n > MAX_SUBMIT_BYTES):
            yield chunk
            chunk, size = [], 0
        chunk.append(op)
        size += n
    if chunk:
        yield chunk


def _default_log(level, msg):
    print(f"[collab:{level}] {msg}")


class Session:
    """
    api:       collab_sync.api.Api
    doc:       the local document being shared
    opener:    fn(document_id, name) -> (doc, seq); loads the latest snapshot
    saver:     fn(doc) -> bytes of an .FCStd copy
    selection: fn(doc) -> [object names]; for presence
    on_update: fn(session); called after state changes (UI refresh)
    """

    def __init__(self, api, document_id, name, doc, last_seen_seq, opener, saver=None,
                 log=_default_log, selection=None, on_update=None, on_chat=None, client_name=None):
        self.api = api
        self.document_id = document_id
        self.name = name
        self.doc = doc
        self.opener = opener
        self.saver = saver
        self.log = log
        self.selection = selection
        self.on_update = on_update
        self.on_chat = on_chat  # fn(session, message) for new (not historical) chat
        self.client_name = client_name or CLIENT_NAME
        self.chat = []  # recent chat, oldest first

        self.inbox = queue.Queue()
        self.conn = None
        self.gen = 0
        self.stopped = False
        self.live = False
        self.applying = False
        self.backoff = 1
        self.reconnect_at = None
        self.stop_reason = None

        self.session_id = None
        self.role = None
        self.participants = {}
        self.locks = {}
        self.selections = {}

        self._reset_sync_state(last_seen_seq)
        self.resync_times = collections.deque()
        self.viewer_warned = False
        self.presence_dirty = False
        self.last_presence = 0.0
        self.upload = None  # {"thread":…, "result":…}

    def _reset_sync_state(self, seq):
        self.last_applied = seq
        self.acked = set()
        self.pending = collections.OrderedDict()  # client_op_id -> {"base", "ops"}
        self.retries = []  # (due, client_op_id)
        self.rebases = []  # {"ops", "base", "head"}
        self.events = []
        self.first_event_at = None
        self.remote_keys = {}
        self.resync_needed = False
        self.since_snapshot = 0

    # ------------------------------------------------------------ lifecycle

    def start(self):
        self._connect()
        self._notify()

    def stop(self, reason=None):
        if self.stopped:
            return
        self.stopped = True
        self.live = False
        self.stop_reason = reason
        if self.conn:
            self.conn.stop()
            self.conn = None
        if reason:
            self.log("warning", f"disconnected: {reason}")
        self._notify()

    def _connect(self):
        self.gen += 1
        hello = {
            "type": "hello",
            "protocol_version": PROTOCOL_VERSION,
            "client": self.client_name,
            "last_seen_seq": self.last_applied,
        }
        self.conn = Connection(self.api.ws_url(self.document_id), self.api.token, hello, self.inbox, self.gen)
        self.conn.start()

    @property
    def status(self):
        if self.stopped:
            return f"Disconnected{': ' + self.stop_reason if self.stop_reason else ''}"
        if self.live:
            return f"Live · seq {self.last_applied} · {len(self.pending)} pending"
        if self.reconnect_at:
            return f"Reconnecting in {max(0, int(self.reconnect_at - time.monotonic()))} s"
        return "Connecting…"

    def _notify(self):
        if self.on_update:
            try:
                self.on_update(self)
            except Exception:
                self.log("error", traceback.format_exc())

    # ------------------------------------------------------------ main loop

    def tick(self):
        now = time.monotonic()
        changed = False
        while True:
            try:
                gen, kind, payload = self.inbox.get_nowait()
            except queue.Empty:
                break
            if gen != self.gen or self.stopped:
                continue
            changed = True
            if kind == "msg":
                self._handle(payload)
            elif kind == "closed":
                self._on_closed(*payload)

        if self.stopped:
            return
        if self.resync_needed:
            self._resync()
            return
        if self.conn is None and self.reconnect_at is not None and now >= self.reconnect_at:
            self.reconnect_at = None
            self._connect()
        if self.live:
            self._process_retries(now)
            self._process_rebases()
            if self.events and now - self.first_event_at >= FLUSH_INTERVAL:
                self._flush()
                changed = True
            if self.presence_dirty and now - self.last_presence >= PRESENCE_INTERVAL:
                self._send_presence(now)
            self._maybe_snapshot()
        self._poll_upload()
        if changed:
            self._notify()

    def _send(self, msg):
        if self.conn is None:
            return
        try:
            self.conn.send(msg)
        except Exception as e:
            self.log("debug", f"send failed: {e}")  # a "closed" event follows

    def _on_closed(self, code, reason):
        self.log("debug", f"connection closed: {code} {reason}")
        self.conn = None
        self.live = False
        if code in FATAL_CLOSE:
            self.stop(FATAL_CLOSE[code])
            return
        if code == 4000:
            self.resync_needed = True
            return
        delay = self.backoff
        self.backoff = min(self.backoff * 2, MAX_BACKOFF)
        self.reconnect_at = time.monotonic() + delay
        self.log("warning", f"connection lost ({code} {reason}); retrying in {delay} s")

    # ------------------------------------------------------------ inbound

    def _handle(self, m):
        t = m.get("type")
        if t not in ("ops", "presence", "pong"):
            self.log("debug", f"received {t}")
        if t == "welcome":
            self.log("debug", f"welcome: head seq {m['head_seq']}, {len(m['participants'])} participant(s), "
                              f"{len(m.get('chat') or [])} chat message(s) in history (history is not answered)")
            self.session_id = m["session_id"]
            self.role = m["you"]["role"]
            self.participants = {p["session_id"]: p for p in m["participants"]}
            self.locks = {lock["object"]: lock["session_id"] for lock in m["locks"]}
            self.selections = {}
            self.chat = list(m.get("chat") or [])
        elif t == "ops":
            self._apply_batch(m["batch"])
        elif t == "catch_up_complete":
            self.live = True
            self.backoff = 1
            for cid, p in self.pending.items():
                self._send({"type": "submit", "client_op_id": cid, "base_seq": p["base"], "ops": p["ops"]})
            self.presence_dirty = True
            self.log("info", f"live on '{self.name}' at seq {self.last_applied} as {self.role}")
        elif t == "ack":
            self.pending.pop(m["client_op_id"], None)
            if m["seq"] > self.last_applied:
                self.acked.add(m["seq"])
                self._advance()
            self.since_snapshot += 1
        elif t == "reject":
            self._on_reject(m)
        elif t == "resync_required":
            self.resync_needed = True
        elif t == "locked":
            for o in m["objects"]:
                self.locks[o] = m["session_id"]
        elif t == "unlocked":
            for o in m["objects"]:
                if self.locks.get(o) == m["session_id"]:
                    del self.locks[o]
        elif t == "lock_denied":
            held = ", ".join(f"{h['object']} ({self._who(h['session_id'])})" for h in m["held_by"])
            self.log("warning", f"lock denied, held by others: {held}")
        elif t == "presence":
            self.selections[m["session_id"]] = m.get("selection") or []
        elif t == "joined":
            p = m["participant"]
            self.participants[p["session_id"]] = p
            self.log("info", f"{p['user_name']} joined")
        elif t == "left":
            p = self.participants.pop(m["session_id"], None)
            self.selections.pop(m["session_id"], None)
            if p:
                self.log("info", f"{p['user_name']} left")
        elif t == "role_changed":
            self.role = m["role"]
            if self.session_id in self.participants:
                self.participants[self.session_id]["role"] = self.role
            self.viewer_warned = False
            self.log("warning", f"your role is now {self.role}")
        elif t == "chat":
            msg = m["message"]
            if not any(c["id"] == msg["id"] for c in self.chat[-50:]):
                self.chat.append(msg)
                del self.chat[:-MAX_CHAT_KEPT]
                if self.on_chat:
                    try:
                        self.on_chat(self, msg)
                    except Exception:
                        self.log("error", "chat handler failed:\n" + traceback.format_exc())
        elif t == "error":
            self.log("warning", f"server: {m.get('code')}: {m.get('message')}")

    def _who(self, session_id):
        p = self.participants.get(session_id)
        return p["user_name"] if p else "someone"

    def _advance(self):
        while self.last_applied + 1 in self.acked:
            self.last_applied += 1
            self.acked.discard(self.last_applied)

    def _apply_batch(self, batch):
        seq = batch["seq"]
        if seq <= self.last_applied or seq in self.acked:
            self.acked.discard(seq)
            self.last_applied = max(self.last_applied, seq)
            self._advance()
            return
        ops = list(batch["ops"])
        # Within the batch, apply sketch geometry before constraints (stable,
        # and only property writes move; creations/removals keep their slots).
        slots = [i for i, op in enumerate(ops) if op["op"] == "set_property"]
        ordered = sorted((ops[i] for i in slots), key=lambda op: codec.apply_order(op["property"]))
        for i, op in zip(slots, ordered):
            ops[i] = op
        self.applying = True
        try:
            for op in ops:
                if op["op"] == "add_object":
                    self._create(op)
            for op in ops:
                self._apply(op)
            self.doc.recompute()
        except ResyncNeeded as e:
            self.log("warning", str(e))
            self.resync_needed = True
        except Exception:
            self.log("error", "failed to apply remote change:\n" + traceback.format_exc())
            self.resync_needed = True
        finally:
            self.applying = False
        for op in ops:
            self.remote_keys[write_key(op)] = seq
        self.last_applied = max(self.last_applied, seq)
        self._advance()
        self.since_snapshot += 1

    def _create(self, op):
        name, type_id = op["name"], op["type_id"]
        existing = self.doc.getObject(name)
        if existing is not None:
            if existing.TypeId != type_id:
                raise ResyncNeeded(f"object name clash on {name}")
            return
        obj = self.doc.addObject(type_id, name)
        if obj.Name != name:
            self.doc.removeObject(obj.Name)
            raise ResyncNeeded(f"could not create {name} (got {obj.Name})")
        proxy = op.get("properties", {}).get("Proxy")
        if proxy is not None:
            self._decode(obj, "Proxy", proxy)

    def _decode(self, obj, prop, pv):
        try:
            codec.decode_into(obj, prop, pv)
        except Exception as e:
            self.log("warning", f"cannot apply {obj.Name}.{prop}: {e}")

    def _apply(self, op):
        kind = op["op"]
        target = op.get("name") or op.get("object")
        obj = self.doc.getObject(target)
        if kind == "remove_object":
            if obj is not None:
                self.doc.removeObject(target)
            return
        if obj is None:
            self.log("debug", f"skipping {kind} on missing object {target}")
            return
        if kind == "add_object":
            if op.get("label"):
                obj.Label = op["label"]
            for prop, pv in sorted(op.get("properties", {}).items(), key=lambda kv: codec.apply_order(kv[0])):
                if prop != "Proxy" and codec.is_synced_name(prop):
                    self._decode(obj, prop, pv)
        elif kind == "set_label":
            obj.Label = op["label"]
        elif kind == "set_property":
            if codec.is_synced_name(op["property"]):
                self._decode(obj, op["property"], op["value"])
        elif kind == "add_property":
            if op["property"] not in obj.PropertiesList:
                obj.addProperty(op["type_id"], op["property"], op.get("group") or "Base", op.get("doc") or "")
        elif kind == "remove_property":
            if op["property"] in obj.PropertiesList:
                obj.removeProperty(op["property"])
        elif kind == "set_expression":
            obj.setExpression(op["path"], op.get("expression"))

    def _on_reject(self, m):
        cid, reason = m["client_op_id"], m["reason"]
        p = self.pending.pop(cid, None)
        if p is None:
            return
        if reason == "internal":
            self.pending[cid] = p
            self.retries.append((time.monotonic() + RETRY_INTERNAL_AFTER, cid))
        elif reason == "conflict":
            self.rebases.append({"ops": p["ops"], "base": p["base"], "head": m["head_seq"]})
            self.log("info", f"concurrent edit detected; rebasing ({m['message']})")
        else:
            self.log("warning", f"change rejected ({reason}: {m['message']}); reloading shared state")
            self.resync_needed = True

    # ------------------------------------------------------------ outbound

    def _process_retries(self, now):
        due = [cid for t, cid in self.retries if t <= now]
        self.retries = [(t, cid) for t, cid in self.retries if t > now]
        for cid in due:
            p = self.pending.get(cid)
            if p:
                self._send({"type": "submit", "client_op_id": cid, "base_seq": p["base"], "ops": p["ops"]})

    def _conflicted(self, op, base):
        key = write_key(op)
        return (self.remote_keys.get(key, 0) > base
                or self.remote_keys.get((key[0], "*"), 0) > base)

    def _process_rebases(self):
        ready = [r for r in self.rebases if self.last_applied >= r["head"]]
        if not ready:
            return
        self.rebases = [r for r in self.rebases if self.last_applied < r["head"]]
        replay = []
        for r in ready:
            keep = [op for op in r["ops"] if not self._conflicted(op, r["base"])]
            lost = len(r["ops"]) - len(keep)
            if lost:
                self.log("warning", f"{lost} of your changes were overridden by a concurrent edit")
            replay += [event_for(op) for op in keep]
        if replay:
            self.events[0:0] = replay
            self.first_event_at = self.first_event_at or time.monotonic()

    def _record(self, event):
        if self.first_event_at is None or not self.events:
            self.first_event_at = time.monotonic()
        self.events.append(event)

    def on_created(self, obj):
        if self._ours(obj):
            self._record(("create", obj.Name))

    def on_deleted(self, obj):
        if self._ours(obj):
            self._record(("delete", obj.Name))

    def on_changed(self, obj, prop):
        if self._ours(obj) and (prop == "Label" or codec.is_synced(obj, prop)):
            self._record(("change", obj.Name, prop))

    def on_property_removed(self, obj, prop):
        if self._ours(obj):
            self._record(("rmprop", obj.Name, prop))

    def on_selection_changed(self):
        self.presence_dirty = True

    def _ours(self, obj):
        if self.stopped or self.applying:
            return False
        try:
            return obj.Document is self.doc or obj.Document.Name == self.doc.Name
        except Exception:
            return False

    def _compress(self, events):
        out, created, changed = [], set(), set()
        for ev in events:
            kind, name = ev[0], ev[1]
            if kind == "create":
                created.add(name)
                out.append(ev)
            elif kind in ("change", "rmprop"):
                key = (name, ev[2])
                if name in created:
                    continue
                if key in changed:
                    out = [e for e in out if not (e[0] in ("change", "rmprop") and (e[1], e[2]) == key)]
                changed.add(key)
                out.append(ev)
            elif kind == "delete":
                was_created = name in created
                out = [e for e in out if e[1] != name]
                created.discard(name)
                changed = {k for k in changed if k[0] != name}
                if not was_created:
                    out.append(ev)
        return out

    def _materialize(self, events):
        ops = []
        for ev in self._compress(events):
            kind, name = ev[0], ev[1]
            if kind == "delete":
                ops.append({"op": "remove_object", "name": name})
                continue
            obj = self.doc.getObject(name)
            if obj is None:
                continue
            if kind == "create":
                ops.append(codec.object_state(obj, self.log))
            elif kind == "rmprop":
                ops.append({"op": "remove_property", "object": name, "property": ev[2]})
            elif kind == "change":
                prop = ev[2]
                if prop == "Label":
                    ops.append({"op": "set_label", "name": name, "label": obj.Label})
                elif prop in obj.PropertiesList:
                    try:
                        ops.append({"op": "set_property", "object": name, "property": prop,
                                    "value": codec.encode(obj, prop)})
                    except Exception as e:
                        self.log("warning", f"cannot encode {name}.{prop}: {e}")
        return ops

    def _flush(self):
        events, self.events, self.first_event_at = self.events, [], None
        if self.role == "viewer":
            if not self.viewer_warned:
                self.log("warning", "you are a viewer: local changes are not shared")
                self.viewer_warned = True
            return
        ops = self._materialize(events)
        for chunk in _chunks(ops):
            cid = str(uuid.uuid4())
            self.pending[cid] = {"base": self.last_applied, "ops": chunk}
            self._send({"type": "submit", "client_op_id": cid, "base_seq": self.last_applied, "ops": chunk})

    def _send_presence(self, now):
        self.presence_dirty = False
        self.last_presence = now
        names = []
        if self.selection:
            try:
                names = list(self.selection(self.doc))[:1000]
            except Exception:
                names = []
        self._send({"type": "presence", "selection": names})

    def send_chat(self, text):
        text = (text or "").strip()
        if not text:
            return False
        if not self.live:
            self.log("warning", "not connected; message not sent")
            return False
        for i in range(0, len(text), MAX_CHAT_CHARS):
            self._send({"type": "chat", "text": text[i:i + MAX_CHAT_CHARS]})
        return True

    def lock(self, names):
        if self.live and names:
            self._send({"type": "lock", "objects": list(names)})

    def unlock(self, names):
        if self.live and names:
            self._send({"type": "unlock", "objects": list(names)})

    # ------------------------------------------------------------ snapshots

    @property
    def idle(self):
        return self.live and not (self.pending or self.events or self.rebases or self.retries)

    def _maybe_snapshot(self):
        if self.since_snapshot >= SNAPSHOT_EVERY and self.idle and self.upload is None:
            self.upload_snapshot()

    def upload_snapshot(self):
        """Upload the current state. Returns False if not in a clean state."""
        if not self.idle or self.upload is not None or self.saver is None:
            return False
        seq = self.last_applied
        try:
            data = self.saver(self.doc)
        except Exception as e:
            self.log("error", f"could not save snapshot: {e}")
            return False
        self.since_snapshot = 0
        box = {"result": None}

        def work():
            try:
                self.api.put_snapshot(self.document_id, seq, data)
                box["result"] = ("ok", seq)
            except Exception as e:
                box["result"] = ("error", str(e))

        t = threading.Thread(target=work, daemon=True, name="collab-snapshot")
        self.upload = {"thread": t, "box": box}
        t.start()
        return True

    def _poll_upload(self):
        if self.upload and not self.upload["thread"].is_alive():
            kind, detail = self.upload["box"]["result"] or ("error", "unknown")
            if kind == "ok":
                self.log("info", f"snapshot uploaded at seq {detail}")
            elif "409" not in detail:
                self.log("warning", f"snapshot upload failed: {detail}")
            self.upload = None

    # ------------------------------------------------------------ resync

    def _resync(self):
        now = time.monotonic()
        while self.resync_times and now - self.resync_times[0] > RESYNC_WINDOW:
            self.resync_times.popleft()
        if len(self.resync_times) >= MAX_RESYNCS:
            self.stop("repeated resync failures; reconnect manually")
            return
        self.resync_times.append(now)

        if self.conn:
            self.conn.stop()
            self.conn = None
            self.gen += 1
        self.live = False
        self.log("warning", "reloading the shared document; unsent local changes are discarded")
        old = self.doc
        try:
            doc, seq = self.opener(self.document_id, self.name)
        except Exception as e:
            self.log("error", f"reload failed: {e}")
            self.resync_times.pop()
            self.reconnect_at = None
            self.resync_needed = True
            self.stop(f"reload failed: {e}")
            return
        if doc is not old:
            try:
                old.Label = f"{old.Label} (detached)"
            except Exception:
                pass
        self.doc = doc
        self._reset_sync_state(seq)
        self._connect()
        self._notify()
