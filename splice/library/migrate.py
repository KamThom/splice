"""Orchestrates scanning and building the library from user-chosen source
folders. Exposed as QThread workers (LibraryScanWorker read-only,
LibraryBuildWorker writes) following the same log/progress/cancelled signal
pattern as splice/workers.py, so the existing LevelGauge and cancel-button UI
drop in unchanged."""

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from ..widgets import natural_sort_key
from ..workers import ts, unique_path
from . import db as libdb
from . import naming
from .playlists import write_m3u8
from .scanner import find_mp3_files, read_scan_info


@dataclass
class MusicSource:
    folder: str
    playlist_name: str
    is_master: bool


@dataclass
class ScanReport:
    new_count: int = 0
    exact_duplicate_count: int = 0
    fuzzy_count: int = 0
    unsorted_count: int = 0
    unreadable_count: int = 0


@dataclass
class BuildReport:
    added_count: int = 0
    already_in_library_count: int = 0
    unreadable_count: int = 0
    unreadable_files: list = field(default_factory=list)
    failed_count: int = 0
    failed_files: list = field(default_factory=list)  # (filename, error) tuples


def _iter_all_sources(music_sources: list[MusicSource], audiobooks_source: str | None):
    for source in music_sources:
        for path in find_mp3_files(source.folder):
            yield path, "music"
    if audiobooks_source:
        for path in find_mp3_files(audiobooks_source):
            yield path, "audiobook"


def refile_track(library_root: str, track_id: int) -> bool:
    """Re-reads a track's tags straight off disk (e.g. right after the Tags
    module edited them) and, if that now resolves to a different destination,
    moves the file there and updates the index — closes the loop for the
    Unsorted tab's "Fix Tags" action so a track actually leaves _Unsorted once
    it's properly tagged, rather than just having its ID3 frames rewritten in
    place. Always refreshes the stored hash too, since editing tags changes
    the file's bytes. Also resets has_art back to NULL — the Health
    dashboard's art-presence flag is otherwise a write-once cache
    (HealthScanWorker only ever checks rows where it's still NULL), so
    without this, adding cover art through the Tags editor to a track
    already flagged "missing art" would leave it stuck showing as missing
    forever; resetting it here lets the next Health sweep re-verify for
    real instead of trusting stale state. Returns True if the file was
    physically moved."""
    conn = libdb.open_db(library_root)
    try:
        row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        if row is None:
            return False
        current_abs = Path(library_root) / row["library_path"]
        if not current_abs.exists():
            return False

        scanned, _ = read_scan_info(str(current_abs))
        if scanned is None:
            return False

        if row["kind"] == "music":
            dest_rel = Path("Music") / naming.music_destination(scanned)
        else:
            # Audiobooks are structure-preserving by design — retagging refreshes
            # the stored tag columns but never relocates them.
            dest_rel = Path(row["library_path"])

        dest_abs = Path(library_root) / dest_rel
        moved = dest_abs != current_abs

        if moved:
            dest_abs = unique_path(dest_abs)
            dest_abs.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(current_abs), str(dest_abs))
            try:
                current_abs.parent.rmdir()  # drop the now-empty old folder, if any
            except OSError:
                pass  # not empty (other tracks still there), or already gone — fine

        new_library_path = dest_abs.relative_to(library_root).as_posix()
        conn.execute(
            """UPDATE tracks SET hash = ?, library_path = ?, title = ?, artist = ?,
                                  album = ?, date = ?, tracknumber = ?, has_art = NULL WHERE id = ?""",
            (scanned.hash, new_library_path, scanned.title, scanned.artist, scanned.album,
             scanned.date, scanned.tracknumber, track_id),
        )
        conn.commit()
        return moved
    finally:
        conn.close()


