# freecad-collab-server

Real-time collaboration server for FreeCAD. The companion FreeCAD plugin connects per document over WebSocket, streams object-level edits, and receives everyone else's edits in one global order. Full `.FCStd` snapshots are stored over REST so new joiners don't replay all history.

Stack: axum 0.8 · tokio · sqlx (SQLite, WAL) · serde · thiserror/anyhow · tracing · schemars · strum · typed-builder · async-trait · dotenv.

## Install

Download the bundle for your system from the releases page (or build it with `just dist linux` / `just dist windows`), unpack it, then:

| | Linux | Windows |
|---|---|---|
| Menu | `./install.sh` | double-click `install.cmd` |
| Server (one machine) | `sudo ./install.sh server --open-firewall` | `install.ps1 -Action Server -OpenFirewall` |
| FreeCAD plugin (everyone) | `./install.sh plugin --server-url http://<server-ip>:14008` | `install.ps1 -Action Plugin -ServerUrl http://<server-ip>:14008` |
| Uninstall | `sudo ./install.sh uninstall-server [--purge]`, `./install.sh uninstall-plugin` | `install.ps1 -Action UninstallServer [-Purge]`, `-Action UninstallPlugin` |

The server installer prints the admin console address and admin token when it finishes.

**Linux server:** binary in `/usr/local/bin`, config in `/etc/freecad-collab/collab.env`, data in `/var/lib/freecad-collab`, hardened systemd unit `freecad-collab.service` running as the `freecad-collab` system user. Logs: `journalctl -u freecad-collab -f`. Re-running the installer upgrades in place and keeps config and data.

**Windows server:** binary in `C:\Program Files\FreeCADCollab`, config and data in `C:\ProgramData\FreeCADCollab` (readable only by administrators and the service), log in `server.log` there. Runs at startup as a scheduled task under `LOCAL SERVICE` and restarts on failure. The firewall rule (with `-OpenFirewall`) only covers private and domain networks.

**Plugin:** copied into every FreeCAD `Mod` folder found for the user (native and Flatpak on Linux, `%APPDATA%\FreeCAD` on Windows, including per-version folders of FreeCAD 1.1+). `--server-url` pre-fills the address in Collab → Settings.

## AI participants

An AI agent joins documents like a person: it appears in the participant list (marked AI), reads the chat, and edits the model through real FreeCAD operations that sync to everyone.

1. Admin console → Users → add a user with kind **AI agent**, copy its token
2. Install the agent on a machine that has FreeCAD (it runs `freecadcmd` headless):
   - Linux: `sudo ./install.sh agent --token <token> --api-key <key>`
   - Windows: `install.ps1 -Action Agent -AgentToken <token> -ApiKey <key>` (or menu option 6)
   - From source: `cp agent/agent.env.example agent/agent.env`, fill it in, `just agent`
3. Invite the agent to a document (admin console, or Collab → Invite in FreeCAD)
4. In FreeCAD's Collab chat, write e.g. `@designbot add a 40 × 20 × 5 mm base plate with 3.4 mm holes in the corners`

The agent answers when mentioned by name (or every message with `COLLAB_AGENT_RESPOND=all`) and never answers other agents, so two agents can't loop.

**Models:** `COLLAB_AGENT_PROVIDER=anthropic` uses Claude via the Anthropic API (default model `claude-sonnet-5`). `openai` works with OpenAI and any OpenAI-compatible server: Ollama, LM Studio, vLLM, llama.cpp. For fully local use, point `COLLAB_AGENT_BASE_URL` at e.g. `http://localhost:11434/v1` and pick a model with tool calling.

**What it can do:** inspect the document (objects, dimensions, bounding boxes, volumes, errors), create objects and PartDesign bodies, sketches (lines, circles, arcs, rectangles, polygons) on planes or faces, pads/pockets/extrusions, booleans, fillets/chamfers, set properties and expressions, delete what it's asked to. It sees who has what selected, so "make this one longer" works. After each change it checks for recompute errors and fixes them.

