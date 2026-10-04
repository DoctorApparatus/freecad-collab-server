"use strict";

// Collab admin console. No dependencies, no build step. All DOM is built with
// createElement/textContent, never innerHTML, so server data cannot inject markup.

const TOKEN_KEY = "collab-admin-token";
const ROLES = ["editor", "viewer", "owner"];
const VIEWS = { overview: "Overview", users: "Users", documents: "Documents" };
const REFRESH_MS = 5000;

let adminToken = sessionStorage.getItem(TOKEN_KEY);
let refreshTimer = null;

// ---------------------------------------------------------------- helpers

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "style") el.style.cssText = v; // CSSOM: allowed under the CSP, unlike style attributes
    else if (v === true) el.setAttribute(k, "");
    else el.setAttribute(k, String(v));
  }
  for (const c of children.flat(Infinity)) {
    if (c == null || c === false) continue;
    el.append(c instanceof Node ? c : String(c));
  }
  return el;
}

const $ = (id) => document.getElementById(id);

function fmtBytes(n) {
  if (n == null) return "none";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n < 10 && i ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
}

function fmtAgo(ms) {
  if (!ms) return "never";
  const s = Math.max(0, (Date.now() - ms) / 1000);
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 86400 * 14) return `${Math.floor(s / 86400)} days ago`;
  return new Date(ms).toLocaleDateString();
}

function fmtDate(ms) {
  return ms ? new Date(ms).toLocaleString() : "";
}

function fmtUptime(sec) {
  const d = Math.floor(sec / 86400), hr = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d) return `${d} d ${hr} h`;
  if (hr) return `${hr} h ${m} min`;
  return `${m} min`;
}

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many}`;
}

function aiTag(kind) {
  return kind === "agent" ? h("span", { class: "tag", title: "AI participant" }, "AI agent") : null;
}

function toast(message, bad = false) {
  const el = h("div", { class: bad ? "bad" : "" }, message);
  $("toast").append(el);
  setTimeout(() => el.remove(), bad ? 7000 : 3500);
}

// ---------------------------------------------------------------- api

class AuthError extends Error {}

async function api(method, path, body) {
  const headers = { Authorization: `Bearer ${adminToken}` };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  if (res.status === 401) throw new AuthError("That admin token was not accepted.");
  if (res.status === 404 && path.startsWith("/v1/admin/overview")) {
    throw new AuthError("The admin API is off. Set COLLAB_ADMIN_TOKEN in the server config and restart the server.");
  }
  if (!res.ok) {
    let message = res.statusText;
    try { message = (await res.json()).message || message; } catch (_) { /* not JSON */ }
    throw new Error(message.charAt(0).toUpperCase() + message.slice(1));
  }
  return res.status === 204 ? null : res.json();
}

async function run(action, success) {
  try {
    const result = await action();
    if (success) toast(success);
    return result;
  } catch (e) {
    if (e instanceof AuthError) { signOut(e.message); return undefined; }
    toast(e.message, true);
    return undefined;
  }
}

// ---------------------------------------------------------------- dialogs

function openDialog(...content) {
  const d = $("dialog");
  d.replaceChildren(...content);
  if (!d.open) d.showModal();
  return d;
}

function closeDialog() {
  const d = $("dialog");
  if (d.open) d.close();
}

function confirmAction({ title, body, confirm, danger = false }) {
  return new Promise((resolve) => {
    const d = $("confirm");
    const done = (v) => { d.close(); resolve(v); };
    d.replaceChildren(
      h("h2", { id: "confirm-title" }, title),
      h("p", {}, body),
      h("div", { class: "dialog-actions" },
        h("button", { class: "btn secondary", type: "button", onclick: () => done(false) }, "Cancel"),
        h("button", { class: `btn ${danger ? "danger solid" : ""}`, type: "button", onclick: () => done(true) }, confirm),
      ),
    );
    d.onclose = () => resolve(false);
    d.showModal();
  });
}

function copyText(input) {
  const text = input.value;
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text).then(() => toast("Copied"));
  }
  input.select();
  document.execCommand("copy");
  toast("Copied");
  return Promise.resolve();
}

function tokenReveal(userName, token, onDone, kind) {
  const input = h("input", { type: "text", readonly: true, value: token, "aria-label": `Token for ${userName}` });
  const box = h("div", { class: "reveal", role: "region", "aria-label": "New token" },
    h("strong", {}, `Token for ${userName}`),
    h("div", { class: "token-row" },
      input,
      h("button", { class: "btn", type: "button", onclick: () => copyText(input) }, "Copy"),
    ),
    h("p", { class: "hint" }, kind === "agent"
      ? `This is shown once. Put it in the agent's agent.env as COLLAB_AGENT_TOKEN, with COLLAB_SERVER_URL=${location.origin}. Then add ${userName} to documents like a person.`
      : `This is shown once. ${userName} pastes it in FreeCAD under Collab → Settings, with this server's address: ${location.origin}`),
    onDone ? h("button", { class: "link", type: "button", onclick: onDone }, "Done") : null,
  );
  setTimeout(() => input.select(), 0);
  return box;
}

