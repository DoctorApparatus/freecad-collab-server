"""Owns the single active session, its timer, observers and the side panel."""

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore, QtGui

from . import freecad_glue as glue
from . import default_server_url
from .api import Api
from .session import Session

TICK_MS = 50

_session = None
_timer = None
_panel = None
_observers_installed = False


def session():
    return _session


def api_from_settings():
    p = glue.params()
    url = p.GetString("ServerUrl", "").strip() or default_server_url()
    token = p.GetString("Token", "").strip()
    if not token:
        raise RuntimeError("No API token set. Run Collab → Settings first.")
    return Api(url, token)


def _install_observers():
    global _observers_installed
    if _observers_installed:
        return
    App.addDocumentObserver(glue.DocumentObserver(session, _on_document_closed))
    Gui.Selection.addObserver(glue.SelectionObserver(session))
    _observers_installed = True


def _on_document_closed():
    stop("the shared document was closed")


def _selection(doc):
    return [o.Name for o in Gui.Selection.getSelection(doc.Name)]


def _tick():
    s = _session
    if s is None:
        return
    try:
        s.tick()
    except Exception as e:
        glue.log("error", f"sync error: {e}")


def start(api, document_id, name, doc, seq):
    global _session, _timer
    stop()
    _install_observers()
    _session = Session(
        api, document_id, name, doc, seq,
        opener=glue.make_opener(api),
        saver=glue.save_copy,
        log=glue.log,
        selection=_selection,
        on_update=lambda s: panel().refresh(s),
    )
    if _timer is None:
        _timer = QtCore.QTimer()
        _timer.timeout.connect(_tick)
    _timer.start(TICK_MS)
    panel().show()
    _session.start()


def stop(reason=None):
    global _session
    if _session is not None:
        _session.stop(reason)
        panel().refresh(_session)
        _session = None
    if _timer is not None:
        _timer.stop()


class Panel(QtGui.QDockWidget):
    def __init__(self):
        super().__init__("Collab")
        self.setObjectName("CollabSyncPanel")
        body = QtGui.QWidget()
        layout = QtGui.QVBoxLayout(body)
        self.status = QtGui.QLabel("Not connected")
        self.status.setWordWrap(True)
        self.people = QtGui.QListWidget()
        self.people.setMaximumHeight(160)
        self.log = QtGui.QTextBrowser()
        self.log.setOpenExternalLinks(False)
        self.input = QtGui.QLineEdit()
        self.input.setPlaceholderText("Message everyone. Mention an AI by name, e.g. @designbot")
        self.input.returnPressed.connect(self._send)
        layout.addWidget(self.status)
        layout.addWidget(QtGui.QLabel("Participants"))
        layout.addWidget(self.people)
        layout.addWidget(QtGui.QLabel("Chat"))
        layout.addWidget(self.log, 1)
        layout.addWidget(self.input)
        self.setWidget(body)
        self._chat_shown = None

    def _send(self):
        s = session()
        if s and s.send_chat(self.input.text()):
            self.input.clear()

    def refresh(self, s):
        if s is None:
            self.status.setText("Not connected")
            self.people.clear()
            self.input.setEnabled(False)
            return
        self.input.setEnabled(bool(s.live))
        self._render_chat(s)
        self.status.setText(f"<b>{s.name}</b><br>{s.status}")
        self.people.clear()
        for sid, p in sorted(s.participants.items(), key=lambda kv: kv[1]["user_name"]):
            me = " (you)" if sid == s.session_id else ""
            ai = " · AI" if p.get("kind") == "agent" else ""
            text = f"● {p['user_name']}{me}{ai} — {p['role']}"
            sel = s.selections.get(sid)
            if sel:
                text += f"\n    selecting: {', '.join(sel[:5])}{'…' if len(sel) > 5 else ''}"
            held = sorted(o for o, h in s.locks.items() if h == sid)
            if held:
                text += f"\n    locked: {', '.join(held[:5])}{'…' if len(held) > 5 else ''}"
            item = QtGui.QListWidgetItem(text)
            item.setForeground(QtGui.QBrush(QtGui.QColor(p.get("color", "#000000"))))
            self.people.addItem(item)


    def _render_chat(self, s):
        key = (id(s), len(s.chat), s.chat[-1]["id"] if s.chat else None)
        if key == self._chat_shown:
            return
        self._chat_shown = key
        import html
        import time as _t

        parts = []
        for m in s.chat:
            when = _t.strftime("%H:%M", _t.localtime(m["created_at"] / 1000))
            who = html.escape(m["user_name"]) + (" (AI)" if m.get("user_kind") == "agent" else "")
            body = html.escape(m["text"]).replace("\n", "<br>")
            parts.append(f"<p><span style='color:gray'>{when}</span> <b>{who}</b><br>{body}</p>")
        self.log.setHtml("".join(parts))
        bar = self.log.verticalScrollBar()
        bar.setValue(bar.maximum())


def panel():
    global _panel
    if _panel is None:
        _panel = Panel()
        Gui.getMainWindow().addDockWidget(QtCore.Qt.RightDockWidgetArea, _panel)
    return _panel
