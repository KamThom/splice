"""A playlist's page: header, editable track table, actions."""

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..dialogs.export import CopyTracksDialog, ExportDialog
from ..dialogs.sources import AddFilesDialog
from ..dialogs.tracks import AddTracksDialog, TrackInfoDialog
from ..library import db as libdb
from ..library.playlists import (
    clear_playlist_artwork,
    find_playlist_artwork,
    set_playlist_artwork,
    write_m3u8,
)
from ..library_actions import SelectionBar, add_menu_action, attach_context_menu
from ..styles import MUTED
from ..track_lists import (
    PLAYLIST_COLUMNS,
    _format_length,
    _format_total_playtime,
    playlist_mosaic_art,
)
from ..widgets import (
    DisplayLabel,
    RoundedArtTile,
    TrackColumnHeader,
    TrackListWidget,
    TrackRowDelegate,
)


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
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        hero_row = QHBoxLayout()
        hero_row.setSpacing(16)
        self.art_tile = RoundedArtTile(size=96, radius=16, clickable=True)
        self.art_tile.clicked.connect(self._choose_artwork)
        hero_row.addWidget(self.art_tile)

        meta_col = QVBoxLayout()
        meta_col.setSpacing(3)
        meta_col.addWidget(DisplayLabel("Playlist", point_size=10, color=MUTED, letter_spacing=1))
        self.title_label = DisplayLabel("", point_size=20, letter_spacing=0)
        meta_col.addWidget(self.title_label)
        self.subtitle_label = QLabel("")
        self.subtitle_label.setObjectName("muted")
        meta_col.addWidget(self.subtitle_label)
        meta_col.addStretch(1)
        hero_row.addLayout(meta_col, stretch=1)
        layout.addLayout(hero_row)

        top_row = QHBoxLayout()
        add_btn = QPushButton("Add tracks…")
        add_btn.clicked.connect(self._open_add_tracks_dialog)
        top_row.addWidget(add_btn)
        add_files_btn = QPushButton("Add files…")
        add_files_btn.clicked.connect(self._open_add_files_dialog)
        top_row.addWidget(add_files_btn)
        top_row.addStretch(1)

        settings_btn = QPushButton("⚙")
        settings_btn.setToolTip("Playlist actions")
        settings_menu = QMenu(self)
        edit_tags_action = QAction("Edit tags", self)
        edit_tags_action.triggered.connect(self._edit_tags_selected)
        remove_artwork_action = QAction("Remove artwork", self)
        remove_artwork_action.triggered.connect(self._remove_artwork)
        remove_action = QAction("Remove selected from playlist", self)
        remove_action.triggered.connect(self._remove_selected)
        copy_action = QAction("Copy selected…", self)
        copy_action.triggered.connect(self._copy_selected)
        export_action = QAction("Export…", self)
        export_action.triggered.connect(self._open_export_dialog)
        delete_action = QAction("Delete playlist", self)
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


        self.track_list = TrackListWidget(PLAYLIST_COLUMNS)
        self.track_list.setItemDelegate(TrackRowDelegate(self.track_list))
        self.track_list.setMouseTracking(True)
        self.track_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.track_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.track_list.model().rowsMoved.connect(lambda *a: self._persist_order())
        self.track_list.itemDoubleClicked.connect(self._play_from_playlist)
        self.track_list.infoClicked.connect(self._show_track_info_by_id)
        # Order here is the playlist's own (drag to reorder), so the header
        # labels the columns but doesn't sort.
        self.track_list.model().rowsMoved.connect(lambda *a: self._renumber())
        layout.addWidget(TrackColumnHeader(self.track_list, set(), show_in=False))
        layout.addWidget(self.track_list, stretch=1)

        bar = SelectionBar([self.track_list], hint="Drag to reorder. Select tracks to act on them, or right-click one")
        bar.add_button("Edit tags", self._edit_tags_selected)
        bar.add_button("Copy…", self._copy_selected)
        bar.add_button("Remove from playlist", self._remove_selected)
        layout.addWidget(bar)

        def track_menu(menu, item):
            data = item.data(Qt.ItemDataRole.UserRole) or {}
            if len(self.track_list.selectedItems()) == 1:
                add_menu_action(menu, "Play from here", lambda: self._play_from_playlist(item))
                add_menu_action(menu, "Edit tags", self._edit_tags_selected)
                add_menu_action(menu, "Track info", lambda: self._show_track_info_by_id(data["id"]))
            add_menu_action(menu, "Copy…", self._copy_selected)
            menu.addSeparator()
            add_menu_action(menu, "Remove from playlist", self._remove_selected)
        attach_context_menu(self.track_list, track_menu)

    def _renumber(self):
        for i in range(self.track_list.count()):
            item = self.track_list.item(i)
            data = dict(item.data(Qt.ItemDataRole.UserRole) or {})
            data["pos"] = str(i + 1)
            item.setData(Qt.ItemDataRole.UserRole, data)

    def show_playlist(self, playlist_id: int, name: str):
        self._playlist_id = playlist_id
        self._playlist_name = name
        self.title_label.setText(name)
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
                    "pos": str(track_count + 1),
                    "title": row["title"] or Path(path).stem,
                    "artist": row["artist"] or "",
                    "album": row["album"] or "",
                    "time": _format_length(row["duration_seconds"]),
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