// ---------------------------------------------------------------- frame

function drawZones() {
  const z = document.querySelector(".zones");
  const cols = 6, rows = 4, parts = [];
  for (let i = 0; i < cols; i++) {
    const left = `calc(var(--margin) + (100% - 2 * var(--margin)) * ${(i + 0.5) / cols} - var(--margin) / 2)`;
    parts.push(h("span", { style: `top:0;left:${left}` }, String(i + 1)));
    parts.push(h("span", { style: `bottom:0;left:${left}` }, String(i + 1)));
    if (i > 0) {
      const x = `calc(var(--margin) + (100% - 2 * var(--margin)) * ${i / cols})`;
      parts.push(h("i", { style: `top:0;left:${x};width:1px;height:var(--margin)` }));
      parts.push(h("i", { style: `bottom:0;left:${x};width:1px;height:var(--margin)` }));
    }
  }
  for (let i = 0; i < rows; i++) {
    const top = `calc(var(--margin) + (100% - 2 * var(--margin)) * ${(i + 0.5) / rows} - var(--margin) / 2)`;
    const letter = String.fromCharCode(65 + i);
    parts.push(h("span", { style: `left:0;top:${top}` }, letter));
    parts.push(h("span", { style: `right:0;top:${top}` }, letter));
    if (i > 0) {
      const y = `calc(var(--margin) + (100% - 2 * var(--margin)) * ${i / rows})`;
      parts.push(h("i", { style: `left:0;top:${y};height:1px;width:var(--margin)` }));
      parts.push(h("i", { style: `right:0;top:${y};height:1px;width:var(--margin)` }));
    }
  }
  z.replaceChildren(...parts);
}

function setTitleBlock(overview, view) {
  $("tb-host").textContent = location.host;
  if (overview) {
    $("tb-version").textContent = overview.version;
    $("tb-uptime").textContent = fmtUptime(overview.uptime_secs);
  }
  const keys = Object.keys(VIEWS);
  if (view) $("tb-sheet").textContent = `${VIEWS[view]}, ${keys.indexOf(view) + 1} of ${keys.length}`;
}

function currentView() {
  const v = location.hash.replace("#", "");
  return VIEWS[v] ? v : "overview";
}

// ---------------------------------------------------------------- login

function renderLogin(message) {
  $("nav").hidden = true;
  $("signout").hidden = true;
  $("titleblock").hidden = true;
  const input = h("input", { type: "password", id: "token", autocomplete: "off", spellcheck: "false", required: true });
  const error = h("p", { class: "error", role: "alert" }, message || "");
  const form = h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      adminToken = input.value.trim();
      try {
        const o = await api("GET", "/v1/admin/overview");
        sessionStorage.setItem(TOKEN_KEY, adminToken);
        startApp(o);
      } catch (err) {
        adminToken = null;
        error.textContent = err.message;
        input.focus();
      }
    },
  },
    h("div", { class: "field" },
      h("label", { for: "token" }, "Admin token"),
      input,
    ),
    h("p", { class: "hint" },
      "It's the COLLAB_ADMIN_TOKEN value in the server config: ",
      h("code", {}, "/etc/freecad-collab/collab.env"), " on Linux, ",
      h("code", {}, "C:\\ProgramData\\FreeCADCollab\\collab.env"), " on Windows. The installer also printed it."),
    error,
    h("div", {}, h("button", { class: "btn", type: "submit" }, "Sign in")),
  );
  $("main").replaceChildren(h("section", { class: "login" }, h("h1", {}, "Sign in to the collab server"), form));
  input.focus();
}

