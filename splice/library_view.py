"""The Library view — the app's home screen: browse/search Songs, Audiobooks,
Duplicates, and Unsorted, and the ADD SOURCES migration wizard that builds or
extends the library from existing folders."""

from pathlib import Path

from PyQt6.QtCore import QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from .library import db as libdb
from .library.migrate import LibraryBuildWorker, LibraryScanWorker, MusicSource
from .widgets import LevelGauge


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


class _NumericItem(QTableWidgetItem):
    """QTableWidgetItem that sorts by an int value instead of its display text."""

    def __init__(self, value: int):
        super().__init__(str(value))
        self._value = value
        self.setFlags(self.flags() & ~Qt.ItemFlag.ItemIsEditable)

    def __lt__(self, other):
        if isinstance(other, _NumericItem):
            return self._value < other._value
        return super().__lt__(other)


def _readonly_item(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


def _is_unsorted(library_path: str) -> bool:
    return "_Unsorted" in Path(library_path).parts


def make_track_table() -> QTableWidget:
    """Read-only Title/Artist/In table, shared by the Library view's Songs and
    Audiobooks tabs and by PlaylistView."""
    table = QTableWidget(0, 3)
    table.setHorizontalHeaderLabels(["Title", "Artist", "In"])
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    table.setSortingEnabled(True)
    table.verticalHeader().setVisible(False)
    header = table.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
    header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
    header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
    return table


def populate_track_table(table: QTableWidget, tracks, membership: dict):
    table.setSortingEnabled(False)
    table.setRowCount(len(tracks))
    for row, t in enumerate(tracks):
        table.setItem(row, 0, _readonly_item(t["title"] or Path(t["library_path"]).stem))
        table.setItem(row, 1, _readonly_item(t["artist"] or ""))
        table.setItem(row, 2, _NumericItem(membership.get(t["id"], 0)))
    table.setSortingEnabled(True)


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


class LibraryView(QWidget):
    """The app's home screen. get_library_root/set_library_root let this view
    share the persisted library location with MainWindow without owning
    QSettings itself."""

    fixTagsRequested = pyqtSignal(str, int)  # mp3 path, track_id — Unsorted tab's "Fix Tags" action
    libraryChanged = pyqtSignal()  # emitted after every refresh — sidebar playlist list listens to this
    libraryLocationChanged = pyqtSignal(str)  # new library_root — MainWindow logs this to the console

    def __init__(self, get_library_root, set_library_root):
        super().__init__()
        self._get_library_root = get_library_root
        self._set_library_root = set_library_root
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

        top_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search title, artist, album...")
        self.search_edit.textChanged.connect(lambda: self._search_debounce.start())
        top_row.addWidget(self.search_edit, stretch=1)
        add_sources_btn = QPushButton("ADD SOURCES...")
        add_sources_btn.setObjectName("primary")
        add_sources_btn.clicked.connect(self._open_migration_dialog)
        top_row.addWidget(add_sources_btn)
        layout.addLayout(top_row)

        self.sub_tabs = QTabWidget()
        self.songs_table = make_track_table()
        self.audiobooks_table = make_track_table()
        self.duplicates_list = QListWidget()
        self.unsorted_list = QListWidget()

        self.sub_tabs.addTab(self.songs_table, "SONGS")
        self.sub_tabs.addTab(self.audiobooks_table, "AUDIOBOOKS")
        self.sub_tabs.addTab(self.duplicates_list, "DUPLICATES")
        self.sub_tabs.addTab(self.unsorted_list, "UNSORTED")
        layout.addWidget(self.sub_tabs, stretch=1)

        dupe_row = QHBoxLayout()
        dupe_row.addStretch(1)
        dismiss_btn = QPushButton("DISMISS SELECTED")
        dismiss_btn.clicked.connect(self._dismiss_selected_dupe)
        dupe_row.addWidget(dismiss_btn)
        layout.addLayout(dupe_row)

        unsorted_row = QHBoxLayout()
        unsorted_row.addStretch(1)
        fix_tags_btn = QPushButton("FIX TAGS")
        fix_tags_btn.clicked.connect(self._fix_selected_unsorted_tags)
        unsorted_row.addWidget(fix_tags_btn)
        layout.addLayout(unsorted_row)

        self.empty_hint = QLabel("")
        self.empty_hint.setObjectName("muted")
        self.empty_hint.setWordWrap(True)
        layout.addWidget(self.empty_hint)

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

        populate_track_table(self.songs_table, self._filter(music_tracks, query), membership)
        populate_track_table(self.audiobooks_table, self._filter(audiobook_tracks, query), membership)
        self._populate_unsorted(unsorted_tracks)
        self._populate_dupes(conn, dupes)

        self.sub_tabs.setTabText(3, f"UNSORTED ({len(unsorted_tracks)})")
        self.empty_hint.setText(
            "" if (music_tracks or audiobook_tracks) else "No library yet — click ADD SOURCES to get started."
        )
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
        self.songs_table.setRowCount(0)
        self.audiobooks_table.setRowCount(0)
        self.duplicates_list.clear()
        self.unsorted_list.clear()
        self.sub_tabs.setTabText(3, "UNSORTED (0)")

    # ---- actions ----

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

    def _fix_selected_unsorted_tags(self):
        item = self.unsorted_list.currentItem()
        if item is None:
            QMessageBox.information(self, "Nothing selected", "Click a file in the list first.")
            return
        data = item.data(Qt.ItemDataRole.UserRole)
        self.fixTagsRequested.emit(data["path"], data["track_id"])


class PlaylistView(QWidget):
    """Read-only view of one playlist's contents — the same track table as the
    Library view, filtered to just that playlist. No editing yet (drag reorder,
    add/remove tracks) — that's a separate, later phase."""

    def __init__(self, get_library_root):
        super().__init__()
        self._get_library_root = get_library_root
        self._playlist_id = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(10)

        self.title_label = QLabel("PLAYLIST")
        self.title_label.setObjectName("panelTitle")
        layout.addWidget(self.title_label)

        self.table = make_track_table()
        layout.addWidget(self.table, stretch=1)

    def show_playlist(self, playlist_id: int, name: str):
        self._playlist_id = playlist_id
        self.title_label.setText(f"PLAYLIST — {name}")
        self.refresh()

    def refresh(self):
        if self._playlist_id is None:
            return
        library_root = self._get_library_root()
        if not library_root:
            return
        conn = libdb.open_db(library_root)
        paths = libdb.playlist_track_paths_in_order(conn, self._playlist_id)
        tracks = []
        for path in paths:
            row = conn.execute("SELECT * FROM tracks WHERE library_path = ?", (path,)).fetchone()
            if row:
                tracks.append(row)
        membership = libdb.playlist_membership_counts(conn)
        populate_track_table(self.table, tracks, membership)
        conn.close()
