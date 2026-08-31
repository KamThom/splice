"""The Library view — the app's home screen: browse/search Songs, Audiobooks,
Duplicates, and Unsorted, and the ADD SOURCES migration wizard that builds or
extends the library from existing folders."""

from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QSettings, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QIcon
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from . import tagging
from .library import db as libdb
from .library import loudness
from .library.export import (
    CopyReport,
    CopyTracksWorker,
    ExportItem,
    ExportReport,
    ExportWorker,
    list_audiobook_entries,
)
from .library.migrate import AddFilesWorker, LibraryBuildWorker, LibraryScanWorker, MusicSource, adopt_orphan_file
from .library.playlists import clear_playlist_artwork, find_playlist_artwork, set_playlist_artwork, write_m3u8
from .library.series_artwork import (
    clear_series_artwork,
    find_series_artwork,
    set_series_artwork,
)
from .styles import GREEN, MUTED, ORANGE, RED, STAT_AMBER, STAT_BLUE, STAT_PURPLE
from .widgets import (
    AudiobookShelfWidget,
    ChamferedArtTile,
    ChamferedFrame,
    LevelGauge,
    PixelLabel,
    TrackListWidget,
    TrackRowDelegate,
    render_chamfered_pixmap,
    render_mosaic_bytes,
)
from .workers import sanitize_filename


def choose_multiple_directories(parent, caption: str, start_dir: str) -> list[str]:
    """Native folder pickers don't support multi-select on any platform, so this
    uses Qt's own (non-native) dialog and turns on multi-selection in its
    internal file-system view — a standard workaround for picking several
    playlist folders at once instead of one add-click per folder."""
    dialog = QFileDialog(parent, caption, start_dir)
    dialog.setFileMode(QFileDialog.FileMode.Directory)
    dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
    dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
    for view in dialog.findChildren((QListView, QTreeView)):
        view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    if dialog.exec() != QDialog.DialogCode.Accepted:
        return []
    return dialog.selectedFiles()


def _is_unsorted(library_path: str) -> bool:
    return "_Unsorted" in Path(library_path).parts


def _try_unlink(path: Path) -> str | None:
    """Attempts to delete a library file, returning None on success
    (including "already gone" — missing_ok=True) or a short reason string
    on failure. A file can be transiently locked by another process on
    Windows (a search indexer or antivirus scan mid-sweep, a leftover
    mutagen/media handle) — that must never be allowed to escape as an
    uncaught PermissionError and crash the whole app the way it did before
    this existed; callers skip that one file and keep going instead."""
    try:
        path.unlink(missing_ok=True)
        return None
    except OSError as e:
        return str(e)


def _collect_cover_art(library_root: str, ordered_paths: list[str], limit: int = 4) -> list[bytes]:
    """Reads embedded art off the first `limit` tracks (in playlist order)
    that actually have any, for building a playlist's auto-mosaic fallback.
    Stops as soon as `limit` covers are found — a playlist can have hundreds
    of tracks and this only ever needs a handful for a 2x2 mosaic."""
    covers = []
    for rel_path in ordered_paths:
        if len(covers) >= limit:
            break
        try:
            art = tagging.read_tags(str(Path(library_root) / rel_path)).get("art")
        except Exception:
            art = None
        if art:
            covers.append(art)
    return covers


def playlist_mosaic_art(library_root: str, ordered_paths: list[str]) -> bytes | None:
    """Auto-generated fallback artwork for a playlist with no explicit
    artwork set yet — a mosaic of its first few tracks' own covers. Callers
    should cache the result keyed on (playlist_id, ordered_paths[:4]) —
    this itself does no caching, since it has no notion of "this playlist"
    across calls, only "these paths right now"."""
    return render_mosaic_bytes(_collect_cover_art(library_root, ordered_paths))


def make_track_list() -> TrackListWidget:
    """Read-only card-style track list, shared by the Library view's Songs
    and Audiobooks tabs. Each row is painted by TrackRowDelegate (title
    bold, artist muted underneath, a playlist-count badge) rather than
    Qt's default single-line item — setMouseTracking is required for the
    delegate's hover state to update as the mouse moves, not just on click.
    TrackListWidget (rather than a plain QListWidget) is what lets clicking
    directly on the badge pill emit badgeClicked instead of just selecting
    the row."""
    list_widget = TrackListWidget()
    list_widget.setItemDelegate(TrackRowDelegate(list_widget))
    list_widget.setMouseTracking(True)
    list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    list_widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    return list_widget


def populate_track_list(list_widget: QListWidget, tracks, membership: dict):
    list_widget.clear()
    for t in tracks:
        item = QListWidgetItem()
        count = membership.get(t["id"], 0)
        item.setData(Qt.ItemDataRole.UserRole, {
            "id": t["id"],
            "library_path": t["library_path"],
            "title": t["title"] or Path(t["library_path"]).stem,
            "artist": t["artist"] or "",
            "badge": str(count) if count else None,
            "loudness_lufs": t["loudness_lufs"],
            "loudness_true_peak": t["loudness_true_peak"],
        })
        list_widget.addItem(item)


def selected_library_paths(list_widget: QListWidget) -> list[str]:
    """library_path for each selected row — used by "COPY SELECTED..." on
    the Songs/Audiobooks tabs and by PlaylistView."""
    paths = []
    for item in list_widget.selectedItems():
        data = item.data(Qt.ItemDataRole.UserRole)
        if data:
            paths.append(data["library_path"])
    return paths


SORT_FIELDS = ["Title", "Artist", "Date Added", "In"]

# Per-track sort keys that don't need anything beyond the row itself. "In"
# isn't here — it needs the membership-count dict, not just the row, so
# sort_tracks() handles it as a special case instead.
_SORT_KEY_FUNCS = {
    "Title": lambda t: (t["title"] or Path(t["library_path"]).stem).lower(),
    "Artist": lambda t: (t["artist"] or "").lower(),
    "Date Added": lambda t: t["added_at"] or "",
}


def sort_tracks(tracks, membership: dict, field: str, ascending: bool):
    if field == "In":
        key = lambda t: membership.get(t["id"], 0)
    else:
        key = _SORT_KEY_FUNCS.get(field, _SORT_KEY_FUNCS["Title"])
    return sorted(tracks, key=key, reverse=not ascending)


HEALTH_CATEGORIES = ["Duplicates", "Missing Tags", "Missing Art", "Missing Files", "Orphaned Files"]
HEALTH_TAB_INDEX = 4


def _find_missing_tags(tracks):
    """Filed (non-Unsorted) music tracks missing title, artist, or album.
    None of these can be blank at filing time (naming.music_destination
    routes any blank one to _Unsorted instead) — but an in-place tag edit
    on an already-filed track can still clear one without moving the file
    back to _Unsorted, which is the gap this actually catches."""
    return [
        t for t in tracks
        if not (t["title"] or "").strip() or not (t["artist"] or "").strip() or not (t["album"] or "").strip()
    ]


def _find_missing_files(library_root: str, tracks) -> list:
    """Indexed tracks whose file no longer exists on disk — covers "broken
    playlist references" too, since a playlist only ever references a track
    row, not a file directly."""
    return [t for t in tracks if not (Path(library_root) / t["library_path"]).exists()]


def _find_orphaned_files(library_root: str, known_paths: set) -> list[tuple[str, str]]:
    """Real files under Library/Music or Library/Audiobooks with no tracks
    row — returns (library_path, kind) pairs."""
    orphans = []
    for subdir, kind in (("Music", "music"), ("Audiobooks", "audiobook")):
        base = Path(library_root) / subdir
        if not base.exists():
            continue
        for p in base.rglob("*.mp3"):
            rel = p.relative_to(library_root).as_posix()
            if rel not in known_paths:
                orphans.append((rel, kind))
    return orphans