def file_single_track(library_root: str, source_path: str, kind: str = "music") -> tuple[int | None, str]:
    """Reads, dedup-checks by hash, copies into Library/Music (or
    Library/Audiobooks), and indexes one already-known file. Returns
    (track_id, status) — status is "added", "already_in_library", or a
    human-readable reason the file couldn't be filed. Standalone, not tied to
    a QThread — used for filing a single fresh YouTube download straight into
    the library instead of requiring a full re-scan of its output folder."""
    scanned, reason = read_scan_info(source_path)
    if scanned is None:
        return None, reason

    conn = libdb.open_db(library_root)
    try:
        existing = libdb.find_by_hash(conn, scanned.hash)
        if existing is not None:
            return existing["id"], "already_in_library"

        if kind == "music":
            dest_rel = Path("Music") / naming.music_destination(scanned)
        else:
            dest_rel = Path("Audiobooks") / Path(source_path).name

        dest_abs = unique_path(Path(library_root) / dest_rel)
        dest_abs.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_path, dest_abs)
        library_path = dest_abs.relative_to(library_root).as_posix()

        # Look up fuzzy matches BEFORE inserting — otherwise the just-inserted
        # row matches its own title+artist and every track ends up flagged as
        # a duplicate of itself.
        fuzzy_matches = libdb.find_by_normalized_title_artist(conn, scanned.title, scanned.artist) if kind == "music" else []
        track_id = libdb.insert_track(
            conn, file_hash=scanned.hash, library_path=library_path, kind=kind,
            title=scanned.title, artist=scanned.artist, album=scanned.album,
            date=scanned.date, tracknumber=scanned.tracknumber,
            duration_seconds=scanned.duration_seconds,
        )
        for match in fuzzy_matches:
            libdb.add_fuzzy_dupe(conn, track_id, match["id"], "same normalized title+artist")
        return track_id, "added"
    finally:
        conn.close()


def adopt_orphan_file(library_root: str, abs_path: str, kind: str = "music") -> tuple[int | None, str]:
    """Indexes a file that's already sitting inside Library/Music or
    Library/Audiobooks but has no tracks row (the Health dashboard's
    "orphaned files" case) — reads/hashes/dedup-checks exactly like
    file_single_track, but never copies or relocates the file: it's already
    in the right place, only the index row is missing. Deliberately not a
    reuse of file_single_track, which always computes a fresh destination via
    naming.*_destination() and copies there — for a file already inside the
    library that would create a redundant duplicate copy instead of just
    adopting the original in place."""
    scanned, reason = read_scan_info(abs_path)
    if scanned is None:
        return None, reason

    conn = libdb.open_db(library_root)
    try:
        existing = libdb.find_by_hash(conn, scanned.hash)
        if existing is not None:
            return None, "already_in_library"

        library_path = Path(abs_path).relative_to(library_root).as_posix()
        fuzzy_matches = libdb.find_by_normalized_title_artist(conn, scanned.title, scanned.artist) if kind == "music" else []
        track_id = libdb.insert_track(
            conn, file_hash=scanned.hash, library_path=library_path, kind=kind,
            title=scanned.title, artist=scanned.artist, album=scanned.album,
            date=scanned.date, tracknumber=scanned.tracknumber,
            duration_seconds=scanned.duration_seconds,
        )
        for match in fuzzy_matches:
            libdb.add_fuzzy_dupe(conn, track_id, match["id"], "same normalized title+artist")
        return track_id, "added"
    finally:
        conn.close()


class AddFilesWorker(QThread):
    """Files a batch of externally-picked mp3s into the library — same
    per-file call (file_single_track) YouTube downloads and the Merge
    handoff already use, just looped over a user-picked file list. Adds
    every successfully-filed track to one playlist at the end (a single
    batched write_m3u8, not one per file) rather than per-file, since a
    whole picked batch commonly targets the same playlist."""

    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished_ok = pyqtSignal(int)  # count actually added (excludes already-in-library/failed)
    cancelled = pyqtSignal()

    def __init__(self, library_root: str, file_paths: list[str], kind: str, playlist_id: int | None = None):
        super().__init__()
        self.library_root = library_root
        self.file_paths = file_paths
        self.kind = kind
        self.playlist_id = playlist_id
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True

    def run(self):
        added_track_ids = []
        total = len(self.file_paths)
        for i, path in enumerate(self.file_paths, start=1):
            if self._cancel_requested:
                self.cancelled.emit()
                return
            self.progress.emit(int(i / total * 100) if total else 100)
            track_id, status = file_single_track(self.library_root, path, kind=self.kind)
            if track_id is None:
                self.log.emit(f"[{ts()}] Could not add {Path(path).name}: {status}")
                continue
            if status == "added":
                self.log.emit(f"[{ts()}] Added: {Path(path).name}")
                added_track_ids.append(track_id)
            else:
                self.log.emit(f"[{ts()}] Already in library: {Path(path).name}")

        if added_track_ids and self.kind == "music" and self.playlist_id is not None:
            conn = libdb.open_db(self.library_root)
            position = libdb.next_playlist_position(conn, self.playlist_id)
            for track_id in added_track_ids:
                libdb.add_playlist_track(conn, self.playlist_id, track_id, position)
                position += 1
            row = conn.execute("SELECT name FROM playlists WHERE id = ?", (self.playlist_id,)).fetchone()
            paths = libdb.playlist_track_paths_in_order(conn, self.playlist_id)
            conn.close()
            if row:
                write_m3u8(str(Path(self.library_root) / "Playlists"), row["name"], paths, self.library_root)
                self.log.emit(f"[{ts()}] Added {len(added_track_ids)} track(s) to playlist: {row['name']}")

        self.finished_ok.emit(len(added_track_ids))


