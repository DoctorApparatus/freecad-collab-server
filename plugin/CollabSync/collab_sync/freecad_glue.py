"""FreeCAD-specific adapters used by the controller."""

import os
import tempfile
import time
import uuid

import FreeCAD as App

PARAM_PATH = "User parameter:BaseApp/Preferences/Mod/CollabSync"


def params():
    return App.ParamGet(PARAM_PATH)


def log(level, msg):
    text = f"[Collab] {msg}\n"
    if level == "error":
        App.Console.PrintError(text)
    elif level == "warning":
        App.Console.PrintWarning(text)
    elif level == "info":
        App.Console.PrintMessage(text)
    else:
        App.Console.PrintLog(text)


def cache_dir():
    getter = getattr(App, "getUserCachePath", None)
    base = getter() if getter else tempfile.gettempdir()
    path = os.path.join(base, "CollabSync")
    os.makedirs(path, exist_ok=True)
    return path


def make_opener(api):
    def opener(document_id, name):
        snap = api.get_snapshot(document_id)
        if snap is None:
            doc = App.newDocument()
            doc.Label = name
            return doc, 0
        seq, data = snap
        path = os.path.join(cache_dir(), f"{document_id}-{seq}-{int(time.time() * 1000)}.FCStd")
        with open(path, "wb") as f:
            f.write(data)
        doc = App.openDocument(path)
        doc.Label = name
        return doc, seq

    return opener


def save_copy(doc):
    path = os.path.join(cache_dir(), f"upload-{uuid.uuid4().hex}.FCStd")
    doc.saveCopy(path)
    try:
        with open(path, "rb") as f:
            return f.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


class DocumentObserver:
    """Forwards App document events to the active session."""

    def __init__(self, get_session, on_document_closed):
        self._session = get_session
        self._closed = on_document_closed

    def slotCreatedObject(self, obj):
        s = self._session()
        if s:
            s.on_created(obj)

    def slotDeletedObject(self, obj):
        s = self._session()
        if s:
            s.on_deleted(obj)

    def slotChangedObject(self, obj, prop):
        s = self._session()
        if s:
            s.on_changed(obj, prop)

    def slotRemoveDynamicProperty(self, obj, prop):
        s = self._session()
        if s:
            s.on_property_removed(obj, prop)

    def slotDeletedDocument(self, doc):
        s = self._session()
        if s and s.doc is not None and doc.Name == s.doc.Name:
            self._closed()


class SelectionObserver:
    def __init__(self, get_session):
        self._session = get_session

    def _dirty(self, *args):
        s = self._session()
        if s:
            s.on_selection_changed()

    addSelection = removeSelection = setSelection = clearSelection = _dirty
