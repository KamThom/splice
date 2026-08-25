"""Library-health checks. Embedded-art presence is the one check that needs a
file read per track (like loudness measurement), so it gets the same
background-QThread-sweep-with-cached-column treatment as
splice/library/loudness.py. The other dashboard categories (missing tags,
duplicates, missing files, orphaned files) are cheap enough to compute live
from the DB/filesystem in library_view.py and don't need a worker."""

from pathlib import Path

from mutagen.id3 import ID3, ID3NoHeaderError
from PyQt6.QtCore import QThread, pyqtSignal

from ..workers import ts
from . import db as libdb


def check_art(path: str) -> bool | None:
    """Read-only embedded-art check — deliberately raw mutagen.id3.ID3, never
    splice/tagging.py::read_tags/EasyID3, which writes an empty ID3 header as
    a side effect if the file has none (the same trap library/scanner.py
    already avoids for bulk operations over the whole library).
    ID3NoHeaderError means "no ID3v2 header at all," which definitively means
    no APIC frame -> False. None is reserved for genuine read failures
    (missing file, permission error, corrupt beyond parsing) so those rows
    stay NULL and get retried on the next sweep, mirroring
    loudness.measure_loudness's None-on-failure convention."""
    try:
        id3 = ID3(path)
    except ID3NoHeaderError:
        return False
    except Exception:
        return None
    return bool(id3.getall("APIC"))


class HealthScanWorker(QThread):
    """Checks embedded-art presence on every music track not yet checked
    (libdb.tracks_missing_art_check) — structural copy of
    LoudnessAnalysisWorker: same signals, same _cancel_requested flag, same
    log-and-continue-on-failure convention. A None result from check_art()
    logs and leaves that track's has_art NULL rather than flagging it either
    way, so it's retried on the next sweep instead of being silently wrong
    forever."""

    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)  # (tracks processed so far, total pending)
    finished_ok = pyqtSignal(int)  # count checked (True or False result)
    cancelled = pyqtSignal()

    def __init__(self, library_root: str):
        super().__init__()
        self.library_root = library_root
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True

    def run(self):
        conn = libdb.open_db(self.library_root)
        try:
            rows = libdb.tracks_missing_art_check(conn)
            total = len(rows)
            checked = 0
            for i, row in enumerate(rows, start=1):
                if self._cancel_requested:
                    self.cancelled.emit()
                    return
                self.progress.emit(i, total)
                abs_path = str(Path(self.library_root) / row["library_path"])
                result = check_art(abs_path)
                if result is None:
                    self.log.emit(f"[{ts()}] Could not check art: {row['library_path']}")
                    continue
                libdb.set_track_art_flag(conn, row["id"], result)
                checked += 1
        finally:
            conn.close()
        self.finished_ok.emit(checked)