class LibraryScanWorker(QThread):
    """Read-only: hashes + reads tags for every file across all sources against
    the existing index, and reports what a build would do. Writes nothing —
    safe to run as many times as you like."""

    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished_ok = pyqtSignal(object)  # ScanReport
    cancelled = pyqtSignal()

    def __init__(self, library_root: str, music_sources: list[MusicSource], audiobooks_source: str | None):
        super().__init__()
        self.library_root = library_root
        self.music_sources = music_sources
        self.audiobooks_source = audiobooks_source
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True

    def run(self):
        conn = libdb.open_db(self.library_root)
        report = ScanReport()

        all_files = list(_iter_all_sources(self.music_sources, self.audiobooks_source))
        total = len(all_files)
        self.log.emit(f"Scanning {total} file(s)...")

        for i, (path, kind) in enumerate(all_files, start=1):
            if self._cancel_requested:
                conn.close()
                self.cancelled.emit()
                return
            self.progress.emit(int(i / total * 100) if total else 100)

            try:
                scanned, reason = read_scan_info(path)
                if scanned is None:
                    report.unreadable_count += 1
                    self.log.emit(f"  Unreadable: {Path(path).name} — {reason}")
                    continue

                if libdb.find_by_hash(conn, scanned.hash) is not None:
                    report.exact_duplicate_count += 1
                    continue

                if kind == "music":
                    dest = naming.music_destination(scanned)
                    if str(dest).startswith("_Unsorted"):
                        report.unsorted_count += 1
                    else:
                        if libdb.find_by_normalized_title_artist(conn, scanned.title, scanned.artist):
                            report.fuzzy_count += 1
                        report.new_count += 1
                else:
                    report.new_count += 1
            except Exception as e:
                # A scan is a preview — one unreadable/unusual file must never
                # abort it and hide the rest of the report.
                report.unreadable_count += 1
                self.log.emit(f"  Skipped (error): {Path(path).name}: {e}")

        conn.close()
        self.log.emit(
            f"Scan complete: {report.new_count} new, {report.exact_duplicate_count} exact duplicates "
            f"(auto-skip), {report.fuzzy_count} fuzzy match(es) to review, {report.unsorted_count} "
            f"landing in _Unsorted, {report.unreadable_count} unreadable."
        )
        self.finished_ok.emit(report)


