#!/usr/bin/env bash
# Symlink the CollabSync plugin into FreeCAD's Mod directory.
# Usage: ./scripts/install-plugin.sh [mod_dir]
set -euo pipefail

src="$(cd "$(dirname "$0")/../plugin/CollabSync" && pwd)"

if [[ $# -ge 1 && -n "$1" ]]; then
    mod="$1"
else
    case "$(uname -s)" in
        Darwin) base="$HOME/Library/Application Support/FreeCAD" ;;
        *)      base="${XDG_DATA_HOME:-$HOME/.local/share}/FreeCAD" ;;
    esac
    # FreeCAD >= 1.1 uses versioned data dirs (e.g. v1-1); prefer the newest.
    latest="$(ls -d "$base"/v1-* 2>/dev/null | sort -V | tail -n 1 || true)"
    mod="${latest:-$base}/Mod"
fi

mkdir -p "$mod"
ln -sfn "$src" "$mod/CollabSync"
echo "installed: $mod/CollabSync -> $src"
echo "restart FreeCAD. If the Collab workbench is missing, run in FreeCAD's Python console:"
echo "  print(App.getUserAppDataDir())"
echo "and re-run: $0 <that path>/Mod"