function signOut(message) {
  clearInterval(refreshTimer);
  refreshTimer = null;
  adminToken = null;
  sessionStorage.removeItem(TOKEN_KEY);
  closeDialog();
  renderLogin(message);
}

// ---------------------------------------------------------------- app shell

function startApp(overview) {
  $("nav").hidden = false;
  $("signout").hidden = false;
  $("titleblock").hidden = false;
  setTitleBlock(overview, currentView());
  route();
}

async function route() {
  if (!adminToken) return renderLogin();
  clearInterval(refreshTimer);
  refreshTimer = null;
  const view = currentView();
  for (const a of document.querySelectorAll("#nav a")) {
    if (a.getAttribute("href") === `#${view}`) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  setTitleBlock(null, view);
  document.title = `${VIEWS[view]} · Collab server admin`;
  const main = $("main");
  if (view === "overview") await renderOverview(main);
  else if (view === "users") await renderUsers(main);
  else await renderDocuments(main);
  main.focus({ preventScroll: true });
}

// ---------------------------------------------------------------- overview

async function renderOverview(main) {
  const draw = async () => {
    const o = await run(() => api("GET", "/v1/admin/overview"));
    if (!o || currentView() !== "overview") return;
    setTitleBlock(o, "overview");
    const s = o.stats;
    const live = o.live.length
      ? o.live.map((d) => h("div", { class: "live-doc" },
          h("h3", {}, d.document_name),
          h("ul", {}, d.participants.map((p) => h("li", {},
            h("span", { class: "swatch", style: `background:${/^#[0-9a-f]{6}$/i.test(p.color) ? p.color : "#888"}` }),
            p.user_name,
            aiTag(p.kind),
            h("span", { class: "who-meta" }, `${p.role}, ${p.client}`),
          ))),
        ))
      : [h("p", { class: "empty" }, "Nobody is connected. People appear here while they have a shared document open in FreeCAD.")];
    const totals = [
      ["Users", s.users - s.disabled_users + (s.disabled_users ? ` (+${s.disabled_users} disabled)` : "")],
      ["Documents", s.documents],
      ["People online", o.live.reduce((n, d) => n + d.participants.length, 0)],
      ["Recorded changes", s.ops.toLocaleString()],
      ["Snapshot storage", fmtBytes(s.snapshot_bytes)],
    ];
    main.replaceChildren(
      h("h1", {}, "Overview"),
      h("h2", {}, "Working now"),
      ...live,
      h("h2", {}, "Totals"),
      h("dl", { class: "totals" }, totals.map(([k, v]) => h("div", {}, h("dt", {}, k), h("dd", {}, String(v))))),
    );
  };
  await draw();
  refreshTimer = setInterval(() => { if (!document.hidden) draw(); }, REFRESH_MS);
}

// ---------------------------------------------------------------- users

async function renderUsers(main) {
  const users = await run(() => api("GET", "/v1/admin/users"));
  if (!users) return;
  const revealSlot = h("div", {});
  const nameInput = h("input", {
    type: "text", id: "new-user", required: true, maxlength: 64,
    pattern: "[A-Za-z0-9_.\\-]{1,64}", autocomplete: "off",
    title: "Letters, digits, dot, dash, underscore",
  });
  const kindSelect = h("select", { id: "new-user-kind" },
    h("option", { value: "human" }, "Person"),
    h("option", { value: "agent" }, "AI agent"));
  const form = h("form", {
    class: "inline-form",
    onsubmit: async (e) => {
      e.preventDefault();
      const created = await run(() => api("POST", "/v1/admin/users", { name: nameInput.value.trim(), kind: kindSelect.value }));
      if (!created) return;
      await renderUsers(main);
      main.querySelector("#reveal-slot").replaceChildren(
        tokenReveal(created.user.name, created.token.token,
          () => main.querySelector("#reveal-slot").replaceChildren(), created.user.kind),
      );
    },
  },
    h("div", { class: "field" }, h("label", { for: "new-user" }, "New user name"), nameInput),
    h("div", { class: "field" }, h("label", { for: "new-user-kind" }, "Kind"), kindSelect),
    h("button", { class: "btn", type: "submit" }, "Add user"),
  );
  revealSlot.id = "reveal-slot";

  const rows = users.map((u) => h("tr", { class: u.disabled ? "off" : "" },
    h("td", {}, u.name, aiTag(u.kind)),
    h("td", { title: fmtDate(u.created_at) }, fmtAgo(u.created_at)),
    h("td", { class: u.disabled ? "status-off" : "" }, u.disabled ? "Disabled" : "Active"),
    h("td", { class: "actions" },
      h("button", { class: "link", type: "button", onclick: () => openTokens(u) }, "Tokens"),
      h("button", {
        class: `link ${u.disabled ? "" : "danger"}`, type: "button",
        onclick: async () => {
          if (!u.disabled) {
            const ok = await confirmAction({
              title: `Disable ${u.name}?`,
              body: `${u.name} is disconnected now and can't sign in until you enable the account again. Their documents stay.`,
              confirm: `Disable ${u.name}`, danger: true,
            });
            if (!ok) return;
          }
          const done = await run(
            () => api("PUT", `/v1/admin/users/${u.id}/disabled`, { disabled: !u.disabled }),
            u.disabled ? `Enabled ${u.name}` : `Disabled ${u.name}`,
          );
          if (done !== undefined) renderUsers(main);
        },
      }, u.disabled ? "Enable" : "Disable"),
    ),
  ));

  main.replaceChildren(
    h("h1", {}, "Users"),
    h("p", { class: "hint" }, "Each person needs a user and a token; the token goes into FreeCAD under Collab → Settings. An AI agent is added the same way, then invited to documents, where people talk to it in the chat with @name."),
    form,
    revealSlot,
    users.length
      ? h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Added"), h("th", {}, "Status"), h("th", {}, h("span", { class: "skip" }, "Actions")))),
          h("tbody", {}, rows)))
      : h("p", { class: "empty" }, "No users yet. Add one above to get a token you can hand out."),
  );
}