class LibraryBuildWorker(QThread):
    """Copies files into Library/Music or Library/Audiobooks, writes .m3u8
    playlists, and updates the index. Safe to cancel (partial progress is
    additive/valid, not corrupt) or re-run later as the collection grows —
    hash dedup means already-migrated files are skipped, never re-copied, and
    playlists are always regenerated in full from the database's current
    membership rather than patched incrementally."""

    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished_ok = pyqtSignal(object)  # BuildReport
    cancelled = pyqtSignal()

    def __init__(self, library_root: str, music_sources: list[MusicSource], audiobooks_source: str | None):
        super().__init__()
        self.library_root = library_root
        self.music_sources = music_sources
        self.audiobooks_source = audiobooks_source
        self._cancel_requested = False
        self.report = BuildReport()

    def cancel(self):
        self._cancel_requested = True

    def run(self):
        conn = libdb.open_db(self.library_root)

        for source in self.music_sources:
            self._build_music_source(conn, source)
            if self._cancel_requested:
                conn.close()
                self.cancelled.emit()
                return

        if self.audiobooks_source:
            self._build_audiobooks(conn)
            if self._cancel_requested:
                conn.close()
                self.cancelled.emit()
                return

        conn.close()
        r = self.report
        self.log.emit(
            f"Library build complete: {r.added_count} added, {r.already_in_library_count} already "
            f"in library, {r.unreadable_count} unreadable, {r.failed_count} failed."
        )
        if r.unreadable_files:
            shown = r.unreadable_files[:20]
            self.log.emit(f"  Unreadable: {', '.join(shown)}" + (" ..." if len(r.unreadable_files) > 20 else ""))
        if r.failed_files:
            for name, err in r.failed_files[:20]:
                self.log.emit(f"  Failed: {name} — {err}")
            if len(r.failed_files) > 20:
                self.log.emit(f"  ...and {len(r.failed_files) - 20} more failures — see the lines above for detail.")
        self.finished_ok.emit(self.report)

    def _build_music_source(self, conn, source: MusicSource):
        files = sorted(find_mp3_files(source.folder), key=natural_sort_key)
        total = len(files)
        self.log.emit(f"Processing '{source.playlist_name}' ({total} file(s))...")

        playlist_id = None if source.is_master else libdb.get_or_create_playlist(conn, source.playlist_name)
        next_position = libdb.next_playlist_position(conn, playlist_id) if playlist_id is not None else 0

        for i, path in enumerate(files, start=1):
            if self._cancel_requested:
                return
            self.progress.emit(int(i / total * 100) if total else 100)

            try:
                scanned, reason = read_scan_info(path)
                if scanned is None:
                    self.log.emit(f"  Skipped (unreadable): {Path(path).name} — {reason}")
                    self.report.unreadable_count += 1
                    self.report.unreadable_files.append(f"{Path(path).name} ({reason})")
                    continue

                existing = libdb.find_by_hash(conn, scanned.hash)
                if existing is not None:
                    track_id = existing["id"]
                    self.report.already_in_library_count += 1
                else:
                    dest_rel = Path("Music") / naming.music_destination(scanned)
                    dest_abs = unique_path(Path(self.library_root) / dest_rel)
                    dest_abs.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, dest_abs)
                    # Always store forward-slash paths, regardless of OS — the library
                    # (and its .splice/index.db) is meant to move between Windows and
                    # macOS on the same external drive, and pathlib parses "/" as a
                    # valid separator on both, but Windows-written "\" paths silently
                    # fail to split on macOS (and vice versa isn't an issue, since "/"
                    # is native there) — as_posix() keeps every stored path readable
                    # regardless of which machine wrote it.
                    library_path = dest_abs.relative_to(self.library_root).as_posix()

                    fuzzy_matches = libdb.find_by_normalized_title_artist(conn, scanned.title, scanned.artist)
                    track_id = libdb.insert_track(
                        conn, file_hash=scanned.hash, library_path=library_path,
                        kind="music", title=scanned.title, artist=scanned.artist, album=scanned.album,
                        date=scanned.date, tracknumber=scanned.tracknumber,
                        duration_seconds=scanned.duration_seconds,
                    )
                    for match in fuzzy_matches:
                        libdb.add_fuzzy_dupe(conn, track_id, match["id"], "same normalized title+artist")
                    self.log.emit(f"  Added: {library_path}")
                    self.report.added_count += 1
            except Exception as e:
                # One bad file (a weird filename, a permissions error, a locked
                # file) must never take down the whole build and lose all
                # progress on everything else — log it and keep going.
                self.log.emit(f"  FAILED {Path(path).name}: {e}")
                self.report.failed_count += 1
                self.report.failed_files.append((Path(path).name, str(e)))
                continue

            if playlist_id is not None:
                libdb.add_playlist_track(conn, playlist_id, track_id, next_position)
                next_position += 1

        if playlist_id is not None:
            paths = libdb.playlist_track_paths_in_order(conn, playlist_id)
            write_m3u8(str(Path(self.library_root) / "Playlists"), source.playlist_name, paths, self.library_root)
            self.log.emit(f"  Wrote playlist: {source.playlist_name}.m3u8 ({len(paths)} track(s))")

    def _build_audiobooks(self, conn):
        source_root = Path(self.audiobooks_source)
        files = sorted(find_mp3_files(self.audiobooks_source), key=natural_sort_key)
        total = len(files)
        self.log.emit(f"Processing audiobooks ({total} file(s))...")

        for i, path in enumerate(files, start=1):
            if self._cancel_requested:
                return
            self.progress.emit(int(i / total * 100) if total else 100)

            try:
                scanned, reason = read_scan_info(path)
                if scanned is None:
                    self.log.emit(f"  Skipped (unreadable): {Path(path).name} — {reason}")
                    self.report.unreadable_count += 1
                    self.report.unreadable_files.append(f"{Path(path).name} ({reason})")
                    continue

                if libdb.find_by_hash(conn, scanned.hash) is not None:
                    self.report.already_in_library_count += 1
                    continue  # already in the library

                dest_rel = Path("Audiobooks") / naming.audiobook_destination(source_root, Path(path))
                dest_abs = Path(self.library_root) / dest_rel
                dest_abs.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest_abs)

                libdb.insert_track(
                    conn, file_hash=scanned.hash, library_path=dest_rel.as_posix(),
                    kind="audiobook", title=scanned.title, artist=scanned.artist, album=scanned.album,
                    date=scanned.date, tracknumber=scanned.tracknumber,
                    duration_seconds=scanned.duration_seconds,
                )
                self.log.emit(f"  Added: {dest_rel}")
                self.report.added_count += 1
            except Exception as e:
                self.log.emit(f"  FAILED {Path(path).name}: {e}")
                self.report.failed_count += 1
                self.report.failed_files.append((Path(path).name, str(e)))
                continue
