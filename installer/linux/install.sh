#!/usr/bin/env bash
# FreeCAD collab installer for Linux.
#
#   sudo ./install.sh server [--port 14008] [--bind 0.0.0.0] [--open-firewall]
#        ./install.sh plugin [--server-url http://host:14008] [--mod-dir DIR]
#   sudo ./install.sh uninstall-server [--purge]
#        ./install.sh uninstall-plugin [--mod-dir DIR]
#   sudo ./install.sh agent [--server-url URL] [--token T] [--provider anthropic|openai]
#                           [--model M] [--api-key K] [--base-url URL]
#   sudo ./install.sh uninstall-agent [--purge]
#        ./install.sh            (interactive menu)
#
# Works from a release bundle (binary next to this script) or from the source
# tree after `just build`.
set -euo pipefail

APP=freecad-collab-server
SERVICE=freecad-collab
SVC_USER=freecad-collab
BIN_DIR=/usr/local/bin
CONF_DIR=/etc/freecad-collab
CONF_FILE=$CONF_DIR/collab.env
DATA_DIR=/var/lib/freecad-collab
UNIT_FILE=/etc/systemd/system/$SERVICE.service
PLUGIN_NAME=CollabSync
AGENT_SERVICE=freecad-collab-agent
AGENT_USER=freecad-collab-agent
AGENT_DIR=/opt/freecad-collab-agent
AGENT_CONF_DIR=/etc/freecad-collab-agent
AGENT_CONF=$AGENT_CONF_DIR/agent.env
AGENT_HOME=/var/lib/freecad-collab-agent
AGENT_UNIT=/etc/systemd/system/$AGENT_SERVICE.service

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

info() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }

need_root() {
    [[ $EUID -eq 0 ]] || die "this step needs root: sudo $0 $*"
}

first_existing() {
    for p in "$@"; do
        [[ -e $p ]] && { printf '%s\n' "$p"; return 0; }
    done
    return 1
}

find_binary() {
    first_existing "$here/$APP" "$here/../../target/release/$APP" \
        || die "no $APP binary found. Use a release bundle, or run 'just build' in the source tree first."
}

find_plugin() {
    first_existing "$here/$PLUGIN_NAME" "$here/../../plugin/$PLUGIN_NAME" \
        || die "plugin folder $PLUGIN_NAME not found next to the installer"
}

has_systemd() {
    [[ -d /run/systemd/system ]] && command -v systemctl >/dev/null
}

random_hex() {
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
}

primary_ip() {
    local ip
    ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<=NF;i++) if ($i=="src") {print $(i+1); exit}}')" || true
    printf '%s\n' "${ip:-127.0.0.1}"
}

conf_get() {
    sed -n "s/^$1=//p" "$CONF_FILE" | tail -n 1
}

conf_set() {
    if grep -q "^$1=" "$CONF_FILE"; then
        sed -i "s|^$1=.*|$1=$2|" "$CONF_FILE"
    else
        printf '%s=%s\n' "$1" "$2" >> "$CONF_FILE"
    fi
}

# ------------------------------------------------------------------ server

install_server() {
    local port="" bind="" open_fw=0
    while [[ $# -gt 0 ]]; do
        case $1 in
            --port) port="$2"; shift 2 ;;
            --bind) bind="$2"; shift 2 ;;
            --open-firewall) open_fw=1; shift ;;
            *) die "unknown option for server: $1" ;;
        esac
    done
    need_root server
    [[ -z $port || $port =~ ^[0-9]+$ && $port -ge 1 && $port -le 65535 ]] || die "invalid port: $port"

    local bin
    bin="$(find_binary)"
    info "1/5 installing $BIN_DIR/$APP"
    if has_systemd && systemctl is-active --quiet "$SERVICE"; then
        systemctl stop "$SERVICE"
    fi
    install -m 0755 "$bin" "$BIN_DIR/$APP"

    info "2/5 creating service user and directories"
    if ! id -u "$SVC_USER" >/dev/null 2>&1; then
        local nologin
        nologin="$(command -v nologin || echo /bin/false)"
        useradd --system --home-dir "$DATA_DIR" --no-create-home --shell "$nologin" "$SVC_USER"
    fi
    install -d -m 0750 -o "$SVC_USER" -g "$SVC_USER" "$DATA_DIR"
    install -d -m 0750 -o root -g "$SVC_USER" "$CONF_DIR"

    info "3/5 writing configuration"
    if [[ ! -f $CONF_FILE ]]; then
        umask 027
        cat > "$CONF_FILE" <<EOF
