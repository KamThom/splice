"""Per-track dialogs: Track info, Add tracks (to a playlist, from the
library), and Add to playlist."""

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from .. import tagging
from ..library import db as libdb
from ..library import loudness
from ..styles import ACCENT, MUTED, RED
from ..track_lists import _format_added_date, _format_duration, _format_filesize
from ..widgets import (
    DisplayLabel,
    RoundedArtTile,
)


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

        header_title = QLabel("Track info")
        header_title.setObjectName("panelTitle")
        layout.addWidget(header_title)

        body = QHBoxLayout()
        body.setSpacing(20)
        body.addLayout(self._build_left_pane(row, full_path, art_bytes))
        body.addLayout(self._build_right_pane(row, kind, art_bytes, playlist_rows), stretch=1)
        layout.addLayout(body)

        footer = QHBoxLayout()
        footer.addStretch(1)
        edit_btn = QPushButton("Edit tags")
        edit_btn.clicked.connect(self._request_edit_tags)
        footer.addWidget(edit_btn)
        close_btn = QPushButton("Close")
        close_btn.setObjectName("primary")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(close_btn)
        layout.addLayout(footer)

    def _build_left_pane(self, row, full_path: Path, art_bytes: bytes | None) -> QVBoxLayout:
        col = QVBoxLayout()
        art = RoundedArtTile(size=168, radius=16, clickable=False)
        art.set_art(art_bytes)
        col.addWidget(art)
        col.addLayout(self._field("Duration", _format_duration(row["duration_seconds"])))
        col.addLayout(self._field("File size", _format_filesize(full_path)))
        col.addLayout(self._field("Added", _format_added_date(row["added_at"])))
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
            heading_row.addWidget(DisplayLabel("Missing art", point_size=8, color=RED, letter_spacing=1))
        col.addLayout(heading_row)

        rule1 = QFrame()
        rule1.setObjectName("rule")
        col.addWidget(rule1)

        row1 = QHBoxLayout()
        row1.addLayout(self._field("Album", row["album"] or "—"))
        row1.addLayout(self._field("Date", row["date"] or "—"))
        col.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addLayout(self._field("TRACK #", row["tracknumber"] or "—"))
        row2.addLayout(self._field("Loudness", self._loudness_text(row, kind)))
        col.addLayout(row2)

        col.addLayout(self._field("Library path", row["library_path"], muted_value=True))

        rule2 = QFrame()
        rule2.setObjectName("rule")
        col.addWidget(rule2)

        col.addWidget(DisplayLabel("In playlists", point_size=8, color=MUTED, letter_spacing=1))
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
        col.addWidget(DisplayLabel(label_text, point_size=8, color=MUTED, letter_spacing=1))
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
                f"border: 1px solid {ACCENT}; color: {ACCENT}; border-radius: 9px; padding: 3px 10px;"
            )
            row.addWidget(chip)
        row.addStretch(1)
        return row

    def _request_edit_tags(self):
        self.edit_tags_requested = True
        self.accept()


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
        cancel_btn = QPushButton("Close")
        cancel_btn.clicked.connect(self.reject)
        add_btn = QPushButton("Add selected")
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
        new_btn = QPushButton("+ Create")
        new_btn.clicked.connect(self._create_and_add)
        new_row.addWidget(self.new_name_edit, stretch=1)
        new_row.addWidget(new_btn)
        layout.addLayout(new_row)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        add_btn = QPushButton("Add")
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