class LibraryMigrationDialog(QDialog):
    """ADD SOURCES wizard: pick music source folders (each named, one flagged as
    master) and an optional audiobooks folder, SCAN (read-only preview), then
    BUILD LIBRARY. Reusable for first-run setup and for migrating more folders
    in later as the collection grows."""

    def __init__(self, parent, library_root: str):
        super().__init__(parent)
        self.setWindowTitle("Add Library Sources")
        self.resize(600, 560)
        self.library_root = library_root
        self.build_succeeded = False
        self._audiobooks_folder = None

        layout = QVBoxLayout(self)

        hint = QLabel(
            "Add folders to scan into your library. Mark exactly one music folder as "
            "your master collection (e.g. \"Liked\") — everything else becomes a "
            "playlist referencing into the shared library. Double-click a name to "
            "rename it (that's the playlist name it'll get)."
        )
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        layout.addWidget(hint)

        self.sources_list = QListWidget()
        self.sources_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        layout.addWidget(self.sources_list)

        source_btn_row = QHBoxLayout()
        add_music_btn = QPushButton("+ ADD MUSIC PLAYLIST FOLDER(S)")
        add_music_btn.clicked.connect(self._add_music_source)
        remove_btn = QPushButton("REMOVE SELECTED")
        remove_btn.clicked.connect(self._remove_selected_source)
        master_btn = QPushButton("SET AS MASTER")
        master_btn.clicked.connect(self._set_selected_as_master)
        source_btn_row.addWidget(add_music_btn)
        source_btn_row.addWidget(remove_btn)
        source_btn_row.addWidget(master_btn)
        layout.addLayout(source_btn_row)

        audiobooks_row = QHBoxLayout()
        self.audiobooks_label = QLabel("Audiobooks folder: (none)")
        self.audiobooks_label.setObjectName("muted")
        audiobooks_btn = QPushButton("SET AUDIOBOOKS FOLDER")
        audiobooks_btn.clicked.connect(self._choose_audiobooks_folder)
        audiobooks_row.addWidget(self.audiobooks_label, stretch=1)
        audiobooks_row.addWidget(audiobooks_btn)
        layout.addLayout(audiobooks_row)

        action_row = QHBoxLayout()
        self.scan_btn = QPushButton("SCAN")
        self.scan_btn.clicked.connect(self._run_scan)
        self.build_btn = QPushButton("BUILD LIBRARY")
        self.build_btn.setObjectName("primary")
        self.build_btn.setEnabled(False)
        self.build_btn.clicked.connect(self._run_build)
        action_row.addWidget(self.scan_btn)
        action_row.addStretch(1)
        action_row.addWidget(self.build_btn)
        layout.addLayout(action_row)

        self.gauge = LevelGauge()
        layout.addWidget(self.gauge)

        self.report_label = QLabel("Scan is read-only — nothing is copied until you click BUILD LIBRARY.")
        self.report_label.setObjectName("muted")
        self.report_label.setWordWrap(True)
        layout.addWidget(self.report_label)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        layout.addWidget(self.log_box, stretch=1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("CLOSE")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

    def _log(self, msg: str):
        self.log_box.appendPlainText(msg)

    def _add_music_source(self):
        # Ctrl/Shift-click to select several playlist folders at once, rather
        # than one add-click per folder.
        folders = choose_multiple_directories(self, "Choose playlist folder(s) — ctrl/shift-click for several", str(Path.home()))
        existing = {self.sources_list.item(i).data(Qt.ItemDataRole.UserRole)["folder"]
                    for i in range(self.sources_list.count())}
        for folder in folders:
            if folder in existing:
                continue
            item = QListWidgetItem(Path(folder).name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            item.setData(Qt.ItemDataRole.UserRole, {"folder": folder, "is_master": False})
            self.sources_list.addItem(item)

    def _remove_selected_source(self):
        for item in self.sources_list.selectedItems():
            self.sources_list.takeItem(self.sources_list.row(item))

    def _set_selected_as_master(self):
        selected = self.sources_list.selectedItems()
        if not selected:
            return
        for i in range(self.sources_list.count()):
            item = self.sources_list.item(i)
            data = item.data(Qt.ItemDataRole.UserRole)
            data["is_master"] = item is selected[0]
            item.setData(Qt.ItemDataRole.UserRole, data)
            font = item.font()
            font.setBold(data["is_master"])
            item.setFont(font)
            item.setText(item.text().removesuffix(" (master)"))
            if data["is_master"]:
                item.setText(item.text() + " (master)")

    def _choose_audiobooks_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose your audiobooks folder", str(Path.home()))
        if not folder:
            return
        self._audiobooks_folder = folder
        self.audiobooks_label.setText(f"Audiobooks folder: {folder}")

    def _collect_music_sources(self) -> list[MusicSource]:
        sources = []
        for i in range(self.sources_list.count()):
            item = self.sources_list.item(i)
            data = item.data(Qt.ItemDataRole.UserRole)
            name = item.text().removesuffix(" (master)").strip() or Path(data["folder"]).name
            sources.append(MusicSource(folder=data["folder"], playlist_name=name, is_master=data["is_master"]))
        return sources

    def _run_scan(self):
        sources = self._collect_music_sources()
        if not sources and not self._audiobooks_folder:
            QMessageBox.warning(self, "Nothing to scan", "Add at least one music folder or an audiobooks folder.")
            return
        self.scan_btn.setEnabled(False)
        self.build_btn.setEnabled(False)
        self.gauge.set_idle()
        self.report_label.setText("Scanning...")
        self._scan_worker = LibraryScanWorker(self.library_root, sources, self._audiobooks_folder)
        self._scan_worker.log.connect(self._log)
        self._scan_worker.progress.connect(self.gauge.set_progress)
        self._scan_worker.finished_ok.connect(self._on_scan_done)
        self._scan_worker.start()

    def _on_scan_done(self, report):
        self.gauge.set_done("SCAN COMPLETE")
        self.scan_btn.setEnabled(True)
        self.build_btn.setEnabled(report.new_count > 0)
        self.report_label.setText(
            f"{report.new_count} new track(s) to add · {report.exact_duplicate_count} exact "
            f"duplicate(s) (auto-skip) · {report.fuzzy_count} fuzzy match(es) to review · "
            f"{report.unsorted_count} landing in _Unsorted · {report.unreadable_count} unreadable"
        )

    def _run_build(self):
        sources = self._collect_music_sources()
        self.scan_btn.setEnabled(False)
        self.build_btn.setEnabled(False)
        self.gauge.set_idle()
        self._build_worker = LibraryBuildWorker(self.library_root, sources, self._audiobooks_folder)
        self._build_worker.log.connect(self._log)
        self._build_worker.progress.connect(self.gauge.set_progress)
        self._build_worker.finished_ok.connect(self._on_build_done)
        self._build_worker.start()

    def _on_build_done(self, report):
        self.gauge.set_done("BUILD COMPLETE")
        self.build_succeeded = True
        self.scan_btn.setEnabled(True)
        text = (
            f"Library updated: {report.added_count} added, {report.already_in_library_count} "
            f"already in library, {report.unreadable_count} unreadable, {report.failed_count} failed."
        )
        if report.unreadable_count or report.failed_count:
            text += " See the log below for which files and why."
        self.report_label.setText(text)


class ExportDialog(QDialog):
    """Copies selected playlists / audiobook entries out of the library onto
    an external device (or a staging folder for something like iTunes to
    import from). Opened either from LibraryView (nothing preselected) or
    from PlaylistView (that one playlist preselected)."""

    def __init__(self, parent, library_root: str, preselected_playlist_id: int | None = None):
        super().__init__(parent)
        self.library_root = library_root
        self.settings = QSettings("Thomas", "Splice")

        title = "Export"
        preselect_row = None

        self.resize(560, 560)
        layout = QVBoxLayout(self)

        hint = QLabel(
            "Check the playlists and/or audiobooks you want to copy out. Each one lands "
            "in its own folder at the destination, in the right play order — for devices "
            "like the Shokz that have no playlist support and just play files in folder order."
        )
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        layout.addWidget(hint)

        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget, stretch=1)

        conn = libdb.open_db(library_root)
        playlists = libdb.list_playlists(conn)
        conn.close()
        audiobooks = list_audiobook_entries(library_root)

        if playlists:
            self._add_header("PLAYLISTS")
            for p in playlists:
                item = self._add_checkable(p["name"], ExportItem("playlist", p["id"], p["name"]))
                if preselected_playlist_id is not None and p["id"] == preselected_playlist_id:
                    preselect_row = item
                    title = f"Export — {p['name']}"

        if audiobooks:
            self._add_header("AUDIOBOOKS")
            for rel_path, name in audiobooks:
                self._add_checkable(name, ExportItem("audiobook", rel_path, name))

        if preselect_row is not None:
            preselect_row.setCheckState(Qt.CheckState.Checked)

        self.setWindowTitle(title)

        dest_row = QHBoxLayout()
        self.dest_edit = QLineEdit()
        self.dest_edit.setReadOnly(True)
        self.dest_edit.setText(str(self.settings.value("export_destination", "") or ""))
        dest_btn = QPushButton("CHOOSE...")
        dest_btn.clicked.connect(self._choose_destination)
        dest_row.addWidget(self.dest_edit, stretch=1)
        dest_row.addWidget(dest_btn)
        layout.addLayout(dest_row)

        self.replace_check = QCheckBox("Replace existing contents of each selected item's destination folder")
        layout.addWidget(self.replace_check)
        replace_hint = QLabel("Only touches that one subfolder per item — never anything else at the destination.")
        replace_hint.setObjectName("muted")
        replace_hint.setWordWrap(True)
        layout.addWidget(replace_hint)

        action_row = QHBoxLayout()
        self.export_btn = QPushButton("EXPORT")
        self.export_btn.setObjectName("primary")
        self.export_btn.clicked.connect(self._run_export)
        self.cancel_btn = QPushButton("CANCEL")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._cancel_export)
        action_row.addWidget(self.export_btn)
        action_row.addStretch(1)
        action_row.addWidget(self.cancel_btn)
        layout.addLayout(action_row)

        self.gauge = LevelGauge()
        layout.addWidget(self.gauge)

        self.report_label = QLabel("")
        self.report_label.setObjectName("muted")
        self.report_label.setWordWrap(True)
        layout.addWidget(self.report_label)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        layout.addWidget(self.log_box, stretch=1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("CLOSE")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

    def _add_header(self, text: str):
        item = QListWidgetItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        font = item.font()
        font.setBold(True)
        item.setFont(font)
        self.list_widget.addItem(item)

    def _add_checkable(self, label: str, export_item: ExportItem) -> QListWidgetItem:
        item = QListWidgetItem(f"    {label}")
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Unchecked)
        item.setData(Qt.ItemDataRole.UserRole, export_item)
        self.list_widget.addItem(item)
        return item

    def _choose_destination(self):
        start_dir = self.dest_edit.text() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose export destination", start_dir)
        if chosen:
            self.dest_edit.setText(chosen)

    def _log(self, msg: str):
        self.log_box.appendPlainText(msg)

    def _collect_selected_items(self) -> list[ExportItem]:
        items = []
        for i in range(self.list_widget.count()):
            row = self.list_widget.item(i)
            if row.flags() & Qt.ItemFlag.ItemIsUserCheckable and row.checkState() == Qt.CheckState.Checked:
                items.append(row.data(Qt.ItemDataRole.UserRole))
        return items

    def _run_export(self):
        items = self._collect_selected_items()
        if not items:
            QMessageBox.warning(self, "Nothing selected", "Check at least one playlist or audiobook to export.")
            return

        destination = self.dest_edit.text().strip()
        if not destination or not Path(destination).exists():
            QMessageBox.warning(self, "No destination", "Choose a destination folder first.")
            return

        seen_names = {}
        for item in items:
            sanitized = sanitize_filename(item.name)
            if sanitized in seen_names:
                QMessageBox.warning(
                    self, "Naming collision",
                    f"'{item.name}' and '{seen_names[sanitized]}' would both export to the folder "
                    f"'{sanitized}' — rename one or deselect it.",
                )
                return
            seen_names[sanitized] = item.name

        self.settings.setValue("export_destination", destination)

        self.export_btn.setEnabled(False)
        self.list_widget.setEnabled(False)
        self.cancel_btn.setEnabled(True)
        self.gauge.set_idle()
        self.report_label.setText("")

        self._worker = ExportWorker(self.library_root, destination, items, self.replace_check.isChecked())
        self._worker.log.connect(self._log)
        self._worker.progress.connect(self.gauge.set_progress)
        self._worker.finished_ok.connect(self._on_export_done)
        self._worker.cancelled.connect(self._on_export_cancelled)
        self._worker.start()

    def _cancel_export(self):
        if hasattr(self, "_worker"):
            self._worker.cancel()

    def _on_export_done(self, report: ExportReport):
        self.gauge.set_done("EXPORT COMPLETE")
        self.export_btn.setEnabled(True)
        self.list_widget.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        text = (
            f"{report.items_exported} item(s) exported ({report.items_replaced} replaced), "
            f"{report.copied_count} file(s) copied, {report.skipped_count} missing, "
            f"{report.failed_count} failed."
        )
        if report.skipped_count or report.failed_count:
            text += " See the log below for detail."
        self.report_label.setText(text)

    def _on_export_cancelled(self):
        self.gauge.set_cancelled("CANCELLED")
        self.export_btn.setEnabled(True)
        self.list_widget.setEnabled(True)
        self.cancel_btn.setEnabled(False)
        self.report_label.setText("Export cancelled — items already copied before cancelling are kept.")


