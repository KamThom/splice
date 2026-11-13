#!/usr/bin/env bash
# Adds Splice to your Linux app menu (GNOME, KDE, etc.) with its icon.
#
#   ./install-linux.sh              install (or update) the launcher
#   ./install-linux.sh --uninstall  remove the launcher and icon
#
# Everything goes in your home folder (~/.local/share), so no sudo is needed.
# Run it again after moving the project folder — the launcher points at
# wherever this script lives.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
DESKTOP_FILE="$DATA_HOME/applications/splice.desktop"
ICON_FILE="$DATA_HOME/icons/hicolor/scalable/apps/splice.svg"

refresh_caches() {
    command -v update-desktop-database >/dev/null && update-desktop-database "$DATA_HOME/applications" 2>/dev/null || true
    command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -t "$DATA_HOME/icons/hicolor" 2>/dev/null || true
}

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$DESKTOP_FILE" "$ICON_FILE"
    refresh_caches
    echo "Removed the Splice launcher and icon."
    exit 0
fi

# 1. Python environment: create .venv and install requirements if missing.
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    echo "Setting up the Python environment in $APP_DIR/.venv ..."
    if ! python3 -m venv "$APP_DIR/.venv"; then
        echo "Couldn't create a virtual environment. Install venv support first:" >&2
        echo "  sudo apt install python3-venv" >&2
        exit 1
    fi
    "$APP_DIR/.venv/bin/pip" install --upgrade pip >/dev/null
    "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
fi

# 2. Icon.
mkdir -p "$(dirname "$ICON_FILE")" "$(dirname "$DESKTOP_FILE")"
cp "$APP_DIR/splice/assets/icon.svg" "$ICON_FILE"

# 3. Launcher. Exec runs the venv's Python directly, so nothing needs activating.
cat > "$DESKTOP_FILE" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Splice
GenericName=Music and Audiobook Library
Comment=Organize, merge, split, download and tag mp3 music and audiobooks
Exec="$APP_DIR/.venv/bin/python" "$APP_DIR/splice.py"
Path=$APP_DIR
Icon=splice
Terminal=false
Categories=AudioVideo;Audio;
Keywords=music;audiobook;mp3;playlist;youtube;tags;
StartupNotify=true
StartupWMClass=splice
DESKTOP
chmod 644 "$DESKTOP_FILE"

if command -v desktop-file-validate >/dev/null; then
    desktop-file-validate "$DESKTOP_FILE"
fi
refresh_caches

if ! command -v ffmpeg >/dev/null; then
    echo "Note: ffmpeg isn't installed — Merge, Split and YouTube need it:  sudo apt install ffmpeg"
fi
echo "Installed. Look for \"Splice\" in your app menu (it can take a few seconds to appear)."
echo "To remove it later:  $APP_DIR/install-linux.sh --uninstall"
