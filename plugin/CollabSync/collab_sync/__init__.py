"""CollabSync: real-time collaboration for FreeCAD via freecad-collab-server."""

__version__ = "0.1.0"

import json as _json
import os as _os

DEFAULTS_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "defaults.json")


def installer_defaults():
    """Values written by the installer (e.g. server_url). Empty if none."""
    try:
        with open(DEFAULTS_FILE, "r", encoding="utf-8") as f:
            data = _json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def default_server_url():
    return installer_defaults().get("server_url") or "http://127.0.0.1:14008"
