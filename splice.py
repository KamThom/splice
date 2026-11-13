#!/usr/bin/env python3
"""Splice — merge, split, download, and tag mp3 audiobooks and music."""

import os
import sys


def _undo_vscode_snap_env():
    """The snap build of VS Code points GTK/GSettings paths (GSETTINGS_SCHEMA_DIR,
    XDG_DATA_DIRS, GTK_PATH, ...) at its own bundled, older GNOME schemas, and
    every terminal / F5 launch inherits them. Qt then loads the host's GTK, which
    asks for settings keys those old schemas lack, and GLib aborts the process
    ("Settings schema ... does not contain a key named 'antialiasing'").
    The snap saves each original value as <NAME>_VSCODE_SNAP_ORIG — restore them
    (an empty original means the variable wasn't set at all)."""
    suffix = "_VSCODE_SNAP_ORIG"
    for key in [k for k in os.environ if k.endswith(suffix)]:
        name = key[: -len(suffix)]
        original = os.environ.pop(key)
        if original:
            os.environ[name] = original
        else:
            os.environ.pop(name, None)


if sys.platform.startswith("linux"):
    _undo_vscode_snap_env()

from splice.main_window import main  # noqa: E402 — env must be fixed before Qt loads

if __name__ == "__main__":
    main()