async function openTokens(user) {
  const tokens = await run(() => api("GET", `/v1/admin/users/${user.id}/tokens`));
  if (!tokens) return;
  const labelInput = h("input", { type: "text", id: "token-label", maxlength: 128, placeholder: "e.g. laptop" });
  const slot = h("div", {});
  const rows = tokens.map((t) => h("tr", { class: t.revoked_at ? "off" : "" },
    h("td", {}, t.label || "Unlabeled"),
    h("td", { title: fmtDate(t.created_at) }, fmtAgo(t.created_at)),
    h("td", { title: fmtDate(t.last_used_at) }, fmtAgo(t.last_used_at)),
    h("td", { class: t.revoked_at ? "status-off" : "" }, t.revoked_at ? `Revoked ${fmtAgo(t.revoked_at)}` : "Active"),
    h("td", { class: "actions" }, t.revoked_at ? null : h("button", {
      class: "link danger", type: "button",
      onclick: async () => {
        const ok = await confirmAction({
          title: "Revoke this token?",
          body: `FreeCAD installs using “${t.label || "this token"}” can no longer connect. Sessions already open continue until they disconnect.`,
          confirm: "Revoke token", danger: true,
        });
        if (ok && (await run(() => api("DELETE", `/v1/admin/tokens/${t.id}`), "Token revoked")) !== undefined) openTokens(user);
      },
    }, "Revoke")),
  ));
  openDialog(
    h("div", { class: "dialog-head" },
      h("h2", { id: "dialog-title" }, `Tokens for ${user.name}`),
      h("button", { class: "link", type: "button", onclick: closeDialog }, "Close"),
    ),
    h("form", {
      class: "inline-form",
      onsubmit: async (e) => {
        e.preventDefault();
        const issued = await run(() => api("POST", `/v1/admin/users/${user.id}/tokens`, { label: labelInput.value.trim() || null }));
        if (!issued) return;
        await openTokens(user);
        $("dialog").querySelector("#dialog-slot").replaceChildren(tokenReveal(user.name, issued.token, null, user.kind));
      },
    },
      h("div", { class: "field" }, h("label", { for: "token-label" }, "Label (optional)"), labelInput),
      h("button", { class: "btn", type: "submit" }, "Issue token"),
    ),
    Object.assign(slot, { id: "dialog-slot" }),
    tokens.length
      ? h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {}, h("th", {}, "Label"), h("th", {}, "Issued"), h("th", {}, "Last used"), h("th", {}, "Status"), h("th", {}, h("span", { class: "skip" }, "Actions")))),
          h("tbody", {}, rows)))
      : h("p", { class: "empty" }, "No tokens. Issue one to let this user connect."),
  );
}

