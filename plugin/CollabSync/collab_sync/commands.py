"""FreeCAD GUI commands. Imported once at startup by InitGui.py."""

import FreeCAD as App
import FreeCADGui as Gui
from PySide import QtCore, QtGui

from . import controller, default_server_url
from . import freecad_glue as glue
from .api import ApiError

ROLES = ["editor", "viewer", "owner"]


def _mw():
    return Gui.getMainWindow()


def _error(title, err):
    glue.log("error", f"{title}: {err}")
    QtGui.QMessageBox.critical(_mw(), title, str(err))


def _busy(fn):
    QtGui.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
    try:
        return fn()
    finally:
        QtGui.QApplication.restoreOverrideCursor()


class _Cmd:
    menu = ""
    tip = ""

    def GetResources(self):
        return {"MenuText": self.menu, "ToolTip": self.tip}


class Settings(_Cmd):
    menu = "Settings…"
    tip = "Server URL and API token"

    def IsActive(self):
        return True

    def Activated(self):
        p = glue.params()
        url, ok = QtGui.QInputDialog.getText(
            _mw(), "Collab server", "Server URL:", QtGui.QLineEdit.Normal,
            p.GetString("ServerUrl", "") or default_server_url())
        if not ok:
            return
        token, ok = QtGui.QInputDialog.getText(
            _mw(), "Collab server", "API token (from `just user <name>`):", QtGui.QLineEdit.Password,
            p.GetString("Token", ""))
        if not ok:
            return
        p.SetString("ServerUrl", url.strip())
        p.SetString("Token", token.strip())
        try:
            me = _busy(lambda: controller.api_from_settings().me())
            QtGui.QMessageBox.information(_mw(), "Collab", f"Signed in as {me['name']}.")
        except Exception as e:
            _error("Could not sign in", e)


class Share(_Cmd):
    menu = "Share active document"
    tip = "Upload the active document and start collaborating on it"

    def IsActive(self):
        return App.ActiveDocument is not None and controller.session() is None

    def Activated(self):
        doc = App.ActiveDocument
        try:
            api = controller.api_from_settings()

            def work():
                remote = api.create_document(doc.Label or doc.Name)
                api.put_snapshot(remote["id"], 0, glue.save_copy(doc))
                return remote

            remote = _busy(work)
        except Exception as e:
            _error("Share failed", e)
            return
        controller.start(api, remote["id"], remote["name"], doc, 0)
        glue.log("info", f"shared '{remote['name']}' (id {remote['id']}). Add people with Collab → Invite.")


class Join(_Cmd):
    menu = "Open shared document…"
    tip = "Open a document shared with you and start collaborating"

    def IsActive(self):
        return controller.session() is None

    def Activated(self):
        try:
            api = controller.api_from_settings()
            docs = _busy(api.list_documents)
        except Exception as e:
            _error("Could not list documents", e)
            return
        if not docs:
            QtGui.QMessageBox.information(_mw(), "Collab", "No documents are shared with you yet.")
            return
        labels = [f"{d['name']}  ({d['role']}, seq {d['head_seq']})" for d in docs]
        choice, ok = QtGui.QInputDialog.getItem(_mw(), "Open shared document", "Document:", labels, 0, False)
        if not ok:
            return
        remote = docs[labels.index(choice)]
        try:
            doc, seq = _busy(lambda: glue.make_opener(api)(remote["id"], remote["name"]))
        except Exception as e:
            _error("Could not open document", e)
            return
        controller.start(api, remote["id"], remote["name"], doc, seq)


class Invite(_Cmd):
    menu = "Invite…"
    tip = "Give another user access to the shared document (owners only)"

    def IsActive(self):
        return controller.session() is not None

    def Activated(self):
        s = controller.session()
        user, ok = QtGui.QInputDialog.getText(_mw(), "Invite", "User name:")
        if not ok or not user.strip():
            return
        role, ok = QtGui.QInputDialog.getItem(_mw(), "Invite", "Role:", ROLES, 0, False)
        if not ok:
            return
        try:
            _busy(lambda: s.api.set_member(s.document_id, user.strip(), role))
            glue.log("info", f"{user.strip()} is now {role}")
        except ApiError as e:
            _error("Invite failed", e.message)
        except Exception as e:
            _error("Invite failed", e)