# FreeCAD collab server configuration. Restart after editing:
#   sudo systemctl restart $SERVICE
COLLAB_BIND_ADDR=${bind:-0.0.0.0}:${port:-14008}
COLLAB_DATABASE_URL=sqlite://$DATA_DIR/collab.db
COLLAB_ADMIN_TOKEN=$(random_hex)
COLLAB_LOG_FORMAT=pretty
RUST_LOG=info,tower_http=warn,sqlx=warn
EOF
        chown root:"$SVC_USER" "$CONF_FILE"
        chmod 0640 "$CONF_FILE"
    else
        echo "    keeping existing $CONF_FILE"
        if [[ -n $port || -n $bind ]]; then
            local cur host cur_port
            cur="$(conf_get COLLAB_BIND_ADDR)"
            host="${cur%:*}"; cur_port="${cur##*:}"
            conf_set COLLAB_BIND_ADDR "${bind:-${host:-0.0.0.0}}:${port:-${cur_port:-14008}}"
        fi
    fi
    local bind_addr listen_port
    bind_addr="$(conf_get COLLAB_BIND_ADDR)"
    listen_port="${bind_addr##*:}"

    info "4/5 setting up the service"
    if has_systemd; then
        local caps=""
        if [[ $listen_port -lt 1024 ]]; then
            caps=$'AmbientCapabilities=CAP_NET_BIND_SERVICE\nCapabilityBoundingSet=CAP_NET_BIND_SERVICE'
        else
            caps="CapabilityBoundingSet="
        fi
        cat > "$UNIT_FILE" <<EOF
[Unit]
Description=FreeCAD collaboration server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$SVC_USER
Group=$SVC_USER
EnvironmentFile=$CONF_FILE
WorkingDirectory=$DATA_DIR
ExecStart=$BIN_DIR/$APP serve
Restart=on-failure
RestartSec=2
TimeoutStopSec=15
UMask=0027
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
RestrictNamespaces=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
SystemCallArchitectures=native
$caps
ReadWritePaths=$DATA_DIR

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        systemctl enable --quiet "$SERVICE"
        systemctl restart "$SERVICE"
    else
        warn "systemd not found; start the server yourself:"
        echo "    sudo -u $SVC_USER $BIN_DIR/$APP --env-file $CONF_FILE serve"
    fi

    if [[ $open_fw -eq 1 ]]; then
        if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
            ufw allow "$listen_port/tcp" >/dev/null && echo "    ufw: opened $listen_port/tcp"
        elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
            firewall-cmd --quiet --permanent --add-port="$listen_port/tcp" && firewall-cmd --quiet --reload \
                && echo "    firewalld: opened $listen_port/tcp"
        else
            echo "    no active ufw/firewalld found; nothing to open"
        fi
    fi

    info "5/5 checking the server"
    local ok=0
    if has_systemd && command -v curl >/dev/null; then
        for _ in $(seq 1 30); do
            if curl -fsS "http://127.0.0.1:$listen_port/readyz" >/dev/null 2>&1; then ok=1; break; fi
            sleep 0.5
        done
        if [[ $ok -eq 1 ]]; then
            echo "    server is ready"
        else
            warn "server did not answer; see: journalctl -u $SERVICE -n 50"
        fi
    fi

    local url
    url="http://$(primary_ip):$listen_port"
    echo
    info "Server installed."
    echo "  Admin console: $url/admin"
    echo "  Admin token:   $(conf_get COLLAB_ADMIN_TOKEN)"
    echo "  Config:        $CONF_FILE"
    echo "  Data:          $DATA_DIR"
    echo "  Logs:          journalctl -u $SERVICE -f"
    echo
    echo "Next: open the admin console, add a user, and give them the token plus this address: $url"
}