// ---------------------------------------------------------------- documents

async function renderDocuments(main) {
  const [docs, users] = await Promise.all([
    run(() => api("GET", "/v1/admin/documents")),
    run(() => api("GET", "/v1/admin/users")),
  ]);
  if (!docs || !users) return;
  const active = users.filter((u) => !u.disabled);

  const nameInput = h("input", { type: "text", id: "new-doc", required: true, maxlength: 200, autocomplete: "off" });
  const ownerSelect = h("select", { id: "new-doc-owner", required: true },
    active.map((u) => h("option", { value: u.name }, u.name)));
  const form = active.length
    ? h("form", {
        class: "inline-form",
        onsubmit: async (e) => {
          e.preventDefault();
          const doc = await run(
            () => api("POST", "/v1/admin/documents", { name: nameInput.value.trim(), owner_name: ownerSelect.value }),
            "Document created",
          );
          if (doc) renderDocuments(main);
        },
      },
        h("div", { class: "field" }, h("label", { for: "new-doc" }, "New empty document"), nameInput),
        h("div", { class: "field" }, h("label", { for: "new-doc-owner" }, "Owner"), ownerSelect),
        h("button", { class: "btn", type: "submit" }, "Create document"),
      )
    : h("p", { class: "hint" }, "Add a user first; every document needs an owner.");

  const rows = docs.map((d) => h("tr", {},
    h("td", {}, h("button", { class: "link", type: "button", onclick: () => openDocument(d.id, main) }, d.name)),
    h("td", {}, d.owner_name),
    h("td", { class: "num" }, d.member_count),
    h("td", { title: fmtDate(d.updated_at) }, fmtAgo(d.updated_at)),
    h("td", { class: "num" }, d.head_seq.toLocaleString()),
    h("td", { class: "num", title: d.snapshot_seq != null ? `Covers changes up to #${d.snapshot_seq}` : "" }, fmtBytes(d.snapshot_bytes)),
    h("td", {}, d.live_sessions ? [h("span", { class: "live-dot" }), plural(d.live_sessions, "person", "people")] : ""),
  ));

  main.replaceChildren(
    h("h1", {}, "Documents"),
    h("p", { class: "hint" }, "Most documents are created from FreeCAD with Collab → Share active document. Open one to manage who can edit it."),
    form,
    docs.length
      ? h("div", { class: "table-wrap" }, h("table", {},
          h("thead", {}, h("tr", {},
            h("th", {}, "Name"), h("th", {}, "Owner"), h("th", { class: "num" }, "Members"),
            h("th", {}, "Last change"), h("th", { class: "num" }, "Changes"),
            h("th", { class: "num" }, "Snapshot"), h("th", {}, "Online"))),
          h("tbody", {}, rows)))
      : h("p", { class: "empty" }, "No documents yet. Share one from FreeCAD, or create an empty one above."),
  );
}

