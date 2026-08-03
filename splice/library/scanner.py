"""Read-only mp3 scanning: tags, duration, and a streamed BLAKE3 hash.

Deliberately does NOT reuse splice/tagging.py::read_tags — that helper writes an
empty ID3 header to any file that doesn't have one, as a side effect of reading.
Fine for editing the one file you opened on purpose in the Tags module; wrong for
scanning thousands of files during a library scan, which must never mutate the
user's existing collection.
"""

from dataclasses import dataclass
from pathlib import Path

import blake3
from mutagen.id3 import ID3NoHeaderError
from mutagen.easyid3 import EasyID3
from mutagen.mp3 import MP3

from ..workers import probe_duration_seconds

HASH_CHUNK_SIZE = 1024 * 1024  # 1 MiB — stream the hash, never load a whole file into memory


@dataclass
class ScannedFile:
    path: str
    hash: str
    title: str
    artist: str
    album: str
    date: str
    tracknumber: str
    duration_seconds: float | None


def hash_file(path: str) -> str:
    hasher = blake3.blake3()
    with open(path, "rb") as f:
        while chunk := f.read(HASH_CHUNK_SIZE):
            hasher.update(chunk)
    return hasher.hexdigest()


def read_scan_info(path: str) -> tuple[ScannedFile | None, str]:
    """Returns (ScannedFile, "") on success, or (None, reason) if the file
    can't be read at all — corrupt, not really an mp3 despite the extension,
    locked, permission denied, whatever. The reason is surfaced in the scan/
    build logs so an "unreadable" file is diagnosable by name and cause instead
    of just disappearing with no explanation.

    Tags and duration are read independently, deliberately. Mutagen's own MP3
    stream-info parser (MPEGInfo) only searches a 1MB window for a run of
    confidently-valid consecutive frames, and rejects some encoder quirks
    outright — real, perfectly playable files (particularly ones with large
    embedded cover art, or from less-common encoders/converters) can fail that
    check with HeaderNotFoundError even though their ID3 tags parse just fine
    (tag parsing doesn't touch the audio stream at all). Treating that as
    "can't read this file" was silently dropping legitimate music. Tags are
    read via EasyID3 regardless of whether the stream-info check succeeds; if
    mutagen's own parser can't find a sync, duration falls back to ffprobe
    (already a hard dependency here, and considerably more lenient)."""
    try:
        tags = EasyID3(path)
    except ID3NoHeaderError:
        tags = {}
    except Exception:
        tags = {}

    duration = None
    try:
        duration = MP3(path).info.length
    except Exception:
        duration = probe_duration_seconds(path)

    try:
        file_hash = hash_file(path)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

    if duration is None:
        return None, "could not determine audio stream info (tried mutagen and ffprobe)"

    def first(key: str) -> str:
        values = tags.get(key)
        return values[0] if values else ""

    return ScannedFile(
        path=path,
        hash=file_hash,
        title=first("title"),
        artist=first("artist"),
        album=first("album"),
        date=first("date"),
        tracknumber=first("tracknumber"),
        duration_seconds=duration,
    ), ""


def find_mp3_files(root: str) -> list[str]:
    return [str(p) for p in Path(root).rglob("*.mp3")]
