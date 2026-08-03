"""Simple .m3u8 read/write. Paths are written relative to Library/Playlists/, so
the whole Library/ folder stays portable as a single unit (copied for backup,
moved to another machine, whatever)."""

import os
from pathlib import Path


def write_m3u8(playlists_dir: str, name: str, track_library_paths: list[str], library_root: str) -> str:
    """track_library_paths are paths relative to Library/ (e.g.
    'Music/Artist/Album/01 Title.mp3'). Always rewrites the full file from the
    given list — playlist contents are regenerated from the database's current
    state on every build, not incrementally patched."""
    playlists_dir_path = Path(playlists_dir)
    playlists_dir_path.mkdir(parents=True, exist_ok=True)
    m3u8_path = playlists_dir_path / f"{name}.m3u8"

    lines = ["#EXTM3U"]
    for rel_path in track_library_paths:
        absolute = Path(library_root) / rel_path
        relative_to_playlists = os.path.relpath(absolute, playlists_dir_path)
        lines.append(relative_to_playlists.replace(os.sep, "/"))

    m3u8_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(m3u8_path)
