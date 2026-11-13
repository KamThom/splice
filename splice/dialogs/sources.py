"""Getting music into the library: the Add sources migration wizard and
the Add files dialog."""

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTreeView,
    QVBoxLayout,
)

from ..library import db as libdb
from ..library.migrate import (
    AddFilesWorker,
    LibraryBuildWorker,
    LibraryScanWorker,
    MusicSource,
)
from ..widgets import (
    LevelGauge,
)


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
        add_music_btn = QPushButton("+ Add music playlist folder(s)")
        add_music_btn.clicked.connect(self._add_music_source)
        remove_btn = QPushButton("Remove selected")
        remove_btn.clicked.connect(self._remove_selected_source)
        master_btn = QPushButton("Set as master")
        master_btn.clicked.connect(self._set_selected_as_master)
        source_btn_row.addWidget(add_music_btn)
        source_btn_row.addWidget(remove_btn)
        source_btn_row.addWidget(master_btn)
        layout.addLayout(source_btn_row)

        audiobooks_row = QHBoxLayout()
        self.audiobooks_label = QLabel("Audiobooks folder: (none)")
        self.audiobooks_label.setObjectName("muted")
        audiobooks_btn = QPushButton("Set audiobooks folder")
        audiobooks_btn.clicked.connect(self._choose_audiobooks_folder)
        audiobooks_row.addWidget(self.audiobooks_label, stretch=1)
        audiobooks_row.addWidget(audiobooks_btn)
        layout.addLayout(audiobooks_row)

        action_row = QHBoxLayout()
        self.scan_btn = QPushButton("Scan")
        self.scan_btn.clicked.connect(self._run_scan)
        self.build_btn = QPushButton("Build library")
        self.build_btn.setObjectName("primary")
        self.build_btn.setEnabled(False)
        self.build_btn.clicked.connect(self._run_build)
        action_row.addWidget(self.scan_btn)
        action_row.addStretch(1)
        action_row.addWidget(self.build_btn)
        layout.addLayout(action_row)

        self.gauge = LevelGauge()
        layout.addWidget(self.gauge)

        self.report_label = QLabel("Scan is read-only — nothing is copied until you click Build library.")
        self.report_label.setObjectName("muted")
        self.report_label.setWordWrap(True)
        layout.addWidget(self.report_label)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        layout.addWidget(self.log_box, stretch=1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        close_btn = QPushButton("Close")
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
        self.gauge.set_done("Scan complete")
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
        self.gauge.set_done("Build complete")
        self.build_succeeded = True
        self.scan_btn.setEnabled(True)
        text = (
            f"Library updated: {report.added_count} added, {report.already_in_library_count} "
            f"already in library, {report.unreadable_count} unreadable, {report.failed_count} failed."
        )
        if report.unreadable_count or report.failed_count:
            text += " See the log below for which files and why."
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
        choose_btn = QPushButton("+ Choose files…")
        choose_btn.clicked.connect(self._choose_files)
        remove_btn = QPushButton("Remove selected")
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
        self.add_btn = QPushButton("Add")
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
        close_btn = QPushButton("Close")
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
        self.gauge.set_done("Add complete")
        self.add_btn.setEnabled(True)
        self.report_label.setText(f"{added_count} of {self.file_list.count()} file(s) added.")
        if added_count:
            self.added_any = True
        self.file_list.clear()
