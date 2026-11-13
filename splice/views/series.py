"""An audiobook series' page: books in series order, play, delete."""

from pathlib import Path

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QAction, QIcon
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

from .. import tagging
from ..dialogs.export import CopyTracksDialog
from ..dialogs.tracks import TrackInfoDialog
from ..library import db as libdb
from ..library.series_artwork import (
    clear_series_artwork,
    find_series_artwork,
    set_series_artwork,
)
from ..library_actions import (
    SelectionBar,
    add_menu_action,
    attach_context_menu,
    delete_audiobook_series,
    delete_library_tracks,
)
from ..styles import MUTED
from ..track_lists import (
    _format_length,
    _format_total_playtime,
    _parse_book_number,
    sort_series_books,
)
from ..widgets import (
    DisplayLabel,
    RoundedArtTile,
    TrackListWidget,
    TrackRowDelegate,
    render_rounded_pixmap,
)


class AudiobookSeriesView(QWidget):
    """One audiobook series, drilled into from the Audiobooks shelf — hero
    art (the series' own, separate from any individual book's cover) + a
    list of its books in Track # order. Structurally close to PlaylistView
    but simpler: no drag-reorder (book order is fixed by tag, not
    user-arranged), and REMOVE SELECTED really deletes from the library —
    audiobooks are never in a playlist, so there's no membership-only
    "remove" concept the way PlaylistView's REMOVE SELECTED has."""

    backRequested = pyqtSignal()
    libraryChanged = pyqtSignal()  # books were deleted — MainWindow refreshes the library/shelf counts
    playRequested = pyqtSignal(list, int)  # queue (dicts with id/title/artist/path/kind), start index
    fixTagsRequested = pyqtSignal(str, int)  # mp3 path, track_id — same contract as the other views'

    def __init__(self, get_library_root):
        super().__init__()
        self._get_library_root = get_library_root
        self._series_name = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        back_btn = QPushButton("‹ Audiobooks")
        back_btn.clicked.connect(self.backRequested.emit)
        layout.addWidget(back_btn, alignment=Qt.AlignmentFlag.AlignLeft)

        hero_row = QHBoxLayout()
        hero_row.setSpacing(16)
        self.art_tile = RoundedArtTile(size=96, radius=16, clickable=True)
        self.art_tile.clicked.connect(self._choose_artwork)
        hero_row.addWidget(self.art_tile)

        meta_col = QVBoxLayout()
        meta_col.setSpacing(3)
        meta_col.addWidget(DisplayLabel("Audiobook series", point_size=10, color=MUTED, letter_spacing=1))
        self.title_label = DisplayLabel("", point_size=20, letter_spacing=0)
        meta_col.addWidget(self.title_label)
        self.subtitle_label = QLabel("")
        self.subtitle_label.setObjectName("muted")
        meta_col.addWidget(self.subtitle_label)
        meta_col.addStretch(1)
        hero_row.addLayout(meta_col, stretch=1)

        settings_btn = QPushButton("⚙")
        settings_btn.setToolTip("Series actions")
        settings_menu = QMenu(self)
        remove_artwork_action = QAction("Remove artwork", self)
        remove_artwork_action.triggered.connect(self._remove_artwork)
        settings_menu.addAction(remove_artwork_action)
        settings_menu.addSeparator()
        delete_series_action = QAction("Delete series…", self)
        delete_series_action.triggered.connect(self._delete_series)
        settings_menu.addAction(delete_series_action)
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
        self.track_list.enable_file_drag(self._get_library_root)
        layout.addWidget(self.track_list, stretch=1)

        bar = SelectionBar([self.track_list])
        bar.add_button("Edit tags", self._edit_tags_selected)
        bar.add_button("Copy…", self._copy_selected)
        bar.add_button("Delete…", self._remove_selected, danger=True)
        layout.addWidget(bar)

        def book_menu(menu, item):
            data = item.data(Qt.ItemDataRole.UserRole) or {}
            if len(self.track_list.selectedItems()) == 1:
                add_menu_action(menu, "Play from here", lambda: self._play_book(data["id"]))
                add_menu_action(menu, "Edit tags", self._edit_tags_selected)
                add_menu_action(menu, "Track info", lambda: self._show_track_info_by_id(data["id"]))
            add_menu_action(menu, "Copy…", self._copy_selected)
            menu.addSeparator()
            add_menu_action(menu, "Delete…", self._remove_selected, danger=True)
        attach_context_menu(self.track_list, book_menu)

    def show_series(self, series_name: str):
        self._series_name = series_name
        self.title_label.setText(series_name)
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
            subtitle = f"{book['artist'] or 'Unknown Author'} · {_format_length(book['duration_seconds']) or '—'}"
            item = QListWidgetItem()
            item.setIcon(QIcon(render_rounded_pixmap(art_bytes, 40, 8)))
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
        tracks = [(i.data(Qt.ItemDataRole.UserRole)["id"], i.data(Qt.ItemDataRole.UserRole)["library_path"])
                  for i in items]
        if delete_library_tracks(self, library_root, tracks, noun="book") > 0:
            self.refresh()
            self.libraryChanged.emit()
            if self.track_list.count() == 0:
                self.backRequested.emit()  # nothing left in this series

    def _delete_series(self):
        library_root = self._library_root_or_none()
        if not library_root or self._series_name is None:
            return
        if delete_audiobook_series(self, library_root, self._series_name) > 0:
            self.libraryChanged.emit()
            self.refresh()
            if self.track_list.count() == 0:
                self.backRequested.emit()

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
