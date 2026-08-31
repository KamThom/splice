"""Audiobook series cover art — structurally identical to playlists.py's
artwork storage, but keyed by the series NAME (sanitized) rather than a
numeric id, since a "series" is just a distinct Album tag value among
audiobooks, not a real database entity. Accepted trade-off: renaming a
series' Album tag orphans its artwork file, unlike playlist artwork (which
is id-keyed specifically to survive a rename) — a deliberate simplicity
choice, not an oversight."""

import shutil
from pathlib import Path

from ..workers import sanitize_filename

ARTWORK_SUBDIR = ".artwork"


def artwork_dir(library_root: str) -> Path:
    return Path(library_root) / "Audiobooks" / ARTWORK_SUBDIR


def find_series_artwork(library_root: str, series_name: str) -> Path | None:
    matches = sorted(artwork_dir(library_root).glob(f"{sanitize_filename(series_name)}.*"))
    return matches[0] if matches else None


def set_series_artwork(library_root: str, series_name: str, source_image_path: str) -> Path:
    """Copies source_image_path in as this series' artwork, replacing
    whatever image (if any) was set before — even if the extension changed."""
    clear_series_artwork(library_root, series_name)
    target_dir = artwork_dir(library_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    ext = Path(source_image_path).suffix or ".jpg"
    target = target_dir / f"{sanitize_filename(series_name)}{ext}"
    shutil.copy2(source_image_path, target)
    return target


def clear_series_artwork(library_root: str, series_name: str):
    existing = find_series_artwork(library_root, series_name)
    if existing:
        existing.unlink(missing_ok=True)
