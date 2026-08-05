"""The Library view — the app's home screen: browse/search Songs, Audiobooks,
Duplicates, and Unsorted, and the ADD SOURCES migration wizard that builds or
extends the library from existing folders."""

from pathlib import Path

from PyQt6.QtCore import QSettings, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
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
from .library.export import (
    CopyReport,
    CopyTracksWorker,
    ExportItem,
    ExportReport,
    ExportWorker,
    list_audiobook_entries,
)
from .library.migrate import LibraryBuildWorker, LibraryScanWorker, MusicSource
from .library.playlists import write_m3u8
from .styles import INK, MUTED, ORANGE
from .widgets import ChamferedFrame, LevelGauge, PixelLabel
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
    table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
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
        title_item = _readonly_item(t["title"] or Path(t["library_path"]).stem)
        title_item.setData(Qt.ItemDataRole.UserRole, {"id": t["id"], "library_path": t["library_path"]})
        table.setItem(row, 0, title_item)
        table.setItem(row, 1, _readonly_item(t["artist"] or ""))
        table.setItem(row, 2, _NumericItem(membership.get(t["id"], 0)))
    table.setSortingEnabled(True)


def selected_library_paths(table: QTableWidget) -> list[str]:
    """library_path for each fully-selected row, in visual (possibly sorted)
    order — used by "COPY SELECTED..." on the Songs/Audiobooks tabs."""
    paths = []
    for index in table.selectionModel().selectedRows(0):
        data = table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole)
        if data:
            paths.append(data["library_path"])
    return paths


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

        stats_row = QHBoxLayout()
        stats_row.setSpacing(10)
        self.stat_songs_value = QLabel("0")
        self.stat_dupes_value = QLabel("0")
        self.stat_audiobooks_value = QLabel("0")
        stats_row.addWidget(self._make_stat_card("SONGS", self.stat_songs_value, MUTED))
        stats_row.addWidget(self._make_stat_card("DUPLICATES", self.stat_dupes_value, ORANGE))
        stats_row.addWidget(self._make_stat_card("AUDIOBOOKS", self.stat_audiobooks_value, MUTED))
        layout.addLayout(stats_row)

        top_row = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search title, artist, album...")
        self.search_edit.textChanged.connect(lambda: self._search_debounce.start())
        top_row.addWidget(self.search_edit, stretch=1)

        settings_btn = QPushButton("⚙")
        settings_btn.setFixedWidth(34)
        settings_btn.setToolTip("Library settings")
        settings_menu = QMenu(self)
        add_sources_action = QAction("ADD SOURCES...", self)
        add_sources_action.triggered.connect(self._open_migration_dialog)
        export_action = QAction("EXPORT...", self)
        export_action.triggered.connect(self._open_export_dialog)
        settings_menu.addAction(add_sources_action)
        settings_menu.addAction(export_action)
        settings_btn.setMenu(settings_menu)
        top_row.addWidget(settings_btn)
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

        self.copy_actions = QWidget()
        copy_row = QHBoxLayout(self.copy_actions)
        copy_row.setContentsMargins(0, 0, 0, 0)
        copy_row.addStretch(1)
        copy_selected_btn = QPushButton("COPY SELECTED...")
        copy_selected_btn.clicked.connect(self._copy_selected_tracks)
        copy_row.addWidget(copy_selected_btn)
        layout.addWidget(self.copy_actions)

        self.dupe_actions = QWidget()
        dupe_row = QHBoxLayout(self.dupe_actions)
        dupe_row.setContentsMargins(0, 0, 0, 0)
        dupe_row.addStretch(1)
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
        layout.addWidget(self.unsorted_actions)

        self.sub_tabs.currentChanged.connect(self._update_sub_tab_actions)
        self._update_sub_tab_actions(self.sub_tabs.currentIndex())

        self.empty_hint = QLabel("")
        self.empty_hint.setObjectName("muted")
        self.empty_hint.setWordWrap(True)
        layout.addWidget(self.empty_hint)

        self.refresh()

    def _make_stat_card(self, label_text: str, value_label: QLabel, label_color: str) -> ChamferedFrame:
        frame = ChamferedFrame(chamfer=12)
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(14, 12, 14, 12)
        inner.setSpacing(8)
        inner.addWidget(PixelLabel(label_text, font_size=7, scale=2.2, color=label_color, letter_spacing=2))
        value_label.setStyleSheet(f"font-size: 22px; font-weight: bold; color: {INK};")
        inner.addWidget(value_label)
        return frame

    def _update_sub_tab_actions(self, index: int):
        self.copy_actions.setVisible(index in (0, 1))  # SONGS, AUDIOBOOKS
        self.dupe_actions.setVisible(index == 2)  # DUPLICATES
        self.unsorted_actions.setVisible(index == 3)  # UNSORTED

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

        populate_track_table(self.songs_table, self._filter(music_tracks, query), membership)
        populate_track_table(self.audiobooks_table, self._filter(audiobook_tracks, query), membership)
        self._populate_unsorted(unsorted_tracks)
        self._populate_dupes(conn, dupes)

        self.sub_tabs.setTabText(3, f"UNSORTED ({len(unsorted_tracks)})")
        self.empty_hint.setText(
            "" if (music_tracks or audiobook_tracks) else "No library yet — click ADD SOURCES to get started."
        )
        self.stat_songs_value.setText(str(len(music_tracks)))
        self.stat_dupes_value.setText(str(len(dupes)))
        self.stat_audiobooks_value.setText(str(len(audiobook_tracks)))
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
        self.stat_songs_value.setText("0")
        self.stat_dupes_value.setText("0")
        self.stat_audiobooks_value.setText("0")

    # ---- actions ----

    def _copy_selected_tracks(self):
        table = self.songs_table if self.sub_tabs.currentIndex() == 0 else self.audiobooks_table
        paths = selected_library_paths(table)
        if not paths:
            QMessageBox.information(self, "Nothing selected", "Select one or more tracks first.")
            return
        library_root = self._get_library_root()
        if not library_root:
            return
        CopyTracksDialog(self, library_root, paths).exec()

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