class CopyTracksDialog(QDialog):
    """Ad-hoc copy of whatever tracks were multi-selected in the Songs/
    Audiobooks table into a folder the user picks — flat, no per-item
    subfolder (that's what the full Export dialog is for)."""

    def __init__(self, parent, library_root: str, library_paths: list[str]):
        super().__init__(parent)
        self.library_root = library_root
        self.library_paths = library_paths
        self.setWindowTitle(f"Copy {len(library_paths)} Track(s)")
        self.resize(480, 380)

        layout = QVBoxLayout(self)
        hint = QLabel(f"Copying {len(library_paths)} track(s) as flat files into a folder you choose.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        dest_row = QHBoxLayout()
        self.dest_edit = QLineEdit()
        self.dest_edit.setReadOnly(True)
        dest_btn = QPushButton("CHOOSE...")
        dest_btn.clicked.connect(self._choose_destination)
        dest_row.addWidget(self.dest_edit, stretch=1)
        dest_row.addWidget(dest_btn)
        layout.addLayout(dest_row)

        action_row = QHBoxLayout()
        self.copy_btn = QPushButton("COPY")
        self.copy_btn.setObjectName("primary")
        self.copy_btn.clicked.connect(self._run_copy)
        action_row.addWidget(self.copy_btn)
        layout.addLayout(action_row)

        self.gauge = LevelGauge()
        layout.addWidget(self.gauge)

        self.report_label = QLabel("")
        self.report_label.setObjectName("muted")
        self.report_label.setWordWrap(True)
        layout.addWidget(self.report_label)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        layout.addWidget(self.log_box, stretch=1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("CLOSE")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

    def _choose_destination(self):
        chosen = QFileDialog.getExistingDirectory(self, "Choose destination folder", str(Path.home()))
        if chosen:
            self.dest_edit.setText(chosen)

    def _log(self, msg: str):
        self.log_box.appendPlainText(msg)

    def _run_copy(self):
        destination = self.dest_edit.text().strip()
        if not destination or not Path(destination).exists():
            QMessageBox.warning(self, "No destination", "Choose a destination folder first.")
            return
        self.copy_btn.setEnabled(False)
        self.gauge.set_idle()
        self.report_label.setText("")
        self._worker = CopyTracksWorker(self.library_root, self.library_paths, destination)
        self._worker.log.connect(self._log)
        self._worker.progress.connect(self.gauge.set_progress)
        self._worker.finished_ok.connect(self._on_done)
        self._worker.start()

    def _on_done(self, report: CopyReport):
        self.gauge.set_done("COPY COMPLETE")
        self.copy_btn.setEnabled(True)
        text = f"{report.copied_count} copied, {report.failed_count} failed."
        if report.failed_count:
            text += " See the log below for detail."
        self.report_label.setText(text)


class AddFilesDialog(QDialog):
    """Files chosen mp3s directly into the library — Music or Audiobook,
    optionally into a playlist — without requiring a full Add Sources folder
    scan. Reuses migrate.py's AddFilesWorker (same file_single_track call
    YouTube downloads and the Merge handoff already use)."""

    def __init__(self, parent, library_root: str, default_playlist_id: int | None = None, lock_to_music: bool = False):
        super().__init__(parent)
        self.library_root = library_root
        self.lock_to_music = lock_to_music
        self.added_any = False
        self.setWindowTitle("Add Files")
        self.resize(520, 480)

        layout = QVBoxLayout(self)
        hint = QLabel("Add mp3 files directly to your library — no folder scan needed.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.file_list = QListWidget()
        self.file_list.setObjectName("filePicker")
        self.file_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(self.file_list, stretch=1)

        file_btn_row = QHBoxLayout()
        choose_btn = QPushButton("+ CHOOSE FILES...")
        choose_btn.clicked.connect(self._choose_files)
        remove_btn = QPushButton("REMOVE SELECTED")
        remove_btn.clicked.connect(self._remove_selected_files)
        file_btn_row.addWidget(choose_btn)
        file_btn_row.addWidget(remove_btn)
        file_btn_row.addStretch(1)
        layout.addLayout(file_btn_row)

        dest_row = QHBoxLayout()
        self.dest_label = QLabel("File as:")
        self.dest_label.setObjectName("muted")
        dest_row.addWidget(self.dest_label)
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("Music (library)", "music")
        self.kind_combo.addItem("Audiobook (library)", "audiobook")
        self.kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        dest_row.addWidget(self.kind_combo)
        if lock_to_music:
            # Playlists are a music-only concept everywhere else in this app —
            # no point offering a kind choice when the caller already knows
            # the answer is always "music".
            self.dest_label.setVisible(False)
            self.kind_combo.setVisible(False)

        playlist_label = QLabel("Add to:")
        playlist_label.setObjectName("muted")
        dest_row.addWidget(playlist_label)
        self.playlist_combo = QComboBox()
        self._refresh_playlist_combo()
        if default_playlist_id is not None:
            idx = self.playlist_combo.findData(default_playlist_id)
            if idx >= 0:
                self.playlist_combo.setCurrentIndex(idx)
        self.playlist_combo.currentIndexChanged.connect(self._on_playlist_combo_changed)
        dest_row.addWidget(self.playlist_combo)
        layout.addLayout(dest_row)

        action_row = QHBoxLayout()
        self.add_btn = QPushButton("ADD")
        self.add_btn.setObjectName("primary")
        self.add_btn.clicked.connect(self._run_add)
        action_row.addWidget(self.add_btn)
        layout.addLayout(action_row)

        self.gauge = LevelGauge()
        layout.addWidget(self.gauge)

        self.report_label = QLabel("")
        self.report_label.setObjectName("muted")
        self.report_label.setWordWrap(True)
        layout.addWidget(self.report_label)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        layout.addWidget(self.log_box, stretch=1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("CLOSE")
        close_btn.clicked.connect(self.accept)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

    def _choose_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Choose mp3 files to add", str(Path.home()), "Audio files (*.mp3)"
        )
        existing = {self.file_list.item(i).text() for i in range(self.file_list.count())}
        for path in paths:
            if path not in existing:
                self.file_list.addItem(path)

    def _remove_selected_files(self):
        for item in self.file_list.selectedItems():
            self.file_list.takeItem(self.file_list.row(item))

    def _on_kind_changed(self, index: int):
        is_music = self.kind_combo.itemData(index) == "music"
        self.playlist_combo.setEnabled(is_music)
        if not is_music:
            self.playlist_combo.setCurrentIndex(0)

    def _refresh_playlist_combo(self):
        previous = self.playlist_combo.currentData() if self.playlist_combo.count() else None
        self.playlist_combo.blockSignals(True)
        self.playlist_combo.clear()
        self.playlist_combo.addItem("No playlist (library only)", None)
        conn = libdb.open_db(self.library_root)
        for p in libdb.list_playlists(conn):
            self.playlist_combo.addItem(p["name"], p["id"])
        conn.close()
        self.playlist_combo.addItem("+ New Playlist...", "__new__")
        idx = self.playlist_combo.findData(previous)
        self.playlist_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.playlist_combo.blockSignals(False)

    def _on_playlist_combo_changed(self, index: int):
        if self.playlist_combo.itemData(index) != "__new__":
            return
        name, ok = QInputDialog.getText(self, "New Playlist", "Playlist name:")
        if ok and name.strip():
            conn = libdb.open_db(self.library_root)
            playlist_id = libdb.get_or_create_playlist(conn, name.strip())
            conn.close()
            self._refresh_playlist_combo()
            idx = self.playlist_combo.findData(playlist_id)
            self.playlist_combo.setCurrentIndex(idx if idx >= 0 else 0)
        else:
            self.playlist_combo.setCurrentIndex(0)

    def _run_add(self):
        paths = [self.file_list.item(i).text() for i in range(self.file_list.count())]
        if not paths:
            QMessageBox.information(self, "No files", "Choose one or more mp3 files first.")
            return
        self.add_btn.setEnabled(False)
        self.gauge.set_idle()
        self.report_label.setText("")
        kind = "music" if self.lock_to_music else self.kind_combo.currentData()
        playlist_id = self.playlist_combo.currentData() if kind == "music" else None
        self._worker = AddFilesWorker(self.library_root, paths, kind, playlist_id)
        self._worker.log.connect(self._log)
        self._worker.progress.connect(self.gauge.set_progress)
        self._worker.finished_ok.connect(self._on_add_done)
        self._worker.start()

    def _log(self, msg: str):
        self.log_box.appendPlainText(msg)

    def _on_add_done(self, added_count: int):
        self.gauge.set_done("ADD COMPLETE")
        self.add_btn.setEnabled(True)
        self.report_label.setText(f"{added_count} of {self.file_list.count()} file(s) added.")
        if added_count:
            self.added_any = True
        self.file_list.clear()


def _format_duration(seconds) -> str:
    if not seconds:
        return "—"
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def _parse_book_number(tracknumber) -> int | None:
    """Parses a Track # tag value ("2", "2/6", "", None) into a book's
    position within its series, the same way naming.music_destination
    already parses tracknumber for its own filename-prefix purposes.
    Returns None when unparseable so the caller can fall back to title
    sort instead of a SQL text-sort (which would put "10" before "2")."""
    if not tracknumber:
        return None
    try:
        return int(str(tracknumber).split("/")[0].strip())
    except ValueError:
        return None


def sort_series_books(tracks: list) -> list:
    """Sorts audiobook rows within a series by parsed book number, falling
    back to title for anything unparseable (pushed to the end, in title
    order, rather than interleaved by parse-failure)."""
    def key(t):
        n = _parse_book_number(t["tracknumber"])
        return (0, n) if n is not None else (1, (t["title"] or "").lower())
    return sorted(tracks, key=key)


def _format_filesize(path: Path) -> str:
    try:
        size_mb = path.stat().st_size / (1024 * 1024)
    except OSError:
        return "—"
    return f"{size_mb:.1f} MB"


def _format_total_playtime(seconds: float) -> str:
    """Formats an aggregate duration (e.g. the whole library's playtime) as
    the coarsest two units that fit — d/h for a large library, h/m or just m
    for a small one. Distinct from _format_duration, which is M:SS for one
    single track."""
    total_minutes = int(seconds // 60)
    hours, minutes = divmod(total_minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


class ResolveDuplicateDialog(QDialog):
    """Shows two duplicate-flagged tracks side by side — title/artist/album,
    duration, and live file size (the two cheap signals that actually help
    judge "which copy is better", e.g. a larger file usually means a higher
    bitrate rip) — and lets the user pick which one to keep. Returns the
    decision via self.keep_id/self.remove_id after exec() accepts; does no
    deletion itself, that's the caller's job."""

    def __init__(self, parent, library_root: str, track_a, track_b):
        super().__init__(parent)
        self.keep_id = None
        self.remove_id = None
        self._library_root = library_root
        self.setWindowTitle("Resolve Duplicate")
        self.resize(560, 260)

        layout = QVBoxLayout(self)
        hint = QLabel("These look like the same track. Pick which copy to keep — the other is "
                      "permanently deleted from your library (originals elsewhere are never touched).")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        columns = QHBoxLayout()
        columns.addLayout(self._build_column(track_a, track_b["id"]))
        columns.addLayout(self._build_column(track_b, track_a["id"]))
        layout.addLayout(columns)

        cancel_row = QHBoxLayout()
        cancel_row.addStretch(1)
        cancel_btn = QPushButton("CANCEL")
        cancel_btn.clicked.connect(self.reject)
        cancel_row.addWidget(cancel_btn)
        layout.addLayout(cancel_row)

    def _build_column(self, track, other_track_id: int) -> QVBoxLayout:
        col = QVBoxLayout()
        full_path = Path(self._library_root) / track["library_path"]
        for text, obj_name in [
            (track["title"] or "(no title)", "panelTitle"),
            (track["artist"] or "Unknown Artist", "muted"),
            (track["album"] or "", "muted"),
            (f"{_format_duration(track['duration_seconds'])}  •  {_format_filesize(full_path)}", "muted"),
            (track["library_path"], "muted"),
        ]:
            label = QLabel(text)
            label.setObjectName(obj_name)
            label.setWordWrap(True)
            col.addWidget(label)
        col.addStretch(1)
        keep_btn = QPushButton("KEEP THIS ONE")
        keep_btn.setObjectName("primary")
        keep_btn.clicked.connect(lambda: self._choose(track["id"], other_track_id))
        col.addWidget(keep_btn)
        return col

    def _choose(self, keep_id: int, remove_id: int):
        self.keep_id = keep_id
        self.remove_id = remove_id
        self.accept()


def _format_added_date(added_at: str | None) -> str:
    if not added_at:
        return "—"
    try:
        return datetime.fromisoformat(added_at).strftime("%b %d, %Y")
    except ValueError:
        return "—"


class TrackInfoDialog(QDialog):
    """Read-only "what is this track" view — art, every tag field, duration/
    file size/added date, measured loudness vs. target, and which playlists
    it's in. Distinct from the Tags editor, which is for *changing* things;
    this is for just looking. Self-contained like every other dialog in this
    file: takes only enough to look everything else up itself. `kind` comes
    from the caller (Songs vs. Audiobooks tab; playlists are always music)
    rather than the row, since it drives the loudness-target lookup and the
    missing-art flag below. EDIT TAGS doesn't emit fixTagsRequested itself —
    that signal belongs to LibraryView/PlaylistView — it just sets
    edit_tags_requested and closes; the caller checks that after exec() and
    emits it, same "dialog sets an attribute, caller acts on it" convention
    AddToPlaylistDialog/ResolveDuplicateDialog already use."""

    def __init__(self, parent, library_root: str, track_id: int, kind: str):
        super().__init__(parent)
        self.edit_tags_requested = False
        self.setWindowTitle("Track Info")
        self.resize(620, 420)

        conn = libdb.open_db(library_root)
        row = conn.execute("SELECT * FROM tracks WHERE id = ?", (track_id,)).fetchone()
        playlist_rows = libdb.playlists_for_track(conn, track_id)
        conn.close()

        full_path = Path(library_root) / row["library_path"]
        art_bytes = None
        try:
            art_bytes = tagging.read_tags(str(full_path)).get("art")
        except Exception:
            pass  # file missing/unreadable — falls back to the placeholder tile

        layout = QVBoxLayout(self)

        header_title = QLabel("TRACK INFO")
        header_title.setObjectName("panelTitle")
        layout.addWidget(header_title)

        body = QHBoxLayout()
        body.setSpacing(20)
        body.addLayout(self._build_left_pane(row, full_path, art_bytes))
        body.addLayout(self._build_right_pane(row, kind, art_bytes, playlist_rows), stretch=1)
        layout.addLayout(body)

        footer = QHBoxLayout()
        footer.addStretch(1)
        edit_btn = QPushButton("EDIT TAGS")
        edit_btn.clicked.connect(self._request_edit_tags)
        footer.addWidget(edit_btn)
        close_btn = QPushButton("CLOSE")
        close_btn.setObjectName("primary")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        layout.addLayout(footer)

    def _build_left_pane(self, row, full_path: Path, art_bytes: bytes | None) -> QVBoxLayout:
        col = QVBoxLayout()
        art = ChamferedArtTile(size=168, chamfer=16, clickable=False)
        art.set_art(art_bytes)
        col.addWidget(art)
        col.addLayout(self._field("DURATION", _format_duration(row["duration_seconds"])))
        col.addLayout(self._field("FILE SIZE", _format_filesize(full_path)))
        col.addLayout(self._field("ADDED", _format_added_date(row["added_at"])))
        col.addStretch(1)
        return col

    def _build_right_pane(self, row, kind: str, art_bytes: bytes | None, playlist_rows) -> QVBoxLayout:
        col = QVBoxLayout()

        heading_row = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        title_label = QLabel(row["title"] or Path(row["library_path"]).stem)
        title_label.setObjectName("panelTitle")
        title_label.setWordWrap(True)
        title_col.addWidget(title_label)
        artist_label = QLabel(row["artist"] or "Unknown Artist")
        artist_label.setObjectName("muted")
        title_col.addWidget(artist_label)
        heading_row.addLayout(title_col, stretch=1)
        if kind == "music" and not art_bytes:
            heading_row.addWidget(PixelLabel("MISSING ART", point_size=8, color=RED, letter_spacing=1))
        col.addLayout(heading_row)

        rule1 = QFrame()
        rule1.setObjectName("rule")
        col.addWidget(rule1)

        row1 = QHBoxLayout()
        row1.addLayout(self._field("ALBUM", row["album"] or "—"))
        row1.addLayout(self._field("DATE", row["date"] or "—"))
        col.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addLayout(self._field("TRACK #", row["tracknumber"] or "—"))
        row2.addLayout(self._field("LOUDNESS", self._loudness_text(row, kind)))
        col.addLayout(row2)

        col.addLayout(self._field("LIBRARY PATH", row["library_path"], muted_value=True))

        rule2 = QFrame()
        rule2.setObjectName("rule")
        col.addWidget(rule2)

        col.addWidget(PixelLabel("IN PLAYLISTS", point_size=8, color=MUTED, letter_spacing=1))
        col.addLayout(self._build_playlist_chips(playlist_rows))

        col.addStretch(1)
        return col

    def _loudness_text(self, row, kind: str) -> str:
        lufs, true_peak = row["loudness_lufs"], row["loudness_true_peak"]
        if lufs is None or true_peak is None:
            return "Not yet measured"
        gain = loudness.compute_gain_db(lufs, true_peak, kind)
        target = loudness.TARGET_LUFS.get(kind, loudness.TARGET_LUFS["music"])
        return f"{lufs:.1f} LUFS (target {target:.0f}, {gain:+.1f}dB)"

    def _field(self, label_text: str, value_text: str, muted_value: bool = False) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(2)
        col.addWidget(PixelLabel(label_text, point_size=8, color=MUTED, letter_spacing=1))
        value = QLabel(value_text)
        value.setWordWrap(True)
        if muted_value:
            value.setObjectName("muted")
        col.addWidget(value)
        return col

    def _build_playlist_chips(self, playlist_rows) -> QHBoxLayout:
        row = QHBoxLayout()
        if not playlist_rows:
            not_in_playlist = QLabel("Not in any playlist")
            not_in_playlist.setObjectName("muted")
            row.addWidget(not_in_playlist)
            row.addStretch(1)
            return row
        for p in playlist_rows:
            chip = QLabel(p["name"])
            chip.setStyleSheet(
                f"border: 1px solid {ORANGE}; color: {ORANGE}; border-radius: 9px; padding: 3px 10px;"
            )
            row.addWidget(chip)
        row.addStretch(1)
        return row

    def _request_edit_tags(self):
        self.edit_tags_requested = True
        self.accept()


class LibraryView(QWidget):
    """The app's home screen. get_library_root/set_library_root let this view
    share the persisted library location with MainWindow without owning
    QSettings itself."""

    fixTagsRequested = pyqtSignal(str, int)  # mp3 path, track_id — Unsorted tab's "Fix Tags" action
    libraryChanged = pyqtSignal()  # emitted after every refresh — sidebar playlist list listens to this
    libraryLocationChanged = pyqtSignal(str)  # new library_root — MainWindow logs this to the console
    playlistActivated = pyqtSignal(int, str)  # playlist_id, name — picked from a track's badge-click menu
    playRequested = pyqtSignal(list, int)  # queue (dicts with id/title/artist/path), start index — double-clicked a song
    healthRescanRequested = pyqtSignal()  # HEALTH tab's RESCAN button — nudges MainWindow's background art-check worker
    audiobookSeriesActivated = pyqtSignal(str)  # series (Album tag) name — a shelf series tile was clicked

    def __init__(self, get_library_root, set_library_root):
        super().__init__()
        self._get_library_root = get_library_root
        self._set_library_root = set_library_root
        self._sort_field = "Title"
        self._sort_ascending = True
        self._standalone_audiobook_art_cache: dict[int, "bytes | None"] = {}
        self._search_debounce = QTimer(self)
        self._search_debounce.setSingleShot(True)
        self._search_debounce.setInterval(200)
        self._search_debounce.timeout.connect(self.refresh)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(10)

        title = QLabel("LIBRARY")
        title.setObjectName("panelTitle")
        layout.addWidget(title)

        self.location_btn = QPushButton()
        self.location_btn.setObjectName("outputBar")
        self.location_btn.clicked.connect(self._change_library_location)
        layout.addWidget(self.location_btn)
        self._refresh_location_label()

        stats_row = QHBoxLayout()
        stats_row.setSpacing(10)
        self.stat_songs_value = QLabel("0")
        self.stat_audiobooks_value = QLabel("0")
        self.stat_playlists_value = QLabel("0")
        self.stat_playtime_value = QLabel("0m")
        self.stat_artists_value = QLabel("0")
        stats_row.addWidget(self._make_stat_card("SONGS", self.stat_songs_value, STAT_BLUE))
        stats_row.addWidget(self._make_stat_card("AUDIOBOOKS", self.stat_audiobooks_value, STAT_PURPLE))
        stats_row.addWidget(self._make_stat_card("PLAYLISTS", self.stat_playlists_value, GREEN))
        stats_row.addWidget(self._make_stat_card("PLAYTIME", self.stat_playtime_value, STAT_AMBER))
        stats_row.addWidget(self._make_stat_card("ARTISTS", self.stat_artists_value, ORANGE))
        layout.addLayout(stats_row)

        top_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search title, artist, album...")
        self.search_edit.textChanged.connect(lambda: self._search_debounce.start())
        top_row.addWidget(self.search_edit, stretch=1)

        settings_btn = QPushButton("⚙")
        settings_btn.setToolTip("Library settings")
        settings_menu = QMenu(self)
        add_sources_action = QAction("ADD SOURCES...", self)
        add_sources_action.triggered.connect(self._open_migration_dialog)
        add_files_action = QAction("ADD FILES...", self)
        add_files_action.triggered.connect(self._open_add_files_dialog)
        export_action = QAction("EXPORT...", self)
        export_action.triggered.connect(self._open_export_dialog)
        settings_menu.addAction(add_sources_action)
        settings_menu.addAction(add_files_action)
        settings_menu.addAction(export_action)
        settings_btn.setMenu(settings_menu)
        top_row.addWidget(settings_btn)
        layout.addLayout(top_row)

        self.sort_actions = QWidget()
        sort_row = QHBoxLayout(self.sort_actions)
        sort_row.setContentsMargins(0, 0, 0, 0)
        sort_label = QLabel("SORT:")
        sort_label.setObjectName("muted")
        sort_row.addWidget(sort_label)
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(SORT_FIELDS)
        self.sort_combo.currentTextChanged.connect(self._on_sort_changed)
        sort_row.addWidget(self.sort_combo)
        self.sort_direction_btn = QPushButton("▲")
        self.sort_direction_btn.setToolTip("Toggle ascending/descending")
        self.sort_direction_btn.clicked.connect(self._toggle_sort_direction)
        sort_row.addWidget(self.sort_direction_btn)
        sort_row.addStretch(1)
        layout.addWidget(self.sort_actions)

        self.sub_tabs = QTabWidget()
        self.songs_list = make_track_list()
        self.audiobooks_list = make_track_list()
        self.songs_list.badgeClicked.connect(self._show_playlist_membership_menu)
        self.audiobooks_list.badgeClicked.connect(self._show_playlist_membership_menu)
        self.songs_list.infoClicked.connect(self._show_track_info_by_id)
        self.audiobooks_list.infoClicked.connect(self._show_track_info_by_id)
        self.songs_list.itemDoubleClicked.connect(self._play_from_songs)  # music only — never audiobooks_list
        self.duplicates_list = QListWidget()
        self.unsorted_list = QListWidget()
        self.unsorted_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        self.sub_tabs.addTab(self.songs_list, "SONGS")
        self.sub_tabs.addTab(self._build_audiobooks_tab(), "AUDIOBOOKS")
        self.sub_tabs.addTab(self.duplicates_list, "DUPLICATES")
        self.sub_tabs.addTab(self.unsorted_list, "UNSORTED")
        self.sub_tabs.addTab(self._build_health_tab(), "HEALTH")
        layout.addWidget(self.sub_tabs, stretch=1)

        self.copy_actions = QWidget()
        copy_row = QHBoxLayout(self.copy_actions)
        copy_row.setContentsMargins(0, 0, 0, 0)
        copy_row.addStretch(1)
        edit_tags_btn = QPushButton("EDIT TAGS")
        edit_tags_btn.clicked.connect(self._edit_tags_selected_track)
        copy_row.addWidget(edit_tags_btn)
        remove_selected_btn = QPushButton("REMOVE SELECTED")
        remove_selected_btn.clicked.connect(self._remove_selected_tracks)
        copy_row.addWidget(remove_selected_btn)
        copy_selected_btn = QPushButton("COPY SELECTED...")
        copy_selected_btn.clicked.connect(self._copy_selected_tracks)
        copy_row.addWidget(copy_selected_btn)
        self.add_to_playlist_btn = QPushButton("ADD TO PLAYLIST...")
        self.add_to_playlist_btn.clicked.connect(self._add_selected_to_playlist)
        copy_row.addWidget(self.add_to_playlist_btn)
        layout.addWidget(self.copy_actions)

        self.dupe_actions = QWidget()
        dupe_row = QHBoxLayout(self.dupe_actions)
        dupe_row.setContentsMargins(0, 0, 0, 0)
        dupe_row.addStretch(1)
        resolve_btn = QPushButton("RESOLVE...")
        resolve_btn.clicked.connect(self._resolve_selected_dupe)
        dupe_row.addWidget(resolve_btn)
        dismiss_btn = QPushButton("DISMISS SELECTED")
        dismiss_btn.clicked.connect(self._dismiss_selected_dupe)
        dupe_row.addWidget(dismiss_btn)
        layout.addWidget(self.dupe_actions)

        self.unsorted_actions = QWidget()
        unsorted_row = QHBoxLayout(self.unsorted_actions)
        unsorted_row.setContentsMargins(0, 0, 0, 0)
        unsorted_row.addStretch(1)
        fix_tags_btn = QPushButton("FIX TAGS")
        fix_tags_btn.clicked.connect(self._fix_selected_unsorted_tags)
        unsorted_row.addWidget(fix_tags_btn)
        remove_unsorted_btn = QPushButton("REMOVE SELECTED")
        remove_unsorted_btn.clicked.connect(self._remove_selected_unsorted)
        unsorted_row.addWidget(remove_unsorted_btn)
        layout.addWidget(self.unsorted_actions)

        self.sub_tabs.currentChanged.connect(self._update_sub_tab_actions)
        self._update_sub_tab_actions(self.sub_tabs.currentIndex())

        self.empty_hint = QLabel("")
        self.empty_hint.setObjectName("muted")
        self.empty_hint.setWordWrap(True)
        layout.addWidget(self.empty_hint)

        self.refresh()

    def _make_stat_card(self, label_text: str, value_label: QLabel, value_color: str) -> ChamferedFrame:
        frame = ChamferedFrame(chamfer=12)
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(14, 12, 14, 12)
        inner.setSpacing(8)
        # Label stays a uniform muted pixel caption — the color goes on the
        # number itself instead, one distinct accent per card, so they're
        # tellable apart at a glance without turning the labels into noise.
        inner.addWidget(PixelLabel(label_text, point_size=12, color=MUTED, letter_spacing=2))
        value_label.setStyleSheet(f"font-size: 22px; font-weight: bold; color: {value_color};")
        inner.addWidget(value_label)
        return frame

    def _build_audiobooks_tab(self) -> QWidget:
        """SERIES (default) / ALL toggle over a stack: the new shelf-of-
        covers browse view, and the existing flat sortable list (unchanged)
        with its full bulk-action row — additive, not a replacement, so
        Copy Selected/Remove Selected/Edit Tags stay available on the full
        set via ALL even though the shelf itself has no bulk actions of its
        own at the top level (drilling into a series gets its own)."""
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(8)

        toggle_row = QHBoxLayout()
        view_label = QLabel("VIEW:")
        view_label.setObjectName("muted")
        toggle_row.addWidget(view_label)
        self.audiobooks_view_toggle = QComboBox()
        self.audiobooks_view_toggle.addItems(["SERIES", "ALL"])
        self.audiobooks_view_toggle.currentTextChanged.connect(self._on_audiobooks_view_changed)
        toggle_row.addWidget(self.audiobooks_view_toggle)
        toggle_row.addStretch(1)
        layout.addLayout(toggle_row)

        self.audiobooks_stack = QStackedWidget()
        self.audiobooks_shelf = AudiobookShelfWidget()
        self.audiobooks_shelf.seriesActivated.connect(self.audiobookSeriesActivated.emit)
        self.audiobooks_shelf.playRequested.connect(self.playRequested.emit)
        self.audiobooks_shelf.infoRequested.connect(self._show_track_info_by_id)
        self.audiobooks_stack.addWidget(self.audiobooks_shelf)
        self.audiobooks_stack.addWidget(self.audiobooks_list)
        layout.addWidget(self.audiobooks_stack, stretch=1)

        return tab

    def _on_audiobooks_view_changed(self, text: str):
        self.audiobooks_stack.setCurrentWidget(
            self.audiobooks_list if text == "ALL" else self.audiobooks_shelf
        )
        self._update_sub_tab_actions(self.sub_tabs.currentIndex())

    def _update_sub_tab_actions(self, index: int):
        audiobooks_all = index == 1 and self.audiobooks_view_toggle.currentText() == "ALL"
        self.sort_actions.setVisible(index == 0 or audiobooks_all)
        self.copy_actions.setVisible(index == 0 or audiobooks_all)
        self.add_to_playlist_btn.setVisible(index == 0)  # SONGS only — playlists are a music-only concept
        self.dupe_actions.setVisible(index == 2)  # DUPLICATES
        self.unsorted_actions.setVisible(index == 3)  # UNSORTED
        if index == HEALTH_TAB_INDEX:
            self._refresh_health()

    def _on_sort_changed(self, field: str):
        self._sort_field = field
        self.refresh()

    def _toggle_sort_direction(self):
        self._sort_ascending = not self._sort_ascending
        self.sort_direction_btn.setText("▲" if self._sort_ascending else "▼")
        self.refresh()

    # ---- data loading ----

    def _open_migration_dialog(self):
        library_root = self._get_library_root()
        if not library_root:
            chosen = QFileDialog.getExistingDirectory(
                self, "Choose or create a folder for your library", str(Path.home())
            )
            if not chosen:
                return
            library_root = chosen
            self._set_library_root(library_root)
            self._refresh_location_label()
            self.libraryLocationChanged.emit(library_root)

        dialog = LibraryMigrationDialog(self, library_root)
        dialog.exec()
        if dialog.build_succeeded:
            self.refresh()

    def _open_add_files_dialog(self):
        library_root = self._get_library_root()
        if not library_root:
            chosen = QFileDialog.getExistingDirectory(
                self, "Choose or create a folder for your library", str(Path.home())
            )
            if not chosen:
                return
            library_root = chosen
            self._set_library_root(library_root)
            self._refresh_location_label()
            self.libraryLocationChanged.emit(library_root)

        dialog = AddFilesDialog(self, library_root)
        dialog.exec()
        if dialog.added_any:
            self.refresh()

    def _open_export_dialog(self):
        library_root = self._get_library_root()
        if not library_root or not Path(library_root).exists():
            QMessageBox.information(self, "No library yet", "Add sources and build your library first.")
            return
        ExportDialog(self, library_root).exec()

    def _refresh_location_label(self):
        root = self._get_library_root()
        if root:
            self.location_btn.setText(f"LIBRARY ▸  {root}   (click to change)")
        else:
            self.location_btn.setText("LIBRARY ▸  (no location chosen yet)   (click to change)")

    def _change_library_location(self):
        current = self._get_library_root()
        start_dir = current if current and Path(current).exists() else str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose your library folder", start_dir)
        if not chosen:
            return
        self._set_library_root(chosen)
        self._refresh_location_label()
        self.libraryLocationChanged.emit(chosen)
        self.refresh()

    def refresh(self):
        library_root = self._get_library_root()
        if not library_root or not Path(library_root).exists():
            self._clear_all()
            self.empty_hint.setText("No library yet — click ADD SOURCES to get started.")
            self.libraryChanged.emit()
            return

        conn = libdb.open_db(library_root)
        query = self.search_edit.text().strip().lower()
        membership = libdb.playlist_membership_counts(conn)

        music_tracks = [t for t in libdb.list_tracks(conn, "music") if not _is_unsorted(t["library_path"])]
        unsorted_tracks = [t for t in libdb.list_tracks(conn, "music") if _is_unsorted(t["library_path"])]
        audiobook_tracks = libdb.list_tracks(conn, "audiobook")
        dupes = libdb.list_unresolved_fuzzy_dupes(conn)

        sorted_music = sort_tracks(self._filter(music_tracks, query), membership, self._sort_field, self._sort_ascending)
        sorted_audiobooks = sort_tracks(
            self._filter(audiobook_tracks, query), membership, self._sort_field, self._sort_ascending
        )
        populate_track_list(self.songs_list, sorted_music, membership)
        populate_track_list(self.audiobooks_list, sorted_audiobooks, membership)
        self._populate_unsorted(unsorted_tracks)
        self._populate_dupes(conn, dupes)
        self._refresh_audiobook_shelf(conn, library_root)

        self.sub_tabs.setTabText(3, f"UNSORTED ({len(unsorted_tracks)})")
        self.empty_hint.setText(
            "" if (music_tracks or audiobook_tracks) else "No library yet — click ADD SOURCES to get started."
        )
        self.stat_songs_value.setText(str(len(music_tracks)))
        self.stat_audiobooks_value.setText(str(len(audiobook_tracks)))
        self.stat_playlists_value.setText(str(len(libdb.list_playlists(conn))))
        self.stat_playtime_value.setText(_format_total_playtime(libdb.music_total_duration_seconds(conn)))
        self.stat_artists_value.setText(str(libdb.music_unique_artist_count(conn)))
        conn.close()
        self.libraryChanged.emit()

    def _filter(self, tracks, query: str):
        if not query:
            return tracks
        return [
            t for t in tracks
            if query in (t["title"] or "").lower()
            or query in (t["artist"] or "").lower()
            or query in (t["album"] or "").lower()
        ]

    def _populate_unsorted(self, tracks):
        self.unsorted_list.clear()
        library_root = self._get_library_root()
        for t in tracks:
            label = t["title"] or Path(t["library_path"]).name
            item = QListWidgetItem(f"{label}  —  {t['library_path']}")
            item.setData(Qt.ItemDataRole.UserRole, {
                "path": str(Path(library_root) / t["library_path"]),
                "track_id": t["id"],
            })
            self.unsorted_list.addItem(item)

    def _refresh_audiobook_shelf(self, conn, library_root: str):
        """Populates the SERIES-view shelf. Series artwork reads are cheap
        (one small file per distinct series, typically very few) so they're
        read fresh every call; standalone books' embedded art is cached by
        track id across refreshes instead of re-read on every keystroke of
        search (the exact anti-pattern the Health dashboard's own refresh
        deliberately avoids for the same reason)."""
        series_entries = []
        for row in libdb.list_audiobook_series(conn):
            art_path = find_series_artwork(library_root, row["album"])
            art_bytes = art_path.read_bytes() if art_path else None
            book_word = "book" if row["book_count"] == 1 else "books"
            series_entries.append((row["album"], f"{row['book_count']} {book_word}", art_bytes))

        standalone_entries = []
        for t in libdb.list_standalone_audiobooks(conn):
            if t["id"] not in self._standalone_audiobook_art_cache:
                full_path = str(Path(library_root) / t["library_path"])
                try:
                    self._standalone_audiobook_art_cache[t["id"]] = tagging.read_tags(full_path).get("art")
                except Exception:
                    self._standalone_audiobook_art_cache[t["id"]] = None
            standalone_entries.append({
                "id": t["id"],
                "title": t["title"] or Path(t["library_path"]).stem,
                "artist": t["artist"] or "",
                "path": str(Path(library_root) / t["library_path"]),
                "kind": "audiobook",
                "loudness_lufs": t["loudness_lufs"],
                "loudness_true_peak": t["loudness_true_peak"],
                "art": self._standalone_audiobook_art_cache[t["id"]],
            })

        self.audiobooks_shelf.populate(series_entries, standalone_entries)

    def _populate_dupes(self, conn, dupes):
        self.duplicates_list.clear()
        for d in dupes:
            a = conn.execute("SELECT * FROM tracks WHERE id = ?", (d["track_id_a"],)).fetchone()
            b = conn.execute("SELECT * FROM tracks WHERE id = ?", (d["track_id_b"],)).fetchone()
            if not a or not b:
                continue
            text = f"{a['title']} — {a['artist']}\n   {a['library_path']}\n   vs. {b['library_path']}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, d["id"])
            self.duplicates_list.addItem(item)

    def _clear_all(self):
        self.songs_list.clear()
        self.audiobooks_list.clear()
        self.audiobooks_shelf.populate([], [])
        self.duplicates_list.clear()
        self.unsorted_list.clear()
        self.sub_tabs.setTabText(3, "UNSORTED (0)")
        self.stat_songs_value.setText("0")
        self.stat_audiobooks_value.setText("0")
        self.stat_playlists_value.setText("0")
        self.stat_playtime_value.setText("0m")
        self.stat_artists_value.setText("0")

    # ---- actions ----

    def _copy_selected_tracks(self):
        active_list = self.songs_list if self.sub_tabs.currentIndex() == 0 else self.audiobooks_list
        paths = selected_library_paths(active_list)
        if not paths:
            QMessageBox.information(self, "Nothing selected", "Select one or more tracks first.")
            return
        library_root = self._get_library_root()
        if not library_root:
            return
        CopyTracksDialog(self, library_root, paths).exec()

    def _add_selected_to_playlist(self):
        items = self.songs_list.selectedItems()  # Songs only — button is hidden on every other tab
        if not items:
            QMessageBox.information(self, "Nothing selected", "Select one or more tracks first.")
            return
        library_root = self._get_library_root()
        if not library_root:
            return
        track_ids = [item.data(Qt.ItemDataRole.UserRole)["id"] for item in items]
        dialog = AddToPlaylistDialog(self, library_root, track_ids)
        if dialog.exec() and dialog.added_count:
            conn = libdb.open_db(library_root)
            paths = libdb.playlist_track_paths_in_order(conn, dialog.target_playlist_id)
            conn.close()
            write_m3u8(str(Path(library_root) / "Playlists"), dialog.target_playlist_name, paths, library_root)
            self.refresh()
            QMessageBox.information(
                self, "Added",
                f"Added {dialog.added_count} track(s) to '{dialog.target_playlist_name}'.",
            )

    def _edit_tags_selected_track(self):
        active_list = self.songs_list if self.sub_tabs.currentIndex() == 0 else self.audiobooks_list
        items = active_list.selectedItems()
        if len(items) != 1:
            QMessageBox.information(self, "Select one track", "Select exactly one track to edit its tags.")
            return
        library_root = self._get_library_root()
        if not library_root:
            return
        data = items[0].data(Qt.ItemDataRole.UserRole)
        full_path = str(Path(library_root) / data["library_path"])
        self.fixTagsRequested.emit(full_path, data["id"])

    def _show_track_info_by_id(self, track_id: int):
        library_root = self._get_library_root()
        if not library_root:
            return
        conn = libdb.open_db(library_root)
        row = conn.execute("SELECT kind, library_path FROM tracks WHERE id = ?", (track_id,)).fetchone()
        conn.close()
        if not row:
            return
        dialog = TrackInfoDialog(self, library_root, track_id, row["kind"])
        dialog.exec()
        if dialog.edit_tags_requested:
            self.fixTagsRequested.emit(str(Path(library_root) / row["library_path"]), track_id)

    def _show_playlist_membership_menu(self, track_id: int, global_pos):
        library_root = self._get_library_root()
        if not library_root:
            return
        conn = libdb.open_db(library_root)
        rows = libdb.playlists_for_track(conn, track_id)
        conn.close()
        if not rows:
            return
        menu = QMenu(self)
        for row in rows:
            action = menu.addAction(row["name"])
            action.triggered.connect(
                lambda checked=False, pid=row["id"], name=row["name"]: self.playlistActivated.emit(pid, name)
            )
        menu.exec(global_pos)

    def _play_from_songs(self, item: QListWidgetItem):
        library_root = self._get_library_root()
        if not library_root:
            return
        tracks = []
        for i in range(self.songs_list.count()):
            data = self.songs_list.item(i).data(Qt.ItemDataRole.UserRole)
            tracks.append({
                "id": data["id"],
                "title": data["title"],
                "artist": data["artist"],
                "path": str(Path(library_root) / data["library_path"]),
                "kind": "music",
                "loudness_lufs": data.get("loudness_lufs"),
                "loudness_true_peak": data.get("loudness_true_peak"),
            })
        self.playRequested.emit(tracks, self.songs_list.row(item))

    def set_now_playing_track(self, track_id: int | None):
        self.songs_list.set_playing_track(track_id)
        self.audiobooks_list.set_playing_track(track_id)  # picks up an audiobook played from the shelf/series view

    def _remove_selected_tracks(self):
        active_list = self.songs_list if self.sub_tabs.currentIndex() == 0 else self.audiobooks_list
        items = active_list.selectedItems()
        if not items:
            QMessageBox.information(self, "Nothing selected", "Select one or more tracks first.")
            return
        library_root = self._get_library_root()
        if not library_root:
            return
        reply = QMessageBox.question(
            self, "Remove from library",
            f"Permanently delete {len(items)} file(s) from your library? This only removes "
            f"Splice's own copy — nothing at your original source location is touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        conn = libdb.open_db(library_root)
        affected_playlist_ids = set()
        failed = []
        for item in items:
            data = item.data(Qt.ItemDataRole.UserRole)
            track_id = data["id"]
            path = Path(library_root) / data["library_path"]
            error = _try_unlink(path)
            if error:
                failed.append(path.name)
                continue  # leave its index entry in place — the file's still really there
            affected_playlist_ids.update(libdb.playlists_containing_track(conn, track_id))
            libdb.delete_track(conn, track_id)
        for playlist_id in affected_playlist_ids:
            row = conn.execute("SELECT name FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
            if row:
                paths = libdb.playlist_track_paths_in_order(conn, playlist_id)
                write_m3u8(str(Path(library_root) / "Playlists"), row["name"], paths, library_root)
        conn.close()
        self.refresh()
        if failed:
            QMessageBox.warning(
                self, "Some files couldn't be removed",
                "These are currently in use by another program, so they weren't removed "
                "(nothing changed for them — try again once whatever has them open is closed):\n\n"
                + "\n".join(failed),
            )

    def _dismiss_selected_dupe(self):
        item = self.duplicates_list.currentItem()
        if item is None:
            QMessageBox.information(self, "Nothing selected", "Click a duplicate pair in the list first.")
            return
        library_root = self._get_library_root()
        conn = libdb.open_db(library_root)
        libdb.resolve_fuzzy_dupe(conn, item.data(Qt.ItemDataRole.UserRole))
        conn.close()
        self.refresh()

    def _resolve_selected_dupe(self):
        item = self.duplicates_list.currentItem()
        if item is None:
            QMessageBox.information(self, "Nothing selected", "Click a duplicate pair in the list first.")
            return
        library_root = self._get_library_root()
        if not library_root:
            return
        dupe_id = item.data(Qt.ItemDataRole.UserRole)
        conn = libdb.open_db(library_root)
        dupe_row = conn.execute("SELECT * FROM fuzzy_dupes WHERE id = ?", (dupe_id,)).fetchone()
        if not dupe_row:
            conn.close()
            return
        track_a = conn.execute("SELECT * FROM tracks WHERE id = ?", (dupe_row["track_id_a"],)).fetchone()
        track_b = conn.execute("SELECT * FROM tracks WHERE id = ?", (dupe_row["track_id_b"],)).fetchone()
        conn.close()
        if not track_a or not track_b:
            QMessageBox.information(self, "Already resolved", "One of these tracks no longer exists.")
            self.refresh()
            return

        dialog = ResolveDuplicateDialog(self, library_root, track_a, track_b)
        if dialog.exec() and dialog.keep_id is not None:
            self._resolve_dupe(dialog.keep_id, dialog.remove_id)

    def _resolve_dupe(self, keep_id: int, remove_id: int):
        library_root = self._get_library_root()
        conn = libdb.open_db(library_root)
        row = conn.execute("SELECT library_path FROM tracks WHERE id = ?", (remove_id,)).fetchone()
        if row:
            error = _try_unlink(Path(library_root) / row["library_path"])
            if error:
                conn.close()
                QMessageBox.warning(
                    self, "Couldn't resolve",
                    "That file is currently in use by another program and can't be removed yet — "
                    "close whatever has it open and try again. Nothing was changed.",
                )
                return
        affected_playlist_ids = libdb.playlists_containing_track(conn, remove_id)
        for playlist_id in affected_playlist_ids:
            position = libdb.next_playlist_position(conn, playlist_id)
            libdb.add_playlist_track(conn, playlist_id, keep_id, position)  # no-op if keep_id is already in there
        libdb.delete_track(conn, remove_id)  # also drops the fuzzy_dupes row for this pair
        for playlist_id in affected_playlist_ids:
            prow = conn.execute("SELECT name FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
            if prow:
                paths = libdb.playlist_track_paths_in_order(conn, playlist_id)
                write_m3u8(str(Path(library_root) / "Playlists"), prow["name"], paths, library_root)
        conn.close()
        self.refresh()

    def _fix_selected_unsorted_tags(self):
        items = self.unsorted_list.selectedItems()
        if len(items) != 1:
            QMessageBox.information(self, "Select one file", "Select exactly one file to fix its tags.")
            return
        data = items[0].data(Qt.ItemDataRole.UserRole)
        self.fixTagsRequested.emit(data["path"], data["track_id"])

    def _remove_selected_unsorted(self):
        items = self.unsorted_list.selectedItems()
        if not items:
            QMessageBox.information(self, "Nothing selected", "Select one or more files first.")
            return
        library_root = self._get_library_root()
        if not library_root:
            return
        reply = QMessageBox.question(
            self, "Remove from library",
            f"Permanently delete {len(items)} file(s) from your library? This only removes "
            f"Splice's own copy — nothing at your original source location is touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        conn = libdb.open_db(library_root)
        affected_playlist_ids = set()
        failed = []
        for item in items:
            data = item.data(Qt.ItemDataRole.UserRole)
            track_id = data["track_id"]
            path = Path(data["path"])
            error = _try_unlink(path)
            if error:
                failed.append(path.name)
                continue  # leave its index entry in place — the file's still really there
            affected_playlist_ids.update(libdb.playlists_containing_track(conn, track_id))
            libdb.delete_track(conn, track_id)
        for playlist_id in affected_playlist_ids:
            row = conn.execute("SELECT name FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
            if row:
                paths = libdb.playlist_track_paths_in_order(conn, playlist_id)
                write_m3u8(str(Path(library_root) / "Playlists"), row["name"], paths, library_root)
        conn.close()
        self.refresh()
        if failed:
            QMessageBox.warning(
                self, "Some files couldn't be removed",
                "These are currently in use by another program, so they weren't removed "
                "(nothing changed for them — try again once whatever has them open is closed):\n\n"
                + "\n".join(failed),
            )

    # ---- health dashboard ----

    def _build_health_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 10, 0, 0)
        layout.setSpacing(10)

        header_row = QHBoxLayout()
        hint = QLabel(
            "Checks your library for issues that can quietly accumulate — missing tags or art, "
            "duplicates, and index entries that have gone stale against the files on disk."
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        header_row.addWidget(hint, stretch=1)
        rescan_btn = QPushButton("RESCAN")
        rescan_btn.clicked.connect(self._health_rescan)
        header_row.addWidget(rescan_btn)
        layout.addLayout(header_row)

        stats_row = QHBoxLayout()
        stats_row.setSpacing(10)
        self.health_stat_values = {}
        for label, color in [
            ("DUPLICATES", GREEN),
            ("MISSING TAGS", STAT_AMBER),
            ("MISSING ART", STAT_PURPLE),
            ("MISSING FILES", RED),
            ("ORPHANED FILES", ORANGE),
        ]:
            value_label = QLabel("0")
            self.health_stat_values[label] = value_label
            stats_row.addWidget(self._make_stat_card(label, value_label, color))
        layout.addLayout(stats_row)

        category_row = QHBoxLayout()
        category_label = QLabel("SHOW:")
        category_label.setObjectName("muted")
        category_row.addWidget(category_label)
        self.health_category_combo = QComboBox()
        self.health_category_combo.addItems(HEALTH_CATEGORIES)
        self.health_category_combo.currentTextChanged.connect(self._on_health_category_changed)
        category_row.addWidget(self.health_category_combo)
        category_row.addStretch(1)
        layout.addLayout(category_row)

        self.health_list = QListWidget()
        self.health_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(self.health_list, stretch=1)

        self.health_fix_tags_btn = QPushButton("FIX TAGS")
        self.health_fix_tags_btn.clicked.connect(self._health_fix_tags)
        self.health_remove_btn = QPushButton("REMOVE FROM INDEX")
        self.health_remove_btn.clicked.connect(self._health_remove_missing)
        self.health_adopt_btn = QPushButton("FILE INTO LIBRARY")
        self.health_adopt_btn.clicked.connect(self._health_file_orphans)
        self.health_goto_dupes_btn = QPushButton("GO TO DUPLICATES")
        self.health_goto_dupes_btn.clicked.connect(lambda: self.sub_tabs.setCurrentIndex(2))

        action_row = QHBoxLayout()
        action_row.addStretch(1)
        action_row.addWidget(self.health_fix_tags_btn)
        action_row.addWidget(self.health_remove_btn)
        action_row.addWidget(self.health_adopt_btn)
        action_row.addWidget(self.health_goto_dupes_btn)
        layout.addLayout(action_row)

        self._health_data = {c: [] for c in HEALTH_CATEGORIES}
        self._on_health_category_changed(self.health_category_combo.currentText())
        return tab

    def _on_health_category_changed(self, category: str):
        self.health_fix_tags_btn.setVisible(category in ("Missing Tags", "Missing Art"))
        self.health_remove_btn.setVisible(category == "Missing Files")
        self.health_adopt_btn.setVisible(category == "Orphaned Files")
        self.health_goto_dupes_btn.setVisible(category == "Duplicates")
        self._populate_health_list(category)

    def _populate_health_list(self, category: str):
        self.health_list.clear()
        for entry in self._health_data.get(category, []):
            item = QListWidgetItem(entry["label"])
            item.setData(Qt.ItemDataRole.UserRole, entry["data"])
            self.health_list.addItem(item)

    def _refresh_health(self):
        """Computes all 5 categories in one pass. Deliberately NOT part of
        refresh() (which reruns on every 200ms search-debounce keystroke) —
        the filesystem-walk-based checks here (Path.exists() per track,
        rglob over Music/Audiobooks) would make typing in search laggy if
        coupled to that hot path. Only called lazily on switching into this
        tab, or via the RESCAN button."""
        library_root = self._get_library_root()
        if not library_root or not Path(library_root).exists():
            self._health_data = {c: [] for c in HEALTH_CATEGORIES}
            for value_label in self.health_stat_values.values():
                value_label.setText("0")
            self._populate_health_list(self.health_category_combo.currentText())
            return

        conn = libdb.open_db(library_root)

        dupe_entries = []
        for d in libdb.list_unresolved_fuzzy_dupes(conn):
            a = conn.execute("SELECT * FROM tracks WHERE id = ?", (d["track_id_a"],)).fetchone()
            b = conn.execute("SELECT * FROM tracks WHERE id = ?", (d["track_id_b"],)).fetchone()
            if not a or not b:
                continue
            dupe_entries.append({"label": f"{a['title']} — {a['artist']}", "data": d["id"]})

        music_tracks = libdb.list_tracks(conn, "music")
        filed_music = [t for t in music_tracks if not _is_unsorted(t["library_path"])]
        tag_entries = [
            {"label": f"{t['title'] or Path(t['library_path']).stem}  —  {t['library_path']}",
             "data": {"path": str(Path(library_root) / t["library_path"]), "track_id": t["id"]}}
            for t in _find_missing_tags(filed_music)
        ]
        art_entries = [
            {"label": f"{t['title'] or Path(t['library_path']).stem}  —  {t['library_path']}",
             "data": {"path": str(Path(library_root) / t["library_path"]), "track_id": t["id"]}}
            for t in libdb.tracks_flagged_no_art(conn) if not _is_unsorted(t["library_path"])
        ]

        all_tracks = libdb.list_tracks(conn)
        missing_entries = [
            {"label": f"{t['title'] or Path(t['library_path']).stem}  —  {t['library_path']}",
             "data": {"track_id": t["id"], "library_path": t["library_path"]}}
            for t in _find_missing_files(library_root, all_tracks)
        ]

        known_paths = {t["library_path"] for t in all_tracks}
        orphan_entries = [
            {"label": rel_path, "data": {"abs_path": str(Path(library_root) / rel_path), "kind": kind}}
            for rel_path, kind in _find_orphaned_files(library_root, known_paths)
        ]
        conn.close()

        self._health_data = {
            "Duplicates": dupe_entries,
            "Missing Tags": tag_entries,
            "Missing Art": art_entries,
            "Missing Files": missing_entries,
            "Orphaned Files": orphan_entries,
        }
        self.health_stat_values["DUPLICATES"].setText(str(len(dupe_entries)))
        self.health_stat_values["MISSING TAGS"].setText(str(len(tag_entries)))
        self.health_stat_values["MISSING ART"].setText(str(len(art_entries)))
        self.health_stat_values["MISSING FILES"].setText(str(len(missing_entries)))
        self.health_stat_values["ORPHANED FILES"].setText(str(len(orphan_entries)))
        self._populate_health_list(self.health_category_combo.currentText())

    def refresh_health_if_active(self):
        """Called by MainWindow after the background art-check worker
        finishes, so Missing Art counts live-update if the user is already
        looking at this tab — without polling."""
        if self.sub_tabs.currentIndex() == HEALTH_TAB_INDEX:
            self._refresh_health()

    def _health_rescan(self):
        self._refresh_health()
        self.healthRescanRequested.emit()

    def _health_fix_tags(self):
        item = self.health_list.currentItem()
        if item is None:
            QMessageBox.information(self, "Nothing selected", "Click a track in the list first.")
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        self.fixTagsRequested.emit(data["path"], data["track_id"])

    def _health_remove_missing(self):
        items = self.health_list.selectedItems()
        if not items:
            QMessageBox.information(self, "Nothing selected", "Select one or more entries first.")
            return
        library_root = self._get_library_root()
        if not library_root:
            return
        conn = libdb.open_db(library_root)
        affected_playlist_ids = set()
        for item in items:
            data = item.data(Qt.ItemDataRole.UserRole)
            track_id = data["track_id"]
            affected_playlist_ids.update(libdb.playlists_containing_track(conn, track_id))
            libdb.delete_track(conn, track_id)  # file's already gone — no unlink needed
        for playlist_id in affected_playlist_ids:
            row = conn.execute("SELECT name FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
            if row:
                paths = libdb.playlist_track_paths_in_order(conn, playlist_id)
                write_m3u8(str(Path(library_root) / "Playlists"), row["name"], paths, library_root)
        conn.close()
        self.refresh()
        self._refresh_health()

    def _health_file_orphans(self):
        items = self.health_list.selectedItems()
        if not items:
            QMessageBox.information(self, "Nothing selected", "Select one or more entries first.")
            return
        library_root = self._get_library_root()
        if not library_root:
            return
        added = 0
        for item in items:
            data = item.data(Qt.ItemDataRole.UserRole)
            _, status = adopt_orphan_file(library_root, data["abs_path"], data["kind"])
            if status == "added":
                added += 1
        self.refresh()
        self._refresh_health()
        QMessageBox.information(self, "Filed", f"Added {added} of {len(items)} file(s) to the library.")


class AddTracksDialog(QDialog):
    """Search + multi-select existing library tracks to add to a playlist.
    Music only — playlists have been a music-only concept throughout this
    app; audiobooks live in their own tab, not playlists."""

    def __init__(self, parent, library_root: str, playlist_id: int):
        super().__init__(parent)
        self.library_root = library_root
        self.playlist_id = playlist_id
        self.added_count = 0
        self.setWindowTitle("Add Tracks")
        self.resize(480, 520)

        layout = QVBoxLayout(self)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search title, artist, album...")
        self.search_edit.textChanged.connect(self._reload)
        layout.addWidget(self.search_edit)

        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        layout.addWidget(self.list_widget, stretch=1)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton("CLOSE")
        cancel_btn.clicked.connect(self.reject)
        add_btn = QPushButton("ADD SELECTED")
        add_btn.setObjectName("primary")
        add_btn.clicked.connect(self._add_selected)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(add_btn)
        layout.addLayout(btn_row)

        self._reload()

    def _reload(self):
        query = self.search_edit.text().strip().lower()
        conn = libdb.open_db(self.library_root)
        existing_ids = libdb.playlist_track_ids(conn, self.playlist_id)
        tracks = libdb.list_tracks(conn, "music")
        conn.close()

        self.list_widget.clear()
        for t in tracks:
            if t["id"] in existing_ids:
                continue
            label = f"{t['title'] or Path(t['library_path']).stem}  —  {t['artist'] or ''}"
            if query and query not in label.lower() and query not in (t["album"] or "").lower():
                continue
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, t["id"])
            self.list_widget.addItem(item)

    def _add_selected(self):
        items = self.list_widget.selectedItems()
        if not items:
            QMessageBox.information(self, "Nothing selected", "Select one or more tracks first.")
            return
        conn = libdb.open_db(self.library_root)
        position = libdb.next_playlist_position(conn, self.playlist_id)
        for item in items:
            libdb.add_playlist_track(conn, self.playlist_id, item.data(Qt.ItemDataRole.UserRole), position)
            position += 1
        conn.close()
        self.added_count = len(items)
        self.accept()


class AddToPlaylistDialog(QDialog):
    """Pick an existing playlist (or create a new one on the fly) to add
    already-selected library tracks to — the inverse of AddTracksDialog
    (that one searches the library from inside a playlist; this searches
    playlists from inside the library). Same division of labor as
    AddTracksDialog: only mutates playlist_tracks here — the caller rewrites
    the affected .m3u8 afterward, since it already owns that logic for every
    other playlist mutation."""

    def __init__(self, parent, library_root: str, track_ids: list[int]):
        super().__init__(parent)
        self.library_root = library_root
        self.track_ids = track_ids
        self.added_count = 0
        self.target_playlist_id = None
        self.target_playlist_name = None
        self.setWindowTitle(f"Add {len(track_ids)} Track(s) to Playlist")
        self.resize(360, 420)

        layout = QVBoxLayout(self)
        hint = QLabel(f"Add {len(track_ids)} track(s) to a playlist.")
        hint.setObjectName("muted")
        layout.addWidget(hint)

        self.list_widget = QListWidget()
        layout.addWidget(self.list_widget, stretch=1)

        new_row = QHBoxLayout()
        self.new_name_edit = QLineEdit()
        self.new_name_edit.setPlaceholderText("New playlist name...")
        new_btn = QPushButton("+ CREATE")
        new_btn.clicked.connect(self._create_and_add)
        new_row.addWidget(self.new_name_edit, stretch=1)
        new_row.addWidget(new_btn)
        layout.addLayout(new_row)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton("CANCEL")
        cancel_btn.clicked.connect(self.reject)
        add_btn = QPushButton("ADD")
        add_btn.setObjectName("primary")
        add_btn.clicked.connect(self._add_to_selected)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(add_btn)
        layout.addLayout(btn_row)

        conn = libdb.open_db(library_root)
        for p in libdb.list_playlists(conn):
            item = QListWidgetItem(p["name"])
            item.setData(Qt.ItemDataRole.UserRole, p["id"])
            self.list_widget.addItem(item)
        conn.close()

    def _add_tracks_to(self, playlist_id: int, name: str):
        conn = libdb.open_db(self.library_root)
        existing_ids = libdb.playlist_track_ids(conn, playlist_id)
        position = libdb.next_playlist_position(conn, playlist_id)
        added = 0
        for track_id in self.track_ids:
            if track_id in existing_ids:
                continue
            libdb.add_playlist_track(conn, playlist_id, track_id, position)
            position += 1
            added += 1
        conn.close()
        self.added_count = added
        self.target_playlist_id = playlist_id
        self.target_playlist_name = name
        self.accept()

    def _add_to_selected(self):
        item = self.list_widget.currentItem()
        if item is None:
            QMessageBox.information(self, "Nothing selected", "Select a playlist, or create a new one below.")
            return
        self._add_tracks_to(item.data(Qt.ItemDataRole.UserRole), item.text())

    def _create_and_add(self):
        name = self.new_name_edit.text().strip()
        if not name:
            QMessageBox.information(self, "Name required", "Enter a name for the new playlist.")
            return
        conn = libdb.open_db(self.library_root)
        playlist_id = libdb.get_or_create_playlist(conn, name)
        conn.close()
        self._add_tracks_to(playlist_id, name)


class PlaylistView(QWidget):
    """Editable view of one playlist: drag to reorder, add tracks from the
    library, remove tracks, delete the whole playlist. Every mutation
    rewrites the .m3u8 immediately, keeping the on-disk file in sync with
    the DB on every change rather than only at migration-build time."""

    playlistsChanged = pyqtSignal()  # tells MainWindow to refresh the sidebar deck / library counts
    fixTagsRequested = pyqtSignal(str, int)  # mp3 path, track_id — same contract as LibraryView's
    playRequested = pyqtSignal(list, int)  # queue (dicts with id/title/artist/path), start index — same contract as LibraryView's

    def __init__(self, get_library_root):
        super().__init__()
        self._get_library_root = get_library_root
        self._playlist_id = None
        self._playlist_name = None
        self._mosaic_signature = None  # (playlist_id, first-4-track-paths) — see _load_artwork
        self._mosaic_cache = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(10)

        hero_row = QHBoxLayout()
        hero_row.setSpacing(16)
        self.art_tile = ChamferedArtTile(size=96, chamfer=16, clickable=True)
        self.art_tile.clicked.connect(self._choose_artwork)
        hero_row.addWidget(self.art_tile)

        meta_col = QVBoxLayout()
        meta_col.setSpacing(3)
        meta_col.addWidget(PixelLabel("PLAYLIST", point_size=9, color=MUTED, letter_spacing=2))
        self.title_label = PixelLabel("", point_size=16, letter_spacing=1)
        meta_col.addWidget(self.title_label)
        self.subtitle_label = QLabel("")
        self.subtitle_label.setObjectName("muted")
        meta_col.addWidget(self.subtitle_label)
        meta_col.addStretch(1)
        hero_row.addLayout(meta_col, stretch=1)
        layout.addLayout(hero_row)

        top_row = QHBoxLayout()
        add_btn = QPushButton("ADD TRACKS...")
        add_btn.clicked.connect(self._open_add_tracks_dialog)
        top_row.addWidget(add_btn)
        add_files_btn = QPushButton("ADD FILES...")
        add_files_btn.clicked.connect(self._open_add_files_dialog)
        top_row.addWidget(add_files_btn)
        top_row.addStretch(1)

        settings_btn = QPushButton("⚙")
        settings_btn.setToolTip("Playlist actions")
        settings_menu = QMenu(self)
        edit_tags_action = QAction("EDIT TAGS", self)
        edit_tags_action.triggered.connect(self._edit_tags_selected)
        remove_artwork_action = QAction("REMOVE ARTWORK", self)
        remove_artwork_action.triggered.connect(self._remove_artwork)
        remove_action = QAction("REMOVE SELECTED", self)
        remove_action.triggered.connect(self._remove_selected)
        copy_action = QAction("COPY SELECTED...", self)
        copy_action.triggered.connect(self._copy_selected)
        export_action = QAction("EXPORT...", self)
        export_action.triggered.connect(self._open_export_dialog)
        delete_action = QAction("DELETE PLAYLIST", self)
        delete_action.triggered.connect(self._delete_playlist)
        settings_menu.addAction(edit_tags_action)
        settings_menu.addAction(remove_artwork_action)
        settings_menu.addSeparator()
        settings_menu.addAction(remove_action)
        settings_menu.addAction(copy_action)
        settings_menu.addSeparator()
        settings_menu.addAction(export_action)
        settings_menu.addSeparator()
        settings_menu.addAction(delete_action)
        settings_btn.setMenu(settings_menu)
        top_row.addWidget(settings_btn)
        layout.addLayout(top_row)

        hint = QLabel("Drag tracks to reorder.")
        hint.setObjectName("muted")
        layout.addWidget(hint)

        self.track_list = TrackListWidget()
        self.track_list.setItemDelegate(TrackRowDelegate(self.track_list))
        self.track_list.setMouseTracking(True)
        self.track_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.track_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.track_list.model().rowsMoved.connect(lambda *a: self._persist_order())
        self.track_list.itemDoubleClicked.connect(self._play_from_playlist)
        self.track_list.infoClicked.connect(self._show_track_info_by_id)
        layout.addWidget(self.track_list, stretch=1)

    def show_playlist(self, playlist_id: int, name: str):
        self._playlist_id = playlist_id
        self._playlist_name = name
        self.title_label.setText(name.upper())
        self.refresh()

    def _library_root_or_none(self) -> str | None:
        library_root = self._get_library_root()
        if not library_root or not Path(library_root).exists():
            return None
        return library_root

    def refresh(self):
        if self._playlist_id is None:
            return
        library_root = self._library_root_or_none()
        self.track_list.clear()
        if not library_root:
            self._load_artwork()
            return
        conn = libdb.open_db(library_root)
        paths = libdb.playlist_track_paths_in_order(conn, self._playlist_id)
        total_seconds = 0.0
        track_count = 0
        for path in paths:
            row = conn.execute("SELECT * FROM tracks WHERE library_path = ?", (path,)).fetchone()
            if row:
                item = QListWidgetItem()
                item.setData(Qt.ItemDataRole.UserRole, {
                    "id": row["id"],
                    "library_path": path,
                    "title": row["title"] or Path(path).stem,
                    "artist": row["artist"] or "",
                    "badge": None,  # playlist order is the point here, not membership count
                    "loudness_lufs": row["loudness_lufs"],
                    "loudness_true_peak": row["loudness_true_peak"],
                })
                self.track_list.addItem(item)
                total_seconds += row["duration_seconds"] or 0.0
                track_count += 1
        conn.close()
        if track_count:
            label = "track" if track_count == 1 else "tracks"
            self.subtitle_label.setText(f"{track_count} {label} · {_format_total_playtime(total_seconds)}")
        else:
            self.subtitle_label.setText("No tracks yet")
        self._load_artwork()

    def _load_artwork(self):
        library_root = self._library_root_or_none()
        if not library_root or self._playlist_id is None:
            self.art_tile.clear_art()
            return
        art_path = find_playlist_artwork(library_root, self._playlist_id)
        if art_path:
            self.art_tile.set_art(art_path.read_bytes())
            return
        # No explicit artwork set — fall back to an auto-generated mosaic of
        # the playlist's own first few tracks' covers. Cached on a signature
        # of (playlist, first-4-track-paths) so reorders/adds elsewhere in
        # the playlist that don't touch the first 4 slots don't force a
        # re-read of every cover file on every refresh() call.
        conn = libdb.open_db(library_root)
        ordered_paths = libdb.playlist_track_paths_in_order(conn, self._playlist_id)
        conn.close()
        signature = (self._playlist_id, tuple(ordered_paths[:4]))
        if signature != self._mosaic_signature:
            self._mosaic_signature = signature
            self._mosaic_cache = playlist_mosaic_art(library_root, ordered_paths)
        self.art_tile.set_art(self._mosaic_cache)

    def _write_current_m3u8(self, library_root: str):
        conn = libdb.open_db(library_root)
        paths = libdb.playlist_track_paths_in_order(conn, self._playlist_id)
        conn.close()
        write_m3u8(str(Path(library_root) / "Playlists"), self._playlist_name, paths, library_root)

    def _persist_order(self):
        library_root = self._library_root_or_none()
        if not library_root or self._playlist_id is None:
            return
        track_ids = [self.track_list.item(i).data(Qt.ItemDataRole.UserRole)["id"] for i in range(self.track_list.count())]
        conn = libdb.open_db(library_root)
        libdb.reorder_playlist_tracks(conn, self._playlist_id, track_ids)
        conn.close()
        self._write_current_m3u8(library_root)
        self.playlistsChanged.emit()

    def _remove_selected(self):
        items = self.track_list.selectedItems()
        if not items:
            QMessageBox.information(self, "Nothing selected", "Select one or more tracks first.")
            return
        library_root = self._library_root_or_none()
        if not library_root:
            return
        conn = libdb.open_db(library_root)
        for item in items:
            libdb.remove_playlist_track(conn, self._playlist_id, item.data(Qt.ItemDataRole.UserRole)["id"])
        conn.close()
        self._write_current_m3u8(library_root)
        self.refresh()
        self.playlistsChanged.emit()

    def _copy_selected(self):
        items = self.track_list.selectedItems()
        if not items:
            QMessageBox.information(self, "Nothing selected", "Select one or more tracks first.")
            return
        library_root = self._library_root_or_none()
        if not library_root:
            return
        paths = [item.data(Qt.ItemDataRole.UserRole)["library_path"] for item in items]
        CopyTracksDialog(self, library_root, paths).exec()

    def _edit_tags_selected(self):
        items = self.track_list.selectedItems()
        if len(items) != 1:
            QMessageBox.information(self, "Select one track", "Select exactly one track to edit its tags.")
            return
        library_root = self._library_root_or_none()
        if not library_root:
            return
        data = items[0].data(Qt.ItemDataRole.UserRole)
        full_path = str(Path(library_root) / data["library_path"])
        self.fixTagsRequested.emit(full_path, data["id"])

    def _show_track_info_by_id(self, track_id: int):
        library_root = self._library_root_or_none()
        if not library_root:
            return
        conn = libdb.open_db(library_root)
        row = conn.execute("SELECT kind, library_path FROM tracks WHERE id = ?", (track_id,)).fetchone()
        conn.close()
        if not row:
            return
        dialog = TrackInfoDialog(self, library_root, track_id, row["kind"])
        dialog.exec()
        if dialog.edit_tags_requested:
            self.fixTagsRequested.emit(str(Path(library_root) / row["library_path"]), track_id)

    def _play_from_playlist(self, item: QListWidgetItem):
        library_root = self._library_root_or_none()
        if not library_root:
            return
        tracks = []
        for i in range(self.track_list.count()):
            data = self.track_list.item(i).data(Qt.ItemDataRole.UserRole)
            tracks.append({
                "id": data["id"],
                "title": data["title"],
                "artist": data["artist"],
                "path": str(Path(library_root) / data["library_path"]),
                "kind": "music",  # playlists are a music-only concept
                "loudness_lufs": data.get("loudness_lufs"),
                "loudness_true_peak": data.get("loudness_true_peak"),
            })
        self.playRequested.emit(tracks, self.track_list.row(item))

    def set_now_playing_track(self, track_id: int | None):
        self.track_list.set_playing_track(track_id)

    def _choose_artwork(self):
        if self._playlist_id is None:
            return
        library_root = self._library_root_or_none()
        if not library_root:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose playlist artwork", str(Path.home()), "Images (*.jpg *.jpeg *.png *.webp *.bmp)"
        )
        if not path:
            return
        set_playlist_artwork(library_root, self._playlist_id, path)
        self._load_artwork()
        self.playlistsChanged.emit()

    def _remove_artwork(self):
        if self._playlist_id is None:
            return
        library_root = self._library_root_or_none()
        if not library_root:
            return
        clear_playlist_artwork(library_root, self._playlist_id)
        self._load_artwork()
        self.playlistsChanged.emit()

    def _open_add_tracks_dialog(self):
        if self._playlist_id is None:
            return
        library_root = self._library_root_or_none()
        if not library_root:
            QMessageBox.information(self, "No library yet", "Add sources and build your library first.")
            return
        dialog = AddTracksDialog(self, library_root, self._playlist_id)
        if dialog.exec() and dialog.added_count:
            self._write_current_m3u8(library_root)
            self.refresh()
            self.playlistsChanged.emit()

    def _open_add_files_dialog(self):
        if self._playlist_id is None:
            return
        library_root = self._library_root_or_none()
        if not library_root:
            QMessageBox.information(self, "No library yet", "Add sources and build your library first.")
            return
        dialog = AddFilesDialog(self, library_root, default_playlist_id=self._playlist_id, lock_to_music=True)
        dialog.exec()
        if dialog.added_any:
            # AddFilesWorker already writes the target playlist's .m3u8
            # itself (batched, once) — no _write_current_m3u8 call needed here.
            self.refresh()
            self.playlistsChanged.emit()

    def _delete_playlist(self):
        if self._playlist_id is None:
            return
        library_root = self._library_root_or_none()
        if not library_root:
            return
        reply = QMessageBox.question(
            self, "Delete playlist",
            f"Delete '{self._playlist_name}'? This only removes the playlist itself — "
            f"none of your music files are touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        conn = libdb.open_db(library_root)
        libdb.delete_playlist(conn, self._playlist_id)
        conn.close()
        m3u8_path = Path(library_root) / "Playlists" / f"{self._playlist_name}.m3u8"
        if m3u8_path.exists():
            m3u8_path.unlink()
        clear_playlist_artwork(library_root, self._playlist_id)
        self._playlist_id = None
        self._playlist_name = None
        self.track_list.clear()
        self.title_label.setText("")
        self.subtitle_label.setText("")
        self.art_tile.clear_art()
        self.playlistsChanged.emit()

    def _open_export_dialog(self):
        if self._playlist_id is None:
            return
        library_root = self._get_library_root()
        if not library_root:
            return
        ExportDialog(self, library_root, preselected_playlist_id=self._playlist_id).exec()


class AudiobookSeriesView(QWidget):
    """One audiobook series, drilled into from the Audiobooks shelf — hero
    art (the series' own, separate from any individual book's cover) + a
    list of its books in Track # order. Structurally close to PlaylistView
    but simpler: no drag-reorder (book order is fixed by tag, not
    user-arranged), and REMOVE SELECTED really deletes from the library —
    audiobooks are never in a playlist, so there's no membership-only
    "remove" concept the way PlaylistView's REMOVE SELECTED has."""

    backRequested = pyqtSignal()
    playRequested = pyqtSignal(list, int)  # queue (dicts with id/title/artist/path/kind), start index
    fixTagsRequested = pyqtSignal(str, int)  # mp3 path, track_id — same contract as the other views'

    def __init__(self, get_library_root):
        super().__init__()
        self._get_library_root = get_library_root
        self._series_name = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(10)

        back_btn = QPushButton("‹ AUDIOBOOKS")
        back_btn.clicked.connect(self.backRequested.emit)
        layout.addWidget(back_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        hero_row = QHBoxLayout()
        hero_row.setSpacing(16)
        self.art_tile = ChamferedArtTile(size=96, chamfer=16, clickable=True)
        self.art_tile.clicked.connect(self._choose_artwork)
        hero_row.addWidget(self.art_tile)

        meta_col = QVBoxLayout()
        meta_col.setSpacing(3)
        meta_col.addWidget(PixelLabel("AUDIOBOOK SERIES", point_size=9, color=MUTED, letter_spacing=2))
        self.title_label = PixelLabel("", point_size=16, letter_spacing=1)
        meta_col.addWidget(self.title_label)
        self.subtitle_label = QLabel("")
        self.subtitle_label.setObjectName("muted")
        meta_col.addWidget(self.subtitle_label)
        meta_col.addStretch(1)
        hero_row.addLayout(meta_col, stretch=1)

        settings_btn = QPushButton("⚙")
        settings_btn.setToolTip("Series actions")
        settings_menu = QMenu(self)
        remove_artwork_action = QAction("REMOVE ARTWORK", self)
        remove_artwork_action.triggered.connect(self._remove_artwork)
        settings_menu.addAction(remove_artwork_action)
        settings_btn.setMenu(settings_menu)
        hero_row.addWidget(settings_btn)
        layout.addLayout(hero_row)

        hint = QLabel("Click a book's cover to play it and queue the rest of the series after it.")
        hint.setObjectName("muted")
        layout.addWidget(hint)

        self.track_list = TrackListWidget()
        self.track_list.setItemDelegate(TrackRowDelegate(self.track_list))
        self.track_list.setMouseTracking(True)
        self.track_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.track_list.infoClicked.connect(self._show_track_info_by_id)
        self.track_list.thumbnailClicked.connect(self._play_book)
        layout.addWidget(self.track_list, stretch=1)

        action_row = QHBoxLayout()
        action_row.addStretch(1)
        edit_tags_btn = QPushButton("EDIT TAGS")
        edit_tags_btn.clicked.connect(self._edit_tags_selected)
        action_row.addWidget(edit_tags_btn)
        remove_btn = QPushButton("REMOVE SELECTED")
        remove_btn.clicked.connect(self._remove_selected)
        action_row.addWidget(remove_btn)
        copy_btn = QPushButton("COPY SELECTED...")
        copy_btn.clicked.connect(self._copy_selected)
        action_row.addWidget(copy_btn)
        layout.addLayout(action_row)

    def show_series(self, series_name: str):
        self._series_name = series_name
        self.title_label.setText(series_name.upper())
        self.refresh()

    def _library_root_or_none(self) -> str | None:
        library_root = self._get_library_root()
        if not library_root or not Path(library_root).exists():
            return None
        return library_root

    def refresh(self):
        if self._series_name is None:
            return
        library_root = self._library_root_or_none()
        self.track_list.clear()
        if not library_root:
            self._load_artwork()
            return
        conn = libdb.open_db(library_root)
        books = sort_series_books(libdb.list_tracks_in_series(conn, self._series_name))
        conn.close()

        total_seconds = 0.0
        for book in books:
            full_path = str(Path(library_root) / book["library_path"])
            try:
                art_bytes = tagging.read_tags(full_path).get("art")
            except Exception:
                art_bytes = None
            book_num = _parse_book_number(book["tracknumber"])
            title = book["title"] or Path(book["library_path"]).stem
            display_title = f"{book_num}. {title}" if book_num is not None else title
            subtitle = f"{book['artist'] or 'Unknown Author'} · {_format_duration(book['duration_seconds'])}"
            item = QListWidgetItem()
            item.setIcon(QIcon(render_chamfered_pixmap(art_bytes, 40, 8)))
            item.setData(Qt.ItemDataRole.UserRole, {
                "id": book["id"],
                "library_path": book["library_path"],
                "title": display_title,
                "artist": subtitle,
                "badge": None,
                "loudness_lufs": book["loudness_lufs"],
                "loudness_true_peak": book["loudness_true_peak"],
            })
            self.track_list.addItem(item)
            total_seconds += book["duration_seconds"] or 0.0

        book_word = "book" if len(books) == 1 else "books"
        self.subtitle_label.setText(f"{len(books)} {book_word} · {_format_total_playtime(total_seconds)} total")
        self._load_artwork()

    def _load_artwork(self):
        library_root = self._library_root_or_none()
        if not library_root or self._series_name is None:
            self.art_tile.clear_art()
            return
        art_path = find_series_artwork(library_root, self._series_name)
        self.art_tile.set_art(art_path.read_bytes() if art_path else None)

    def _choose_artwork(self):
        if self._series_name is None:
            return
        library_root = self._library_root_or_none()
        if not library_root:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose series artwork", str(Path.home()), "Images (*.jpg *.jpeg *.png *.webp *.bmp)"
        )
        if not path:
            return
        set_series_artwork(library_root, self._series_name, path)
        self._load_artwork()

    def _remove_artwork(self):
        if self._series_name is None:
            return
        library_root = self._library_root_or_none()
        if not library_root:
            return
        clear_series_artwork(library_root, self._series_name)
        self._load_artwork()

    def _show_track_info_by_id(self, track_id: int):
        library_root = self._library_root_or_none()
        if not library_root:
            return
        conn = libdb.open_db(library_root)
        row = conn.execute("SELECT kind, library_path FROM tracks WHERE id = ?", (track_id,)).fetchone()
        conn.close()
        if not row:
            return
        dialog = TrackInfoDialog(self, library_root, track_id, row["kind"])
        dialog.exec()
        if dialog.edit_tags_requested:
            self.fixTagsRequested.emit(str(Path(library_root) / row["library_path"]), track_id)

    def _play_book(self, track_id: int):
        """Builds the queue from the full series (in order) so playback
        auto-advances into the next book — the caller clicked one specific
        book's cover, but the point of series ordering is continuing
        through it, not playing one book in isolation."""
        library_root = self._library_root_or_none()
        if not library_root:
            return
        start = None
        tracks = []
        for i in range(self.track_list.count()):
            data = self.track_list.item(i).data(Qt.ItemDataRole.UserRole)
            if data["id"] == track_id:
                start = i
            tracks.append({
                "id": data["id"],
                "title": data["title"],
                "artist": data["artist"],
                "path": str(Path(library_root) / data["library_path"]),
                "kind": "audiobook",
                "loudness_lufs": data.get("loudness_lufs"),
                "loudness_true_peak": data.get("loudness_true_peak"),
            })
        if start is not None:
            self.playRequested.emit(tracks, start)

    def set_now_playing_track(self, track_id: int | None):
        self.track_list.set_playing_track(track_id)

    def _edit_tags_selected(self):
        items = self.track_list.selectedItems()
        if len(items) != 1:
            QMessageBox.information(self, "Select one book", "Select exactly one book to edit its tags.")
            return
        library_root = self._library_root_or_none()
        if not library_root:
            return
        data = items[0].data(Qt.ItemDataRole.UserRole)
        full_path = str(Path(library_root) / data["library_path"])
        self.fixTagsRequested.emit(full_path, data["id"])

    def _remove_selected(self):
        items = self.track_list.selectedItems()
        if not items:
            QMessageBox.information(self, "Nothing selected", "Select one or more books first.")
            return
        library_root = self._library_root_or_none()
        if not library_root:
            return
        reply = QMessageBox.question(
            self, "Remove from library",
            f"Permanently delete {len(items)} book(s) from your library? This only removes "
            f"Splice's own copy — nothing at your original source location is touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        conn = libdb.open_db(library_root)
        failed = []
        for item in items:
            data = item.data(Qt.ItemDataRole.UserRole)
            path = Path(library_root) / data["library_path"]
            error = _try_unlink(path)
            if error:
                failed.append(path.name)
                continue  # leave its index entry in place — the file's still really there
            libdb.delete_track(conn, data["id"])  # audiobooks are never in playlists — no m3u8 cleanup needed
        conn.close()
        self.refresh()
        if failed:
            QMessageBox.warning(
                self, "Some books couldn't be removed",
                "These are currently in use by another program, so they weren't removed "
                "(nothing changed for them — try again once whatever has them open is closed):\n\n"
                + "\n".join(failed),
            )

    def _copy_selected(self):
        items = self.track_list.selectedItems()
        if not items:
            QMessageBox.information(self, "Nothing selected", "Select one or more books first.")
            return
        library_root = self._library_root_or_none()
        if not library_root:
            return
        paths = [item.data(Qt.ItemDataRole.UserRole)["library_path"] for item in items]
        CopyTracksDialog(self, library_root, paths).exec()