uninstall_server() {
    local purge=0
    [[ ${1:-} == --purge ]] && purge=1
    need_root uninstall-server
    if has_systemd && [[ -f $UNIT_FILE ]]; then
        systemctl disable --now --quiet "$SERVICE" || true
        rm -f "$UNIT_FILE"
        systemctl daemon-reload
    fi
    rm -f "$BIN_DIR/$APP"
    if [[ $purge -eq 1 ]]; then
        rm -rf "$DATA_DIR" "$CONF_DIR"
        id -u "$SVC_USER" >/dev/null 2>&1 && userdel "$SVC_USER" || true
        info "Server removed, including all documents and configuration."
    else
        info "Server removed. Documents kept in $DATA_DIR, config in $CONF_DIR (use --purge to delete)."
    fi
}

# ------------------------------------------------------------------ plugin

target_user() {
    if [[ $EUID -eq 0 && -n ${SUDO_USER:-} && $SUDO_USER != root ]]; then
        printf '%s\n' "$SUDO_USER"
    else
        id -un
    fi
}

user_home() {
    getent passwd "$1" | cut -d: -f6
}

# All FreeCAD Mod directories for a user: native and Flatpak, unversioned
# (<= 1.0) and versioned (>= 1.1, e.g. v1-1).
mod_dirs() {
    local home="$1" base found=()
    local bases=("$home/.local/share/FreeCAD" "$home/.var/app/org.freecad.FreeCAD/data/FreeCAD")
    [[ -n ${XDG_DATA_HOME:-} && $EUID -ne 0 ]] && bases=("$XDG_DATA_HOME/FreeCAD" "${bases[@]}")
    for base in "${bases[@]}"; do
        [[ -d $base ]] || continue
        found+=("$base/Mod")
        for v in "$base"/v[0-9]*-[0-9]*; do
            [[ -d $v ]] && found+=("$v/Mod")
        done
    done
    [[ ${#found[@]} -eq 0 ]] && found=("$home/.local/share/FreeCAD/Mod")
    printf '%s\n' "${found[@]}" | awk '!seen[$0]++'
}

install_plugin() {
    local server_url="" mod_dir=""
    while [[ $# -gt 0 ]]; do
        case $1 in
            --server-url) server_url="$2"; shift 2 ;;
            --mod-dir) mod_dir="$2"; shift 2 ;;
            *) die "unknown option for plugin: $1" ;;
        esac
    done
    [[ -z $server_url || $server_url =~ ^https?://[^[:space:]\"]+$ ]] || die "server URL must start with http:// or https://"

    local src user home
    src="$(find_plugin)"
    user="$(target_user)"
    home="$(user_home "$user")"
    [[ -n $home ]] || die "cannot find home directory of $user"

    local dirs=()
    if [[ -n $mod_dir ]]; then dirs=("$mod_dir"); else mapfile -t dirs < <(mod_dirs "$home"); fi

    for d in "${dirs[@]}"; do
        local dest="$d/$PLUGIN_NAME"
        mkdir -p "$d"
        rm -rf "$dest"
        cp -R "$src" "$dest"
        find "$dest" -name __pycache__ -type d -prune -exec rm -rf {} +
        rm -f "$dest/collab_sync/defaults.json"
        if [[ -n $server_url ]]; then
            printf '{"server_url": "%s"}\n' "$server_url" > "$dest/collab_sync/defaults.json"
        fi
        if [[ $EUID -eq 0 && $user != root ]]; then
            chown -R "$user": "$d"
        fi
        echo "    installed $dest"
    done
    echo
    info "Plugin installed for $user."
    echo "Next: restart FreeCAD, pick the Collab workbench, open Collab → Settings and paste your token."
    command -v freecad >/dev/null || command -v FreeCAD >/dev/null || [[ -d $home/.var/app/org.freecad.FreeCAD ]] \
        || warn "FreeCAD itself was not found on this machine"
}

uninstall_plugin() {
    local mod_dir=""
    [[ ${1:-} == --mod-dir ]] && mod_dir="$2"
    local home dirs=()
    home="$(user_home "$(target_user)")"
    if [[ -n $mod_dir ]]; then dirs=("$mod_dir"); else mapfile -t dirs < <(mod_dirs "$home"); fi
    for d in "${dirs[@]}"; do
        if [[ -e $d/$PLUGIN_NAME ]]; then
            rm -rf "${d:?}/$PLUGIN_NAME"
            echo "    removed $d/$PLUGIN_NAME"
        fi
    done
    info "Plugin removed."
}

# ------------------------------------------------------------------ agent

find_agent() {
    first_existing "$here/agent/run_agent.py" "$here/../../agent/run_agent.py" >/dev/null \
        || die "agent folder not found next to the installer"
    dirname "$(first_existing "$here/agent/run_agent.py" "$here/../../agent/run_agent.py")"
}

agent_conf_set() {
    local key="$1" value="$2"
    [[ -n $value ]] || return 0
    if grep -q "^#\{0,1\} \{0,1\}$key=" "$AGENT_CONF"; then
        sed -i "0,/^#\{0,1\} \{0,1\}$key=.*/s||$key=$value|" "$AGENT_CONF"
    else
        printf '%s=%s\n' "$key" "$value" >> "$AGENT_CONF"
    fi
}

install_agent() {
    local server_url="" token="" provider="" model="" api_key="" base_url=""
    while [[ $# -gt 0 ]]; do
        case $1 in
            --server-url) server_url="$2"; shift 2 ;;
            --token) token="$2"; shift 2 ;;
            --provider) provider="$2"; shift 2 ;;
            --model) model="$2"; shift 2 ;;
            --api-key) api_key="$2"; shift 2 ;;
            --base-url) base_url="$2"; shift 2 ;;
            *) die "unknown option for agent: $1" ;;
        esac
    done
    need_root agent
    local src plugin
    src="$(find_agent)"
    plugin="$(find_plugin)"

    info "1/4 installing the AI agent into $AGENT_DIR"
    if has_systemd && systemctl is-active --quiet "$AGENT_SERVICE"; then
        systemctl stop "$AGENT_SERVICE"
    fi
    rm -rf "$AGENT_DIR"
    install -d -m 0755 "$AGENT_DIR"
    cp -R "$src"/collab_agent "$src"/run_agent.py "$src"/collab-agent "$AGENT_DIR/"
    cp -R "$plugin" "$AGENT_DIR/CollabSync"
    find "$AGENT_DIR" -name __pycache__ -type d -prune -exec rm -rf {} +
    chmod 0755 "$AGENT_DIR/collab-agent"

    info "2/4 creating the agent's system user"
    if ! id -u "$AGENT_USER" >/dev/null 2>&1; then
        local nologin
        nologin="$(command -v nologin || echo /bin/false)"
        useradd --system --home-dir "$AGENT_HOME" --no-create-home --shell "$nologin" "$AGENT_USER"
    fi
    install -d -m 0750 -o "$AGENT_USER" -g "$AGENT_USER" "$AGENT_HOME"

    info "3/4 writing $AGENT_CONF"
    install -d -m 0750 -o root -g "$AGENT_USER" "$AGENT_CONF_DIR"
    if [[ ! -f $AGENT_CONF ]]; then
        install -m 0640 -o root -g "$AGENT_USER" "$src/agent.env.example" "$AGENT_CONF"
        if [[ -z $server_url && -f $CONF_FILE ]]; then
            server_url="http://127.0.0.1:$(conf_get COLLAB_BIND_ADDR | sed 's/.*://')"
        fi
    else
        echo "    keeping existing values in $AGENT_CONF"
    fi
    agent_conf_set COLLAB_SERVER_URL "$server_url"
    agent_conf_set COLLAB_AGENT_TOKEN "$token"
    agent_conf_set COLLAB_AGENT_PROVIDER "$provider"
    agent_conf_set COLLAB_AGENT_MODEL "$model"
    agent_conf_set COLLAB_AGENT_API_KEY "$api_key"
    agent_conf_set COLLAB_AGENT_BASE_URL "$base_url"

    local fccmd=""
    for c in freecadcmd FreeCADCmd freecad.cmd; do
        command -v "$c" >/dev/null && { fccmd="$c"; break; }
    done
    [[ -n $fccmd ]] || warn "freecadcmd not found: install FreeCAD on this machine (e.g. pacman -S freecad, apt install freecad)"

    info "4/4 setting up the service"
    local ready=1
    grep -q '^COLLAB_AGENT_TOKEN=.\+' "$AGENT_CONF" || ready=0
    if grep -q '^COLLAB_AGENT_PROVIDER=anthropic' "$AGENT_CONF"; then
        grep -q '^COLLAB_AGENT_API_KEY=.\+' "$AGENT_CONF" || ready=0
    fi
    if has_systemd; then
        cat > "$AGENT_UNIT" <<EOF