class PlaylistView(QWidget):
    """Editable view of one playlist: drag to reorder, add tracks from the
    library, remove tracks, delete the whole playlist. Every mutation
    rewrites the .m3u8 immediately, keeping the on-disk file in sync with
    the DB on every change rather than only at migration-build time."""

    playlistsChanged = pyqtSignal()  # tells MainWindow to refresh the sidebar deck / library counts

    def __init__(self, get_library_root):
        super().__init__()
        self._get_library_root = get_library_root
        self._playlist_id = None
        self._playlist_name = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(10)

        self.title_label = QLabel("PLAYLIST")
        self.title_label.setObjectName("panelTitle")
        layout.addWidget(self.title_label)

        top_row = QHBoxLayout()
        add_btn = QPushButton("ADD TRACKS...")
        add_btn.clicked.connect(self._open_add_tracks_dialog)
        remove_btn = QPushButton("REMOVE SELECTED")
        remove_btn.clicked.connect(self._remove_selected)
        copy_btn = QPushButton("COPY SELECTED...")
        copy_btn.clicked.connect(self._copy_selected)
        top_row.addWidget(add_btn)
        top_row.addWidget(remove_btn)
        top_row.addWidget(copy_btn)
        top_row.addStretch(1)
        export_btn = QPushButton("EXPORT...")
        export_btn.clicked.connect(self._open_export_dialog)
        delete_btn = QPushButton("DELETE PLAYLIST")
        delete_btn.clicked.connect(self._delete_playlist)
        top_row.addWidget(export_btn)
        top_row.addWidget(delete_btn)
        layout.addLayout(top_row)

        hint = QLabel("Drag tracks to reorder.")
        hint.setObjectName("muted")
        layout.addWidget(hint)

        self.track_list = QListWidget()
        self.track_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.track_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.track_list.model().rowsMoved.connect(lambda *a: self._persist_order())
        layout.addWidget(self.track_list, stretch=1)

    def show_playlist(self, playlist_id: int, name: str):
        self._playlist_id = playlist_id
        self._playlist_name = name
        self.title_label.setText(f"PLAYLIST — {name}")
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
            return
        conn = libdb.open_db(library_root)
        paths = libdb.playlist_track_paths_in_order(conn, self._playlist_id)
        for path in paths:
            row = conn.execute("SELECT * FROM tracks WHERE library_path = ?", (path,)).fetchone()
            if row:
                item = QListWidgetItem(f"{row['title'] or Path(path).stem}  —  {row['artist'] or ''}")
                item.setData(Qt.ItemDataRole.UserRole, {"id": row["id"], "library_path": path})
                self.track_list.addItem(item)
        conn.close()

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
        self._playlist_id = None
        self._playlist_name = None
        self.track_list.clear()
        self.title_label.setText("PLAYLIST")
        self.playlistsChanged.emit()

    def _open_export_dialog(self):
        if self._playlist_id is None:
            return
        library_root = self._get_library_root()
        if not library_root:
            return
        ExportDialog(self, library_root, preselected_playlist_id=self._playlist_id).exec()
