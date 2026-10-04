"""Entry point, run inside FreeCAD's console interpreter:

    freecadcmd run_agent.py        (use the collab-agent launcher instead)

Configuration comes from the environment or COLLAB_AGENT_ENV_FILE.
"""
import os
import sys


def _home():
    env = os.environ.get("COLLAB_AGENT_HOME")
    if env:
        return env
    f = globals().get("__file__")
    return os.path.dirname(os.path.abspath(f)) if f else os.getcwd()


def main():
    home = _home()
    sys.path.insert(0, home)
    for cand in (os.path.join(home, "..", "plugin", "CollabSync"), os.path.join(home, "CollabSync"),
                 os.path.join(home, "..", "CollabSync")):
        if os.path.isdir(os.path.join(cand, "collab_sync")):
            sys.path.insert(0, os.path.abspath(cand))
            break
    else:
        print("error: the CollabSync plugin folder was not found next to the agent", file=sys.stderr)
        return 2

    import FreeCAD as App
    from collab_sync import freecad_glue as glue
    from collab_sync.api import Api
    from collab_sync.session import Session

    from collab_agent.agent import Agent, Config, make_logger

    cfg = Config()
    problems = cfg.validate()
    if problems:
        for p in problems:
            print(f"error: {p}", file=sys.stderr)
        return 2

    log = make_logger(cfg.debug)
    log("debug", f"debug logging on; server {cfg.server_url}, provider {cfg.provider}, "
                 f"model {cfg.model or 'default'}, respond={cfg.respond}, datasheet={cfg.datasheet}, "
                 f"names={'enforced' if cfg.enforce_names else 'free'}, python={'on' if cfg.allow_python else 'off'}")
    api = Api(cfg.server_url, cfg.token)
    try:
        me = api.me()
    except Exception as e:
        print(f"error: cannot sign in to {cfg.server_url}: {e}", file=sys.stderr)
        return 1
    log("info", f"signed in as {me['name']}")
    if me.get("kind") != "agent":
        log("warning", f"user {me['name']} is not marked as an AI agent; people won't see it labelled as AI")

    agent_ref = {}

    class Observer:
        def _each(self, method, *args):
            a = agent_ref.get("agent")
            if a:
                for s in list(a.sessions.values()):
                    getattr(s, method)(*args)

        def slotCreatedObject(self, obj):
            self._each("on_created", obj)

        def slotDeletedObject(self, obj):
            self._each("on_deleted", obj)

        def slotChangedObject(self, obj, prop):
            self._each("on_changed", obj, prop)

        def slotRemoveDynamicProperty(self, obj, prop):
            self._each("on_property_removed", obj, prop)

    App.addDocumentObserver(Observer())
    opener = glue.make_opener(api)
    client = f"collab-agent 0.1 ({cfg.provider}/{cfg.model or 'default'})"

    def make_session(d):
        doc, seq = opener(d["id"], d["name"])
        log("debug", f"opened {d['name']} locally at seq {seq} ({len(doc.Objects)} objects)")
        return Session(api, d["id"], d["name"], doc, seq, opener=opener, saver=glue.save_copy,
                       log=lambda level, msg, _n=d["name"]: log(level, f"[{_n}] {msg}"), client_name=client)

    def close_document(s):
        try:
            App.closeDocument(s.doc.Name)
        except Exception:
            pass

    agent = Agent(cfg, api, me["name"], make_session, log, close_document)
    agent_ref["agent"] = agent
    agent.run()
    return 0


sys.exit(main())