async function openDocument(id, main) {
  const [d, users] = await Promise.all([
    run(() => api("GET", `/v1/admin/documents/${id}`)),
    run(() => api("GET", "/v1/admin/users")),
  ]);
  if (!d || !users) return;
  const refresh = () => { openDocument(id, main); renderDocuments(main); };
  const memberIds = new Set(d.members.map((m) => m.user_id));
  const candidates = users.filter((u) => !u.disabled && !memberIds.has(u.id));

  const memberRows = d.members.map((m) => {
    const isCreator = m.user_id === d.owner_id;
    const roleCell = isCreator
      ? h("td", {}, "owner (creator)")
      : h("td", {}, h("select", {
          "aria-label": `Role for ${m.user_name}`,
          onchange: async (e) => {
            const role = e.target.value;
            if ((await run(() => api("PUT", `/v1/admin/documents/${id}/members`, { user_name: m.user_name, role }),
              `${m.user_name} is now ${role}`)) !== undefined) refresh();
          },
        }, ROLES.map((r) => h("option", { value: r, selected: r === m.role }, r))));
    return h("tr", {},
      h("td", {}, m.user_name, aiTag(m.user_kind)),
      roleCell,
      h("td", { title: fmtDate(m.added_at) }, fmtAgo(m.added_at)),
      h("td", { class: "actions" }, isCreator ? null : h("button", {
        class: "link danger", type: "button",
        onclick: async () => {
          const ok = await confirmAction({
            title: `Remove ${m.user_name}?`,
            body: `${m.user_name} loses access to “${d.name}” and is disconnected from it now.`,
            confirm: `Remove ${m.user_name}`, danger: true,
          });
          if (ok && (await run(() => api("DELETE", `/v1/admin/documents/${id}/members/${m.user_id}`), `Removed ${m.user_name}`)) !== undefined) refresh();
        },
      }, "Remove")),
    );
  });

  const addUser = h("select", { id: "add-member" },
    candidates.map((u) => h("option", { value: u.name }, u.kind === "agent" ? `${u.name} (AI agent)` : u.name)));
  const addRole = h("select", { id: "add-role" }, ROLES.map((r) => h("option", { value: r }, r)));
  const confirmName = h("input", { type: "text", id: "confirm-name", autocomplete: "off" });
  const deleteBtn = h("button", { class: "btn danger solid", type: "button", disabled: true }, "Delete document");
  confirmName.addEventListener("input", () => { deleteBtn.disabled = confirmName.value !== d.name; });
  deleteBtn.addEventListener("click", async () => {
    if ((await run(() => api("DELETE", `/v1/admin/documents/${id}`), `Deleted “${d.name}”`)) !== undefined) {
      closeDialog();
      renderDocuments(main);
    }
  });

  openDialog(
    h("div", { class: "dialog-head" },
      h("h2", { id: "dialog-title" }, d.name),
      h("button", { class: "link", type: "button", onclick: closeDialog }, "Close"),
    ),
    h("p", { class: "hint" },
      `${plural(d.head_seq, "change", "changes")} recorded, last ${fmtAgo(d.updated_at)}. `,
      d.snapshot ? `Latest snapshot ${fmtBytes(d.snapshot.size_bytes)}, taken ${fmtAgo(d.snapshot.created_at)}.` : "No snapshot yet."),
    d.participants.length
      ? h("p", {}, h("span", { class: "live-dot" }), "Online now: ", d.participants.map((p) => p.user_name).join(", "))
      : null,
    h("h3", {}, "Members"),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", {}, "Name"), h("th", {}, "Role"), h("th", {}, "Added"), h("th", {}, h("span", { class: "skip" }, "Actions")))),
      h("tbody", {}, memberRows))),
    candidates.length
      ? h("form", {
          class: "inline-form",
          onsubmit: async (e) => {
            e.preventDefault();
            if ((await run(() => api("PUT", `/v1/admin/documents/${id}/members`, { user_name: addUser.value, role: addRole.value }),
              `Added ${addUser.value}`)) !== undefined) refresh();
          },
        },
          h("div", { class: "field" }, h("label", { for: "add-member" }, "Add member"), addUser),
          h("div", { class: "field" }, h("label", { for: "add-role" }, "Role"), addRole),
          h("button", { class: "btn", type: "submit" }, "Add member"),
        )
      : h("p", { class: "hint" }, "Every active user is already a member."),
    h("div", { class: "danger-zone" },
      h("h3", {}, "Delete document"),
      h("p", {}, "This removes the document, its history and snapshots for everyone. People working on it are disconnected. It can't be undone."),
      h("div", { class: "inline-form" },
        h("div", { class: "field" }, h("label", { for: "confirm-name" }, `Type “${d.name}” to confirm`), confirmName),
        deleteBtn,
      ),
    ),
  );
}

// ---------------------------------------------------------------- boot

window.addEventListener("hashchange", () => { if (adminToken) route(); });
window.addEventListener("DOMContentLoaded", async () => {
  drawZones();
  $("signout").addEventListener("click", () => signOut("Signed out."));
  $("dialog").addEventListener("close", () => $("dialog").replaceChildren());
  if (!adminToken) return renderLogin();
  try {
    startApp(await api("GET", "/v1/admin/overview"));
  } catch (e) {
    signOut(e.message);
  }
});
