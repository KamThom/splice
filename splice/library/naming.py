"""Canonical library destination paths — where a scanned file gets copied to."""

from pathlib import Path

from ..workers import sanitize_filename
from .scanner import ScannedFile


def music_destination(scanned: ScannedFile) -> Path:
    """Path relative to Library/Music/. Any missing/blank title, artist, or
    album routes to _Unsorted/ rather than a guessed real-artist folder —
    doesn't pollute the real tree with placeholders."""
    if not (scanned.title.strip() and scanned.artist.strip() and scanned.album.strip()):
        return Path("_Unsorted") / Path(scanned.path).name

    artist = sanitize_filename(scanned.artist)
    album = sanitize_filename(scanned.album)
    title = sanitize_filename(scanned.title)

    prefix = ""
    if scanned.tracknumber.strip():
        try:
            prefix = f"{int(scanned.tracknumber.split('/')[0].strip()):02d} "
        except ValueError:
            prefix = ""

    return Path(artist) / album / f"{prefix}{title}.mp3"


def audiobook_destination(source_root: Path, file_path: Path) -> Path:
    """Structure-preserving: path relative to Library/Audiobooks/, mirroring the
    file's position under the chosen audiobooks source folder exactly — no
    renaming, the user's existing filenames (e.g. 1_leviathan_wakes.mp3) and
    series-folder structure are kept as-is."""
    return file_path.relative_to(source_root)
