#!/usr/bin/env bash
# Build a release bundle into dist/.
#   ./scripts/dist.sh linux     -> dist/freecad-collab-<version>-linux-<arch>.tar.gz
#   ./scripts/dist.sh windows   -> dist/freecad-collab-<version>-windows-x86_64.zip
# On Linux, "windows" cross-compiles (needs: rustup target add x86_64-pc-windows-gnu,
# plus mingw-w64). On Windows (Git Bash), it uses the native build.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
platform="${1:-linux}"
version="$(sed -n 's/^version = "\(.*\)"$/\1/p' Cargo.toml | head -n 1)"
py="$(command -v python3 || command -v python || true)"
[[ -n $py ]] || { echo "error: python is needed to write archives" >&2; exit 1; }

stage_common() {
    local dir="$1"
    cp -R plugin/CollabSync "$dir/CollabSync"
    find "$dir/CollabSync" -name __pycache__ -type d -prune -exec rm -rf {} +
    rm -f "$dir/CollabSync/collab_sync/defaults.json"
    mkdir -p "$dir/agent"
    cp -R agent/collab_agent agent/run_agent.py agent/agent.env.example "$dir/agent/"
    find "$dir/agent" -name __pycache__ -type d -prune -exec rm -rf {} +
    cp README.md LICENSE "$dir/"
}

case $platform in
    linux)
        cargo build --release --locked
        name="freecad-collab-$version-linux-$(uname -m)"
        rm -rf "dist/$name" && mkdir -p "dist/$name"
        cp target/release/freecad-collab-server installer/linux/install.sh "dist/$name/"
        stage_common "dist/$name"
        cp agent/collab-agent "dist/$name/agent/"
        cat > "dist/$name/INSTALL.txt" <<TXT
Server (one machine):   sudo ./install.sh server --open-firewall
FreeCAD plugin (each):  ./install.sh plugin --server-url http://<server-ip>:14008
AI agent (optional):    sudo ./install.sh agent --token <agent token> --api-key <key>
Menu:                   ./install.sh
TXT
        tar -C dist -czf "dist/$name.tar.gz" "$name"
        echo "dist/$name.tar.gz"
        ;;
    windows)
        if [[ ${OS:-} == Windows_NT ]]; then
            cargo build --release --locked
            exe=target/release/freecad-collab-server.exe
        else
            cargo build --release --locked --target x86_64-pc-windows-gnu
            exe=target/x86_64-pc-windows-gnu/release/freecad-collab-server.exe
        fi
        name="freecad-collab-$version-windows-x86_64"
        rm -rf "dist/$name" && mkdir -p "dist/$name"
        cp "$exe" installer/windows/install.ps1 installer/windows/install.cmd "dist/$name/"
        stage_common "dist/$name"
        cp agent/collab-agent.cmd "dist/$name/agent/"
        printf '%s\r\n' \
            'Double-click install.cmd and pick an option.' \
            'Server (one computer): option 2. FreeCAD plugin (everyone): option 1.' \
            'AI agent (optional, needs FreeCAD on that computer): option 6.' \
            > "dist/$name/INSTALL.txt"
        (cd dist && "$py" -m zipfile -c "$name.zip" "$name")
        echo "dist/$name.zip"
        ;;
    *)
        echo "usage: $0 linux|windows" >&2
        exit 1
        ;;
esac