**Parametric by rule:** every dimension the agent writes comes from a spreadsheet named `Datasheet` in the document (columns Parameter | Value | Description; each value cell aliased by name, e.g. `plate_length`). The agent first defines parameters, then binds sizes, positions, sketch constraints, pad lengths and fillet radii to them as FreeCAD expressions (`Datasheet.plate_length`). Plain numbers are rejected by its tools; derived values are formulas in the sheet (`=plate_length - 2 * wall`). Changing a value in the datasheet, by a person or the agent, updates the model for everyone.

**Named by rule:** everything the agent creates gets a descriptive name (`BasePlate`, `BasePlate_Sketch`, `MountingHole_Pocket`) and a readable label. Generic names like `Box001` or `Pad` are rejected.

**Checked before every reply:** the agent audits all objects it touched; unbound dimensions or generic labels send it back to fix them (up to `COLLAB_AGENT_POLICY_RETRIES` times). Ask `@designbot audit the model` to bring existing work by people into line: it moves their literal values into the datasheet (`bind_to_parameter`) and relabels generic names.

**CSV:** paste CSV in the chat (`@designbot import these parameters: …`) or use Collab → Import/Export parameters in FreeCAD. Columns `name,value[,unit][,description]`, comma or semicolon separated, German headers (`Name;Wert;Einheit;Beschreibung`) and decimal commas work.

Settings: `COLLAB_AGENT_DATASHEET` (sheet name, `off` to disable), `COLLAB_AGENT_ENFORCE_NAMES`, `COLLAB_AGENT_NAME_PATTERN`.

**Safety:** the agent has the role you give it (a viewer agent only answers questions). Running arbitrary Python (`COLLAB_AGENT_ALLOW_PYTHON=1`) is off by default because anyone who can chat could then run code on the agent's machine; enable it only when the agent runs isolated and every member is trusted. Limits: `COLLAB_AGENT_MAX_STEPS` per request, `COLLAB_AGENT_MAX_TURNS_PER_HOUR` overall. The installers run it as its own unprivileged account.

## Admin console

Open `http://<server>:14008/admin` and sign in with `COLLAB_ADMIN_TOKEN`. From there you can:

- see who is working on which document right now
- add users and hand out their token, issue extra tokens, revoke tokens
- disable users (disconnects them immediately)
- create documents, add or remove members, change roles, delete documents

The console is built into the binary, needs no internet access, and is served with a strict Content-Security-Policy. The token is kept in the browser tab's session storage only. Put the server behind HTTPS (e.g. Caddy or nginx) before exposing it beyond your LAN.

## Use it with FreeCAD

1. Install the server (above), or run it from source with `just run`
2. In the admin console, add users `alice` and `bob` — note both tokens
3. Install the plugin (above), then restart FreeCAD
4. In FreeCAD: switch to the **Collab** workbench → **Collab → Settings…**, enter `http://<server-ip>:14008` and your token
5. Alice: open a model → **Share active document** → **Invite…** `bob` as `editor`
6. Bob: **Open shared document…** → pick it

Plugin details: [`plugin/CollabSync/README.md`](plugin/CollabSync/README.md).

## Quick start

```bash
cp .env.example .env
just user alice          # prints an API token
just user bob
just run                 # listens on :14008
```

Docker: `just docker-build && just docker-run` (data in the `collab-data` volume).

## Model

| Concept | Meaning |
|---|---|
| Document | A shared FreeCAD model. Has members with roles `owner` > `editor` > `viewer`. |
| Op | One object-level mutation (`add_object`, `set_property`, …). Objects are addressed by FreeCAD internal `Name`. |
| Batch / seq | Ops submitted together are committed atomically and get the next `seq` (1, 2, 3 …). Apply batches in `seq` order. |
| Snapshot | An `.FCStd` file representing the document at some `seq`. |
| Lock | Optional exclusive edit claim on objects. Released on unlock or disconnect. |

### Conflict rule

