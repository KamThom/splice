"""Copies curated playlists and audiobook folders out of the Library onto an
external device (Shokz OpenSwim Pro — plain mass storage, folder-order
playback, no playlist support; or a future Rockbox-flashed iPod, same
constraints). Mirrors splice/library/migrate.py's QThread worker pattern."""

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from ..widgets import natural_sort_key
from ..workers import sanitize_filename, unique_path
from . import db as libdb


@dataclass
class ExportItem:
    kind: str  # "playlist" | "audiobook"
    key: object  # playlist_id (int) for "playlist"; path relative to Library/Audiobooks/, posix str, for "audiobook"
    name: str  # display name; sanitized to become the destination subfolder name


@dataclass
class ExportReport:
    items_exported: int = 0
    items_replaced: int = 0
    copied_count: int = 0
    skipped_count: int = 0  # source file missing on disk (stale DB row)
    skipped_files: list = field(default_factory=list)
    failed_count: int = 0
    failed_files: list = field(default_factory=list)  # (filename, error) tuples


@dataclass
class CopyReport:
    copied_count: int = 0
    failed_count: int = 0
    failed_files: list = field(default_factory=list)  # (filename, reason) tuples


class CopyTracksWorker(QThread):
    """Ad-hoc copy of arbitrary selected tracks straight into a destination
    folder, flat (no per-item subfolder) — unlike ExportWorker, which is for
    whole playlists/audiobook entries and gives each its own subfolder. This
    is for "just these few songs, right into this one folder."."""

    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished_ok = pyqtSignal(object)  # CopyReport
    cancelled = pyqtSignal()

    def __init__(self, library_root: str, library_paths: list[str], destination: str):
        super().__init__()
        self.library_root = library_root
        self.library_paths = library_paths
        self.destination = destination
        self._cancel_requested = False
        self.report = CopyReport()

    def cancel(self):
        self._cancel_requested = True

    def run(self):
        total = len(self.library_paths)
        self.log.emit(f"Copying {total} track(s)...")
        for i, rel_path in enumerate(self.library_paths, start=1):
            if self._cancel_requested:
                self.cancelled.emit()
                return
            self.progress.emit(int(i / total * 100) if total else 100)

            src = Path(self.library_root) / rel_path
            if not src.exists():
                self.report.failed_count += 1
                self.report.failed_files.append((rel_path, "missing from library"))
                self.log.emit(f"  Missing: {rel_path}")
                continue
            try:
                dest = unique_path(Path(self.destination) / src.name)
                shutil.copy2(src, dest)
                self.report.copied_count += 1
            except Exception as e:
                self.report.failed_count += 1
                self.report.failed_files.append((src.name, str(e)))
                self.log.emit(f"  FAILED {src.name}: {e}")

        self.log.emit(f"Copy complete: {self.report.copied_count} copied, {self.report.failed_count} failed.")
        self.finished_ok.emit(self.report)


def list_audiobook_entries(library_root: str) -> list[tuple[str, str]]:
    """(relative_path_posix, display_name) for every direct child — series
    folder or loose file — of Library/Audiobooks/, for the export checklist."""
    root = Path(library_root) / "Audiobooks"
    if not root.exists():
        return []
    return sorted(
        ((c.relative_to(root).as_posix(), c.name) for c in root.iterdir() if not c.name.startswith(".")),
        key=lambda pair: pair[1].lower(),
    )