def _selected_names():
    s = controller.session()
    return [o.Name for o in Gui.Selection.getSelection(s.doc.Name)] if s else []


class Lock(_Cmd):
    menu = "Lock selection"
    tip = "Reserve the selected objects so nobody else can edit them"

    def IsActive(self):
        s = controller.session()
        return bool(s and s.live)

    def Activated(self):
        controller.session().lock(_selected_names())


class Unlock(_Cmd):
    menu = "Unlock selection"
    tip = "Release your locks on the selected objects"

    def IsActive(self):
        s = controller.session()
        return bool(s and s.live)

    def Activated(self):
        controller.session().unlock(_selected_names())


class Snapshot(_Cmd):
    menu = "Upload snapshot now"
    tip = "Store the current state on the server (speeds up joining)"

    def IsActive(self):
        s = controller.session()
        return bool(s and s.idle)

    def Activated(self):
        if not controller.session().upload_snapshot():
            glue.log("warning", "snapshot skipped: changes are still in flight, try again in a moment")


def _target_doc():
    s = controller.session()
    return s.doc if s is not None else App.ActiveDocument


class ImportParameters(_Cmd):
    menu = "Import parameters from CSV…"
    tip = "Load name,value[,unit][,description] rows into the Datasheet spreadsheet"

    def IsActive(self):
        return _target_doc() is not None

    def Activated(self):
        from . import datasheet

        path, _ = QtGui.QFileDialog.getOpenFileName(_mw(), "Import parameters", "", "CSV files (*.csv *.txt)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                text = f.read()
            doc = _target_doc()
            written, errors = datasheet.import_csv(doc, text)
            doc.recompute()
        except Exception as e:
            _error("Import failed", e)
            return
        msg = f"Imported {len(written)} parameter(s) into {datasheet.DEFAULT_NAME}."
        if errors:
            msg += "\n\nSkipped:\n" + "\n".join(f"{k}: {v}" for k, v in list(errors.items())[:20])
        glue.log("info", msg.replace("\n", " "))
        QtGui.QMessageBox.information(_mw(), "Collab", msg)


class ExportParameters(_Cmd):
    menu = "Export parameters to CSV…"
    tip = "Save the Datasheet spreadsheet as CSV"

    def IsActive(self):
        from . import datasheet

        doc = _target_doc()
        return doc is not None and datasheet.get_sheet(doc) is not None

    def Activated(self):
        from . import datasheet

        path, _ = QtGui.QFileDialog.getSaveFileName(_mw(), "Export parameters", "parameters.csv", "CSV files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(datasheet.export_csv(_target_doc()))
        except Exception as e:
            _error("Export failed", e)
            return
        glue.log("info", f"parameters exported to {path}")


class Disconnect(_Cmd):
    menu = "Disconnect"
    tip = "Stop collaborating; the local document stays open"

    def IsActive(self):
        return controller.session() is not None

    def Activated(self):
        controller.stop("disconnected by you")


COMMANDS = {
    "Collab_Settings": Settings(),
    "Collab_Share": Share(),
    "Collab_Join": Join(),
    "Collab_Invite": Invite(),
    "Collab_Lock": Lock(),
    "Collab_Unlock": Unlock(),
    "Collab_Snapshot": Snapshot(),
    "Collab_ImportParameters": ImportParameters(),
    "Collab_ExportParameters": ExportParameters(),
    "Collab_Disconnect": Disconnect(),
}
TOOLBAR = ["Collab_Share", "Collab_Join", "Collab_Lock", "Collab_Unlock", "Collab_Disconnect"]
MENU = ["Collab_Settings", "Collab_Share", "Collab_Join", "Collab_Invite", "Separator",
        "Collab_Lock", "Collab_Unlock", "Collab_Snapshot", "Separator",
        "Collab_ImportParameters", "Collab_ExportParameters", "Separator", "Collab_Disconnect"]

for _name, _cmd in COMMANDS.items():
    Gui.addCommand(_name, _cmd)