The server tracks, per `(object, property)`, the last `seq` and session that wrote it. A submit with `base_seq = N` is rejected with `conflict` if another session wrote any touched `(object, property)` — or created/removed the object — after `N`. Different properties of the same object never conflict. A rejected client applies the pending remote ops it has received, re-derives its change, and resubmits with a **new** `client_op_id`.

### Compaction

Each snapshot upload keeps the newest `COLLAB_SNAPSHOT_RETENTION` snapshots and deletes ops already covered by the oldest kept one. Clients behind that point get `resync_required` and must reload the snapshot.

## REST API

All endpoints except health take `Authorization: Bearer <token>`. Non-members get `404` for documents (ids are not enumerable). Errors are `{"error": "<code>", "message": "..."}`.

| Method & path | Role | Purpose |
|---|---|---|
| `GET /healthz`, `GET /readyz` | – | Liveness / DB readiness |
| `GET /v1/protocol` | – | JSON Schema for all WS messages |
| `GET /v1/me` | any | Current user |
| `GET /v1/documents` | any | Documents you belong to, with your role |
| `POST /v1/documents` `{name, description?}` | any | Create; caller becomes owner |
| `GET /v1/documents/{id}` | viewer | Details, latest snapshot info, members |
| `DELETE /v1/documents/{id}` | owner | Delete; live sessions get `document_deleted` |
| `GET /v1/documents/{id}/members` | viewer | List members |
| `PUT /v1/documents/{id}/members` `{user_name, role}` | owner | Add / change role (live sessions get `role_changed`) |
| `DELETE /v1/documents/{id}/members/{user_id}` | owner or self | Remove (live sessions closed with 4003) |
| `GET /v1/documents/{id}/ops?after=N&limit=M` | viewer | Page ops; `410` if compacted |
| `GET /v1/documents/{id}/chat?limit=N` | viewer | Recent chat, oldest first |
| `GET /v1/documents/{id}/snapshot` | viewer | Latest `.FCStd`; headers `X-Snapshot-Seq`, `ETag` (`If-None-Match` → 304) |
| `PUT /v1/documents/{id}/snapshot?seq=N` (raw body) | editor | Upload `.FCStd` reflecting all ops `<= N` |
| `GET /v1/documents/{id}/ws` | viewer | WebSocket (below) |

Admin (`Authorization: Bearer $COLLAB_ADMIN_TOKEN`; disabled if unset): `GET /v1/admin/overview`, `GET/POST /v1/admin/users`, `GET/POST /v1/admin/users/{id}/tokens`, `PUT /v1/admin/users/{id}/disabled`, `DELETE /v1/admin/tokens/{id}`, `GET/POST /v1/admin/documents`, `GET/DELETE /v1/admin/documents/{id}`, `PUT /v1/admin/documents/{id}/members`, `DELETE /v1/admin/documents/{id}/members/{user_id}`.

## WebSocket protocol (v1)

JSON text frames, tagged by `type`. Full schema: `just schema` or `GET /v1/protocol`. The token may also be passed as `?access_token=` if the client can't set headers.

### Session flow

1. Connect to `/v1/documents/{id}/ws`.
2. Send `hello` within 15 s:
   `{"type":"hello","protocol_version":1,"client":"FreeCAD 1.0 / collab 0.1","last_seen_seq":42}`
   Omit `last_seen_seq` to start from the latest snapshot.
3. Receive `welcome` (session id, participants, locks, head seq, snapshot info).
4. Receive missed `ops` batches, then `catch_up_complete`.
   Or `resync_required` + close 4000 → download snapshot, reconnect with `last_seen_seq = snapshot.seq`.
5. Live phase: send `submit` / `lock` / `unlock` / `presence` / `ping`; receive the rest.

### Client → server

| type | fields |
|---|---|
| `hello` | `protocol_version`, `client`, `last_seen_seq?` |
| `submit` | `client_op_id` (unique per user+doc, ≤128 chars), `base_seq`, `ops[]` |
| `lock` / `unlock` | `objects[]` (all-or-nothing for lock) |
| `presence` | `selection[]`, `camera?` (≤4 KB JSON) |
| `ping` | `nonce` |
| `chat` | `text` (1-16000 characters, max 10 per 10 s) |