class ExportWorker(QThread):
    """Copies each selected item (playlist or audiobook entry) into its own
    named subfolder under destination_root. Safe to cancel — already-copied
    items stay valid, nothing is left half-written mid-file (shutil.copy2 per
    file, not streamed)."""

    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished_ok = pyqtSignal(object)  # ExportReport
    cancelled = pyqtSignal()

    def __init__(self, library_root: str, destination_root: str, items: list[ExportItem], replace_existing: bool):
        super().__init__()
        self.library_root = library_root
        self.destination_root = destination_root
        self.items = items
        self.replace_existing = replace_existing
        self._cancel_requested = False
        self.report = ExportReport()

    def cancel(self):
        self._cancel_requested = True

    def run(self):
        conn = libdb.open_db(self.library_root)
        try:
            for item in self.items:
                if self._cancel_requested:
                    self.cancelled.emit()
                    return
                if item.kind == "playlist":
                    self._export_playlist(conn, item)
                else:
                    self._export_audiobook(item)
                if self._cancel_requested:
                    self.cancelled.emit()
                    return
                self.report.items_exported += 1
        finally:
            conn.close()

        r = self.report
        self.log.emit(
            f"Export complete: {r.items_exported} item(s), {r.copied_count} file(s) copied, "
            f"{r.skipped_count} missing, {r.failed_count} failed."
        )
        self.finished_ok.emit(r)

    def _dest_folder_for(self, name: str) -> Path:
        return Path(self.destination_root) / sanitize_filename(name)

    def _prepare_dest_folder(self, dest_folder: Path):
        # Only ever remove a folder that is a direct child of the chosen
        # destination root — never the root itself, never anything outside
        # it. dest_folder is always built from sanitize_filename() of an
        # item's own name via _dest_folder_for, never raw user text.
        if self.replace_existing and dest_folder.exists():
            assert dest_folder.parent == Path(self.destination_root)
            shutil.rmtree(dest_folder)
            self.report.items_replaced += 1
        dest_folder.mkdir(parents=True, exist_ok=True)

    def _export_playlist(self, conn, item: ExportItem):
        paths = libdb.playlist_track_paths_in_order(conn, item.key)
        dest_folder = self._dest_folder_for(item.name)
        self._prepare_dest_folder(dest_folder)
        total = len(paths)
        width = max(3, len(str(total)))
        self.log.emit(f"Exporting playlist '{item.name}' ({total} track(s))...")

        for i, rel_path in enumerate(paths, start=1):
            if self._cancel_requested:
                return
            self.progress.emit(int(i / total * 100) if total else 100)

            src = Path(self.library_root) / rel_path
            if not src.exists():
                self.report.skipped_count += 1
                self.report.skipped_files.append(rel_path)
                self.log.emit(f"  Missing (skipped): {rel_path}")
                continue

            title = Path(rel_path).stem
            dest_name = f"{i:0{width}d} - {sanitize_filename(title)}{src.suffix}"
            try:
                shutil.copy2(src, dest_folder / dest_name)
                self.report.copied_count += 1
            except Exception as e:
                self.report.failed_count += 1
                self.report.failed_files.append((dest_name, str(e)))
                self.log.emit(f"  FAILED {dest_name}: {e}")

    def _export_audiobook(self, item: ExportItem):
        src = Path(self.library_root) / "Audiobooks" / item.key
        dest_folder = self._dest_folder_for(item.name)
        self._prepare_dest_folder(dest_folder)

        if not src.exists():
            self.report.skipped_count += 1
            self.report.skipped_files.append(str(item.key))
            self.log.emit(f"  Missing (skipped): {item.key}")
            return

        if src.is_file():
            files = [src]
            rel_base = src.parent
        else:
            files = sorted(src.rglob("*.mp3"), key=lambda p: natural_sort_key(str(p)))
            rel_base = src

        total = len(files)
        self.log.emit(f"Exporting audiobook '{item.name}' ({total} file(s))...")
        for i, path in enumerate(files, start=1):
            if self._cancel_requested:
                return
            self.progress.emit(int(i / total * 100) if total else 100)
            try:
                dest_path = dest_folder / path.relative_to(rel_base)
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest_path)
                self.report.copied_count += 1
            except Exception as e:
                self.report.failed_count += 1
                self.report.failed_files.append((path.name, str(e)))
                self.log.emit(f"  FAILED {path.name}: {e}")