[Unit]
Description=FreeCAD collaboration AI agent
After=network-online.target $SERVICE.service
Wants=network-online.target

[Service]
Type=simple
User=$AGENT_USER
Group=$AGENT_USER
Environment=HOME=$AGENT_HOME
Environment=QT_QPA_PLATFORM=offscreen
WorkingDirectory=$AGENT_HOME
ExecStart=$AGENT_DIR/collab-agent --env-file $AGENT_CONF
Restart=on-failure
RestartSec=10
# Exit code 2 = configuration error: don't retry until the config is fixed.
RestartPreventExitStatus=2
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes
ReadWritePaths=$AGENT_HOME

[Install]
WantedBy=multi-user.target
EOF
        systemctl daemon-reload
        if [[ $ready -eq 1 ]]; then
            systemctl enable --quiet "$AGENT_SERVICE"
            systemctl restart "$AGENT_SERVICE"
            echo "    agent started"
        fi
    fi
    echo
    info "AI agent installed."
    if [[ $ready -eq 0 ]]; then
        echo "  It is not started yet: set COLLAB_AGENT_TOKEN and your model settings in $AGENT_CONF"
        echo "  (token: admin console → Users → add user with kind 'AI agent'), then run:"
        echo "    sudo systemctl enable --now $AGENT_SERVICE"
    fi
    echo "  Config: $AGENT_CONF"
    echo "  Logs:   journalctl -u $AGENT_SERVICE -f"
    echo
    echo "Next: invite the agent to a document (admin console or Collab → Invite), then write @<agent name> in the chat."
}

