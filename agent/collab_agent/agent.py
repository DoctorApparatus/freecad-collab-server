"""The AI participant: joins every document it is invited to, listens to the
chat, and answers requests by working on the model through tools.

Threads:
  main thread   - ticks sync sessions, runs FreeCAD tool calls (FreeCAD is
                  not thread-safe), handles document discovery results
  turn workers  - one per active request; talk to the model and ask the main
                  thread to run tools
  discovery     - polls the server for documents the agent can open
"""

import json
import os
import queue
import re
import threading
import time
import traceback

from . import tools
from .llm import LLMError, make_provider

# ------------------------------------------------------------------ config


def _load_env_file(path):
    """KEY=VALUE lines; within the file the last value wins (an empty
    `KEY=` from the template doesn't hide a value set further down).
    Real environment variables take precedence over the file."""
    values = {}
    with open(path, "r", encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if value or key.strip() not in values:
                values[key.strip()] = value
    for key, value in values.items():
        if not os.environ.get(key):
            os.environ[key] = value


class Config:
    def __init__(self):
        env_file = os.environ.get("COLLAB_AGENT_ENV_FILE")
        if env_file:
            _load_env_file(env_file)
        g = os.environ.get
        self.server_url = (g("COLLAB_SERVER_URL") or "http://127.0.0.1:14008").rstrip("/")
        self.token = g("COLLAB_AGENT_TOKEN", "")
        self.provider = (g("COLLAB_AGENT_PROVIDER") or "anthropic").lower()
        self.model = g("COLLAB_AGENT_MODEL", "")
        self.base_url = g("COLLAB_AGENT_BASE_URL", "")
        self.api_key = g("COLLAB_AGENT_API_KEY") or (
            g("ANTHROPIC_API_KEY") if self.provider == "anthropic" else g("OPENAI_API_KEY")) or ""
        self.respond = (g("COLLAB_AGENT_RESPOND") or "mention").lower()
        self.allow_python = g("COLLAB_AGENT_ALLOW_PYTHON", "0").lower() in ("1", "true", "yes")
        self.max_steps = int(g("COLLAB_AGENT_MAX_STEPS") or 16)
        self.max_turns_per_hour = int(g("COLLAB_AGENT_MAX_TURNS_PER_HOUR") or 60)
        self.max_tokens = int(g("COLLAB_AGENT_MAX_TOKENS") or 4096)
        self.instructions = g("COLLAB_AGENT_INSTRUCTIONS", "")
        docs = g("COLLAB_AGENT_DOCUMENTS", "")
        self.documents = {d.strip() for d in docs.split(",") if d.strip()}
        self.poll_seconds = int(g("COLLAB_AGENT_POLL_SECONDS") or 30)
        sheet = (g("COLLAB_AGENT_DATASHEET") or "Datasheet").strip()
        self.datasheet = None if sheet.lower() in ("0", "off", "false", "no", "none") else sheet
        self.enforce_names = g("COLLAB_AGENT_ENFORCE_NAMES", "1").lower() in ("1", "true", "yes")
        self.name_pattern = g("COLLAB_AGENT_NAME_PATTERN") or tools.DEFAULT_NAME_PATTERN
        self.policy_retries = int(g("COLLAB_AGENT_POLICY_RETRIES") or 3)
        self.debug = g("COLLAB_AGENT_DEBUG", "0").lower() in ("1", "true", "yes") or \
            g("COLLAB_AGENT_LOG_LEVEL", "").lower() == "debug"

    def policy(self):
        return tools.Policy(self.datasheet, self.enforce_names, self.name_pattern)

    def validate(self):
        problems = []
        if not self.token:
            problems.append("COLLAB_AGENT_TOKEN is not set (create an AI agent user in the admin console)")
        if self.provider not in ("anthropic", "openai"):
            problems.append("COLLAB_AGENT_PROVIDER must be anthropic or openai")
        if self.provider == "anthropic" and not self.api_key:
            problems.append("COLLAB_AGENT_API_KEY is not set")
        if self.provider == "openai" and not self.model:
            problems.append("COLLAB_AGENT_MODEL is required for the openai provider")
        if self.respond not in ("mention", "all"):
            problems.append("COLLAB_AGENT_RESPOND must be mention or all")
        if self.datasheet and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", self.datasheet):
            problems.append("COLLAB_AGENT_DATASHEET must be a valid object name like Datasheet")
        try:
            re.compile(self.name_pattern)
        except re.error as e:
            problems.append(f"COLLAB_AGENT_NAME_PATTERN is not a valid regex: {e}")
        return problems


# ------------------------------------------------------------------ prompt

SYSTEM_PROMPT = """You are {name}, an AI participant in a shared FreeCAD model. Several people edit this \
document at the same time; everything you change appears on their screens within a second.

How to work:
- Units are millimeters and degrees unless someone says otherwise.
- Look before you change: use get_document_overview / get_object to find the right objects. "This", \
"that" or "the selected part" usually means what the person has selected (listed below).
- For solid parts prefer a PartDesign body: create_body, create_sketch in it, extrude_sketch (pad/pocket), \
dress_up. Part primitives and boolean are fine for quick shapes.
- Change only what was asked. Never delete or rebuild other people's work unless explicitly asked.
- After changes, check the tool result for "problems" and fix them.
- If the request is ambiguous or risky (deleting, large changes), ask one short question instead of guessing.
{rules}
- Reply in the language the person used. Keep replies short: what you did, with object labels and key \
dimensions, or the answer to their question. No markdown headings.
{extra}
Document: {document}
Your access: {access}
People online and their selection:
{people}
"""


RULE_DATASHEET = """- Every dimension comes from the datasheet spreadsheet "{sheet}". Before modelling, define each \
size, position, angle and count with set_parameters (snake_case name, value with unit, short description), \
then pass parameter names or expressions over them (e.g. "plate_length / 2") wherever a number is needed. \
Plain numbers are rejected by the tools (0 is allowed). Reuse existing parameters; derived values can be \
formulas in the datasheet (value "=plate_length - 2 * wall"). If the user gives a CSV, use \
import_parameters_csv. When asked to change a size, change the parameter, not the feature.
"""
RULE_NAMES = """- You name everything you create: a descriptive PascalCase name saying what it is \
(BasePlate, BasePlate_Sketch, MountingHole_Pocket, Edge_Fillet) and a readable label ("Base plate"). \
Generic names like Box001, Pad or Sketch002 are rejected. Rename unclear labels you touch with rename_object.
"""


def _short(value, limit=300):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + f"… ({len(text)} chars)"


def make_logger(debug):
    """Timestamped lines on stdout (systemd puts them in the journal)."""
    levels = {"debug": 10, "info": 20, "warning": 30, "error": 40}
    threshold = 10 if debug else 20
    lock = threading.Lock()

    def log(level, msg):
        if levels.get(level, 20) < threshold:
            return
        stamp = time.strftime("%H:%M:%S")
        with lock:
            print(f"{stamp} {level.upper():<7} {msg}", flush=True)

    return log


def _people_text(session):
    lines = []
    for sid, p in session.participants.items():
        if sid == session.session_id:
            continue
        sel = session.selections.get(sid) or []
        what = ", ".join(sel[:10]) if sel else "nothing selected"
        tag = " (AI)" if p.get("kind") == "agent" else ""
        lines.append(f"- {p['user_name']}{tag}, {p['role']}: {what}")
    return "\n".join(lines) or "- nobody else"


# ------------------------------------------------------------------ agent


class Agent:
    def __init__(self, cfg, api, name, make_session, log, close_document=None):
        """
        make_session(document) -> Session (not started); document is the REST dict.
        close_document(session) -> None; closes the local FreeCAD document.
        """
        self.cfg = cfg
        self.api = api
        self.name = name
        self.make_session = make_session
        self.close_document = close_document or (lambda s: None)
        self.log = log
        self.provider = make_provider(cfg)
        self.sessions = {}  # document_id -> Session
        self.busy = {}  # document_id -> worker thread
        self.backlog = {}  # document_id -> [chat message]
        self.touched = {}  # document_id -> [names]
        self.main_calls = queue.Queue()
        self.discovered = queue.Queue()
        self.turn_times = []
        self.stopping = threading.Event()
        self.mention = re.compile(rf"(^|[^\w@])@?{re.escape(name)}\b", re.IGNORECASE)
        self._last_poll = 0.0

    # -------------------------------------------------------------- main loop

    def run(self, tick_seconds=0.03):
        self.log("info", f"{self.name} is starting (provider {self.cfg.provider}, "
                         f"python tool {'on' if self.cfg.allow_python else 'off'})")
        try:
            while not self.stopping.is_set():
                self.step()
                time.sleep(tick_seconds)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()

    def step(self):
        now = time.monotonic()
        if now - self._last_poll >= self.cfg.poll_seconds:
            self._last_poll = now
            threading.Thread(target=self._discover, daemon=True, name="agent-discover").start()
        while True:
            try:
                docs = self.discovered.get_nowait()
            except queue.Empty:
                break
            self._sync_documents(docs)
        for doc_id, s in list(self.sessions.items()):
            s.tick()
            if s.stopped:
                self.log("warning", f"left document {s.name}: {s.stop_reason}")
                self.sessions.pop(doc_id, None)
                self.close_document(s)
        self._run_main_calls()
        for doc_id in list(self.backlog):
            if doc_id not in self.busy and self.backlog[doc_id]:
                self._start_turn(doc_id, self.backlog[doc_id].pop(0))
            if not self.backlog.get(doc_id):
                self.backlog.pop(doc_id, None)

    def shutdown(self):
        self.stopping.set()
        for s in self.sessions.values():
            s.stop("agent shutting down")
            self.close_document(s)
        self.sessions.clear()

    def _discover(self):
        try:
            docs = self.api.list_documents()
        except Exception as e:
            self.log("warning", f"cannot list documents: {e}")
            return
        self.log("debug", "documents I'm a member of: " + (
            ", ".join(f"{d['name']} ({d['role']}, {d['id'][:8]})" for d in docs) or "none - invite me to one"))
        self.discovered.put(docs)

    def _sync_documents(self, docs):
        wanted = {d["id"]: d for d in docs if not self.cfg.documents or d["id"] in self.cfg.documents}
        skipped = [d["name"] for d in docs if d["id"] not in wanted]
        if skipped:
            self.log("debug", f"not joining (COLLAB_AGENT_DOCUMENTS filter): {', '.join(skipped)}")
        for doc_id, d in wanted.items():
            if doc_id in self.sessions:
                continue
            self.log("debug", f"opening {d['name']}: downloading snapshot")
            try:
                s = self.make_session(d)
            except Exception as e:
                self.log("error", f"cannot open {d['name']}: {e}")
                self.log("debug", traceback.format_exc())
                continue
            s.on_chat = self._on_chat
            s.selection = lambda doc, _id=doc_id: list(self.touched.get(_id, []))
            self.sessions[doc_id] = s
            s.start()
            self.log("info", f"joined {d['name']} as {d['role']}")
        for doc_id in [i for i in self.sessions if i not in wanted]:
            s = self.sessions.pop(doc_id)
            s.stop("no longer a member")
            self.close_document(s)

    # -------------------------------------------------------------- main-thread calls

    def call_main(self, fn, *args, timeout=300):
        """Run fn on the main thread and return its result (called from workers)."""
        box = queue.Queue(maxsize=1)
        self.main_calls.put((fn, args, box))
        try:
            ok, value = box.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("FreeCAD did not answer in time") from None
        if not ok:
            raise value
        return value

    def _run_main_calls(self):
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            try:
                fn, args, box = self.main_calls.get_nowait()
            except queue.Empty:
                return
            try:
                box.put((True, fn(*args)))
            except Exception as e:
                box.put((False, e))

    # -------------------------------------------------------------- chat

    def _addressed(self, msg):
        if msg.get("user_kind") == "agent":
            return False  # never answer agents: avoids bot-to-bot loops
        if self.cfg.respond == "all":
            return True
        return bool(self.mention.search(msg.get("text", "")))

    def _on_chat(self, session, msg):
        who, text = msg.get("user_name"), _short(msg.get("text", ""), 120)
        if msg.get("session_id") == session.session_id:
            return
        if not self._addressed(msg):
            reason = "author is an AI agent" if msg.get("user_kind") == "agent" else \
                f"no mention of '{self.name}' (COLLAB_AGENT_RESPOND={self.cfg.respond})"
            self.log("debug", f"[{session.name}] ignoring chat from {who}: {reason}: {text}")
            return
        queued = len(self.backlog.get(session.document_id, []))
        self.log("debug", f"[{session.name}] request from {who} queued "
                          f"(busy={session.document_id in self.busy}, ahead={queued}): {text}")
        self.backlog.setdefault(session.document_id, []).append(msg)

    def _within_budget(self):
        now = time.time()
        self.turn_times = [t for t in self.turn_times if now - t < 3600]
        if len(self.turn_times) >= self.cfg.max_turns_per_hour:
            return False
        self.turn_times.append(now)
        return True

    def _start_turn(self, doc_id, msg):
        session = self.sessions.get(doc_id)
        if session is None or not session.live:
            if session is None:
                self.log("debug", "dropping request: document no longer open")
                return
            self.backlog.setdefault(doc_id, []).insert(0, msg)
            return
        if not self._within_budget():
            self.log("warning", f"hourly limit of {self.cfg.max_turns_per_hour} requests reached")
            session.send_chat(f"I've reached my limit of {self.cfg.max_turns_per_hour} requests per hour. "
                              "Please try again later.")
            return
        t = threading.Thread(target=self._turn, args=(doc_id, msg), daemon=True, name="agent-turn")
        self.busy[doc_id] = t
        t.start()

    # -------------------------------------------------------------- one request

    def _turn(self, doc_id, msg):
        try:
            reply = self._converse(doc_id, msg)
        except LLMError as e:
            reply = f"I couldn't reach my model: {e}"
            self.log("error", reply)
        except Exception as e:
            self.log("error", "turn failed:\n" + traceback.format_exc())
            reply = f"Something went wrong on my side ({type(e).__name__}). Nothing else was changed."
        try:
            self.call_main(self._post_reply, doc_id, reply)
        except Exception as e:
            self.log("error", f"could not post reply: {e}")
        finally:
            self.busy.pop(doc_id, None)

    def _post_reply(self, doc_id, text):
        s = self.sessions.get(doc_id)
        self.log("debug", f"reply ({len(text or '')} chars): {_short(text or '', 200)}")
        if s is not None and text:
            if not s.send_chat(text):
                self.log("warning", "reply not sent: not connected")
        self.touched.pop(doc_id, None)
        if s is not None:
            s.on_selection_changed()

    def _snapshot_context(self, doc_id, msg):
        s = self.sessions[doc_id]
        can_edit = s.role in ("editor", "owner")
        overview = tools.document_overview(s.doc, sheet_name=self.cfg.datasheet or "Datasheet")
        history = [m for m in s.chat if m["id"] != msg["id"]][-15:]
        rules = (RULE_DATASHEET.format(sheet=self.cfg.datasheet) if self.cfg.datasheet else "") + \
            (RULE_NAMES if self.cfg.enforce_names else "")
        system = SYSTEM_PROMPT.format(
            name=self.name,
            rules=rules,
            extra=(self.cfg.instructions.strip() + "\n") if self.cfg.instructions.strip() else "",
            document=s.name,
            access="can edit" if can_edit else "view only (answer questions, don't try to edit)",
            people=_people_text(s),
        )
        convo = "\n".join(f"{m['user_name']}{' (AI)' if m.get('user_kind') == 'agent' else ''}: {m['text']}"
                          for m in history)
        user_text = (
            f"Current model:\n{json.dumps(overview, ensure_ascii=False, default=str)[:tools.MAX_RESULT_CHARS]}\n\n"
            + (f"Earlier chat:\n{convo}\n\n" if convo else "")
            + f"{msg['user_name']} says:\n{msg['text']}"
        )
        return system, user_text, can_edit

    def _policy_issues(self, doc_id):
        s = self.sessions.get(doc_id)
        names = [n for n in self.touched.get(doc_id, []) if s is not None and s.doc.getObject(n) is not None]
        if s is None or not names:
            return []
        ctx = tools.ToolContext(s.doc, True, False, [], self.cfg.policy())
        return tools.audit(ctx, names)

    def _run_tool(self, doc_id, can_edit, name, args):
        s = self.sessions.get(doc_id)
        if s is None:
            return "the document is no longer open", True
        ctx = tools.ToolContext(s.doc, can_edit, self.cfg.allow_python, self.touched.setdefault(doc_id, []),
                                self.cfg.policy())
        started = time.monotonic()
        self.log("debug", f"tool {name} {_short(args)}")
        content, is_error = tools.call_tool(ctx, name, args)
        self.log("debug", f"tool {name} -> {'ERROR ' if is_error else ''}{time.monotonic() - started:.2f}s "
                          f"{_short(content)}")
        if name in tools.MUTATING:
            s.on_selection_changed()  # show others what the agent is working on
        return content, is_error

    def _converse(self, doc_id, msg):
        system, user_text, can_edit = self.call_main(self._snapshot_context, doc_id, msg)
        specs = tools.tool_specs(self.cfg.allow_python, can_edit)
        turns = [{"role": "user", "text": user_text}]
        self.log("info", f"working on request from {msg['user_name']}: {msg['text'][:80]}")
        checks = 0
        self.log("debug", f"{len(specs)} tools offered; context {len(system) + len(user_text)} chars")
        for step in range(self.cfg.max_steps):
            started = time.monotonic()
            self.log("debug", f"model call {step + 1}/{self.cfg.max_steps} ({self.cfg.provider}/"
                              f"{self.cfg.model or 'default'}, {len(turns)} turns)")
            reply = self.provider.complete(system, turns, specs)
            self.log("debug", f"model answered in {time.monotonic() - started:.1f}s: stop={reply.stop_reason}, "
                              f"tools={[c['name'] for c in reply.tool_calls]}, text={_short(reply.text, 150)}")
            turns.append({"role": "assistant", "text": reply.text, "tool_calls": reply.tool_calls})
            if not reply.tool_calls:
                issues = self.call_main(self._policy_issues, doc_id) if can_edit else []
                self.log("debug", f"policy check: {len(issues)} issue(s) {_short(issues, 300) if issues else ''}")
                if not issues:
                    return reply.text or "Done."
                if checks >= self.cfg.policy_retries:
                    listed = "; ".join(f"{i['object']} {i.get('property', '')} ({i['issue']})".replace("  ", " ")
                                       for i in issues[:5])
                    return (reply.text or "Done.") + f"\n\nStill open: {listed}."
                checks += 1
                turns.append({"role": "user", "text": (
                    "Policy check failed for objects you changed. Fix these before replying "
                    "(bind_to_parameter / rename_object):\n" + json.dumps(issues[:40], ensure_ascii=False))})
                continue
            results = []
            for call in reply.tool_calls:
                content, is_error = self.call_main(self._run_tool, doc_id, can_edit, call["name"], call["args"])
                results.append({"id": call["id"], "content": content, "is_error": is_error})
            turns.append({"role": "tool", "results": results})
        return ("I stopped after the maximum number of steps. Here's where things stand: "
                + (turns[-2].get("text") or "the changes so far are in the model."))
