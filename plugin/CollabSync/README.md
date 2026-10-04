# CollabSync (FreeCAD plugin)

Real-time collaboration on FreeCAD documents through `freecad-collab-server`. Pure Python, no extra packages: works with the Python bundled with FreeCAD 0.21 and 1.x.

## Install

Linux/macOS: `just install-plugin` from the repo root (symlinks this folder into FreeCAD's `Mod` directory).
Windows: copy this `CollabSync` folder into `%APPDATA%\FreeCAD\Mod\` (check with `print(App.getUserAppDataDir())`).

Restart FreeCAD. A **Collab** workbench appears.

## Use

| Command | What it does |
|---|---|
| Settings… | Server URL and your API token |
| Share active document | Uploads the open document and starts a session |
| Open shared document… | Downloads a document shared with you and starts a session |
| Invite… | Owner adds a user as editor/viewer/owner |
| Lock / Unlock selection | Reserve objects so others can't edit them |
| Upload snapshot now | Store current state (new joiners load faster) |
| Import parameters from CSV… | Load `name,value[,unit][,description]` rows into the `Datasheet` spreadsheet |
| Export parameters to CSV… | Save the `Datasheet` spreadsheet as CSV |
| Disconnect | Stop syncing; the local document stays open |

The panel also has a chat for everyone in the document. AI participants are marked "AI"; address one with `@name`, e.g. `@designbot fillet all outer edges with 1 mm`.

The session keeps running when you switch to Part Design, Sketcher, etc. The **Collab** panel on the right shows status, participants, their selections and locks. Messages go to the Report view with a `[Collab]` prefix.

## How it syncs

- Object creation/deletion, labels and every non-computed property are sent, batched every 150 ms.
- Property values use FreeCAD's own serializer (`dumpPropertyContent`), the same code path as saving an `.FCStd`, so sketches, expressions, links and Python features round-trip.
- Computed properties (`Shape` of parametric features, anything flagged Output/Transient) are not sent; each client recomputes.
- `Shape` of plain `Part::Feature` objects (imported STEP/BREP) is sent as BREP text. Large imports may need `COLLAB_MAX_PROPERTY_VALUE_BYTES` raised on the server.
- Concurrent edits of the same property: the first to reach the server wins, the other client is told and adopts it. Different properties never conflict.
- Disconnects are retried with backoff; edits made offline are sent after reconnecting.
- If local and shared state can't be reconciled, the plugin reloads the shared document into a new tab and marks the old tab `(detached)`.

## Limits

- Visibility and view settings (colors, display mode) are per user and not synced.
- Python feature *view providers* are only restored when a document is opened from a snapshot; objects created live by others may show default icons until you reopen.
- Undo/redo works locally; the undone state is synced as a normal edit.
- The API token is stored in FreeCAD's user parameters (plain text).
