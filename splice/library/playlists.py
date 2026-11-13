"""Simple .m3u8 read/write. Paths are written relative to Library/Playlists/, so
the whole Library/ folder stays portable as a single unit (copied for backup,
moved to another machine, whatever)."""

import os
import shutil
from pathlib import Path

ARTWORK_SUBDIR = ".artwork"


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


def artwork_dir(library_root: str) -> Path:
    return Path(library_root) / "Playlists" / ARTWORK_SUBDIR


def find_playlist_artwork(library_root: str, playlist_id: int) -> Path | None:
    """The playlist's cover image, if one has been set. Stored by playlist id,
    not name — unlike the .m3u8 (which is keyed by name and gets deleted and
    rewritten on rename), an id-keyed file never has to move just because the
    playlist was renamed. Whatever extension the source image had is kept
    as-is, so this globs for it rather than assuming one."""
    matches = sorted(artwork_dir(library_root).glob(f"{playlist_id}.*"))
    return matches[0] if matches else None


def set_playlist_artwork(library_root: str, playlist_id: int, source_image_path: str) -> Path:
    """Copies source_image_path in as this playlist's artwork, replacing
    whatever image (if any) was set before — even if the extension changed."""
    clear_playlist_artwork(library_root, playlist_id)
    target_dir = artwork_dir(library_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(source_image_path).suffix or ".jpg"
    target = target_dir / f"{playlist_id}{ext}"
    shutil.copy2(source_image_path, target)
    return target


def clear_playlist_artwork(library_root: str, playlist_id: int):
    existing = find_playlist_artwork(library_root, playlist_id)
    if existing:
        existing.unlink(missing_ok=True)