uninstall_agent() {
    need_root uninstall-agent
    if has_systemd && [[ -f $AGENT_UNIT ]]; then
        systemctl disable --now --quiet "$AGENT_SERVICE" || true
        rm -f "$AGENT_UNIT"
        systemctl daemon-reload
    fi
    rm -rf "$AGENT_DIR"
    if [[ ${1:-} == --purge ]]; then
        rm -rf "$AGENT_CONF_DIR" "$AGENT_HOME"
        id -u "$AGENT_USER" >/dev/null 2>&1 && userdel "$AGENT_USER" || true
        info "AI agent removed, including its configuration."
    else
        info "AI agent removed. Config kept in $AGENT_CONF_DIR (use --purge to delete)."
    fi
}

# ------------------------------------------------------------------ main

usage() {
    sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'
}

menu() {
    echo "FreeCAD collab installer"
    echo "  1) Install the plugin for FreeCAD (for everyone who collaborates)"
    echo "  2) Install the server (one machine only, needs sudo)"
    echo "  3) Install both"
    echo "  4) Uninstall the plugin"
    echo "  5) Uninstall the server"
    echo "  6) Install the AI agent (needs sudo and FreeCAD on this machine)"
    echo "  7) Uninstall the AI agent"
    read -r -p "Choose 1-7: " choice
    case $choice in
        1) read -r -p "Server address (e.g. http://192.168.1.10:14008, empty to skip): " url
           if [[ -n $url ]]; then install_plugin --server-url "$url"; else install_plugin; fi ;;
        2) install_server ;;
        3) install_server && install_plugin --server-url "http://127.0.0.1:$(conf_get COLLAB_BIND_ADDR | sed 's/.*://')" ;;
        4) uninstall_plugin ;;
        5) uninstall_server ;;
        6) install_agent ;;
        7) uninstall_agent ;;
        *) die "no such choice: $choice" ;;
    esac
}

main() {
    local cmd="${1:-}"
    [[ $# -gt 0 ]] && shift
    case $cmd in
        server) install_server "$@" ;;
        plugin) install_plugin "$@" ;;
        uninstall-server) uninstall_server "$@" ;;
        uninstall-plugin) uninstall_plugin "$@" ;;
        agent) install_agent "$@" ;;
        uninstall-agent) uninstall_agent "$@" ;;
        -h|--help|help) usage ;;
        "") if [[ -t 0 ]]; then menu; else usage; exit 1; fi ;;
        *) usage; exit 1 ;;
    esac
}

main "$@"