### Ops

```json
{"op":"add_object","name":"Box","type_id":"Part::Box","label":"Base","properties":{"Length":{"type_id":"App::PropertyLength","value":10.0}}}
{"op":"remove_object","name":"Box"}
{"op":"set_label","name":"Box","label":"Base plate"}
{"op":"set_property","object":"Box","property":"Length","value":{"type_id":"App::PropertyLength","value":20.0}}
{"op":"add_property","object":"Box","property":"Note","type_id":"App::PropertyString","group":"Meta","doc":"..."}
{"op":"remove_property","object":"Box","property":"Note"}
{"op":"set_expression","object":"Box","path":"Length","expression":"Spreadsheet.width * 2"}
```

`value.value` is opaque to the server; the plugin defines the encoding per property type (placements, links, shapes as BREP strings, etc.). Max size per value: `COLLAB_MAX_PROPERTY_VALUE_BYTES`.

### Server → client

`welcome` (includes the last 50 chat messages), `chat {message}`, `ops` (other sessions' batches), `catch_up_complete`, `resync_required`, `ack {client_op_id, seq}`, `reject {client_op_id, reason, message, head_seq}`, `locked`, `unlocked`, `lock_denied`, `presence`, `joined`, `left`, `role_changed`, `snapshot_available`, `error {code, message}`, `pong`.

Reject reasons: `forbidden`, `invalid_op`, `invalid_base_seq`, `conflict`, `locked_by_other`, `too_large`, `internal` (retry with the same id).

### Close codes

| Code | Meaning | Plugin action |
|---|---|---|
| 1000 | Normal | – |
| 1001 | Server shutting down | Reconnect with backoff |
| 1011 | Internal error | Reconnect with backoff |
| 4000 | Resync required | Download snapshot, reconnect |
| 4001 | Timeout (hello or heartbeat) | Reconnect |
| 4002 | Protocol error | Fix client; don't loop |
| 4003 | Access revoked | Stop |
| 4004 | Document deleted | Stop |

The server sends WebSocket pings every `COLLAB_HEARTBEAT_INTERVAL_SECS` and drops clients silent for `COLLAB_CLIENT_TIMEOUT_SECS`.

## Plugin contract (what the FreeCAD side must do)

1. Track `last_applied_seq`: set from snapshot `seq`, then from each applied `ops.batch.seq` and each own `ack.seq`.
2. Hook `App.DocumentObserver` (`slotCreatedObject`, `slotDeletedObject`, `slotChangedObject`) and turn local changes into ops. Buffer them per transaction and `submit` with `base_seq = last_applied_seq`.
3. Keep unacked submits in a queue. On reconnect, resend them unchanged (same `client_op_id`) — the server dedupes.
4. When applying remote ops, suppress the observer so they aren't echoed back, then `recompute()`.
5. On `reject: conflict`, revert or rebase the local change against the received ops, then resubmit with a new id.
6. Periodically (e.g. every 500 ops or on save) upload a snapshot: `saveAs` to a temp file, `PUT .../snapshot?seq=<last_applied_seq>` — only when no own submits are pending.

## Operations

- Single process, single node: rooms live in memory, state in SQLite. Put it behind a TLS-terminating reverse proxy (Caddy/nginx) that allows WebSocket upgrades and long-lived connections.
- Backups: `sqlite3 data/collab.db ".backup backup.db"` works while running (WAL).
- Logs: set `COLLAB_LOG_FORMAT=json` for log shipping; `RUST_LOG` controls verbosity.
- Graceful shutdown on SIGTERM/Ctrl-C: sessions get close 1001 so plugins reconnect.

## Development

```bash
just check     # fmt + clippy + tests
just test      # unit + WebSocket integration tests
just plugin-test  # plugin sync core end-to-end against a real server
just agent-test   # AI agent end-to-end with fake Anthropic/OpenAI endpoints
just smoke     # REST smoke test against a running server (needs COLLAB_ADMIN_TOKEN, curl, jq)
```
