"""The Library page: Songs, Audiobooks (shelf and All), and the cleanup
sections (Duplicates, Unsorted, Health), switched from the sidebar."""

from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import tagging
from ..dialogs.duplicates import AutoResolveDialog, ResolveDuplicateDialog
from ..dialogs.export import CopyTracksDialog, ExportDialog
from ..dialogs.sources import AddFilesDialog, LibraryMigrationDialog
from ..dialogs.tracks import AddToPlaylistDialog, TrackInfoDialog
from ..library import db as libdb
from ..library.migrate import adopt_orphan_file
from ..library.playlists import write_m3u8
from ..library.series_artwork import (
    find_series_artwork,
)
from ..library_actions import (
    SelectionBar,
    add_menu_action,
    attach_context_menu,
    delete_audiobook_series,
    delete_library_tracks,
    remove_files,
)
from ..styles import ACCENT, GREEN, MUTED, RED, STAT_AMBER, STAT_PURPLE
from ..track_lists import (
    AUDIOBOOK_COLUMNS,
    COLUMN_SORT_FIELDS,
    SONG_COLUMNS,
    _format_total_playtime,
    _is_unsorted,
    make_track_list,
    populate_track_list,
    selected_library_paths,
    sort_tracks,
)
from ..widgets import (
    AudiobookShelfWidget,
    DisplayLabel,
    RoundedFrame,
    TrackColumnHeader,
)

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
    countsChanged = pyqtSignal(int, int, int, int)  # songs, audiobooks, duplicate pairs, unsorted — sidebar counts/badges
    headerChanged = pyqtSignal(str, str)  # page title, one-line summary — MainWindow's top bar shows these

    SECTIONS = ["songs", "audiobooks", "duplicates", "unsorted", "health"]  # sub_tabs index order

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
        # Search only re-filters the rows already loaded (_apply_filter) — it
        # never reloads the library or tells the rest of the window it changed.
        self._search_debounce.timeout.connect(self._apply_filter)
        self._music_tracks: list = []
        self._audiobook_tracks: list = []
        self._membership: dict = {}
        self._shown = {"songs": 0, "audiobooks": 0}

        self._counts = {"songs": 0, "audiobooks": 0, "duplicates": 0, "unsorted": 0,
                        "playtime": "", "series": 0}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        self.search_row = QWidget()
        top_row = QHBoxLayout(self.search_row)
        top_row.setContentsMargins(0, 0, 0, 0)
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search title, artist, album")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(lambda: self._search_debounce.start())
        top_row.addWidget(self.search_edit, stretch=1)
        layout.addWidget(self.search_row)

        # Library-wide actions live in a ⋯ menu that MainWindow puts in its
        # top bar (see library_menu) — including the library folder, which
        # used to take up a full-width bar of its own.
        self.library_menu = QMenu(self)
        add_sources_action = QAction("Add sources…", self)
        add_sources_action.triggered.connect(self._open_migration_dialog)
        add_files_action = QAction("Add files…", self)
        add_files_action.triggered.connect(self._open_add_files_dialog)
        export_action = QAction("Export…", self)
        export_action.triggered.connect(self._open_export_dialog)
        self.library_menu.addAction(add_sources_action)
        self.library_menu.addAction(add_files_action)
        self.library_menu.addAction(export_action)
        self.library_menu.addSeparator()
        self.location_action = QAction("", self)
        self.location_action.setEnabled(False)
        self.library_menu.addAction(self.location_action)
        change_location_action = QAction("Change library folder…", self)
        change_location_action.triggered.connect(self._change_library_location)
        self.library_menu.addAction(change_location_action)
        self._refresh_location_label()

        # Sections are switched from the sidebar, so the tab bar itself is
        # hidden — the QTabWidget is kept (rather than a QStackedWidget) only
        # because plenty of code already talks to it by index.
        self.sub_tabs = QTabWidget()
        self.sub_tabs.setObjectName("sectionStack")
        self.sub_tabs.setDocumentMode(True)
        self.sub_tabs.tabBar().hide()
        self.songs_list = make_track_list(SONG_COLUMNS)
        self.audiobooks_list = make_track_list(AUDIOBOOK_COLUMNS)
        self.songs_list.badgeClicked.connect(self._show_playlist_membership_menu)
        self.audiobooks_list.badgeClicked.connect(self._show_playlist_membership_menu)
        self.songs_list.infoClicked.connect(self._show_track_info_by_id)
        self.audiobooks_list.infoClicked.connect(self._show_track_info_by_id)
        self.songs_list.itemDoubleClicked.connect(self._play_from_songs)  # music only — never audiobooks_list
        self.songs_list.enable_file_drag(self._get_library_root)
        self.audiobooks_list.enable_file_drag(self._get_library_root)
        self.duplicates_list = QListWidget()
        self.unsorted_list = QListWidget()
        self.unsorted_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        sortable = set(COLUMN_SORT_FIELDS)
        self.songs_header = TrackColumnHeader(self.songs_list, sortable)
        self.songs_header.sortRequested.connect(self._on_column_sort)
        self.audiobooks_header = TrackColumnHeader(self.audiobooks_list, sortable)
        self.audiobooks_header.sortRequested.connect(self._on_column_sort)
        self._sync_sort_headers()

        self.sub_tabs.addTab(self._with_header(self.songs_header, self.songs_list), "Songs")
        self.sub_tabs.addTab(self._build_audiobooks_tab(), "Audiobooks")
        self.sub_tabs.addTab(self.duplicates_list, "Duplicates")
        self.sub_tabs.addTab(self.unsorted_list, "Unsorted")
        self.sub_tabs.addTab(self._build_health_tab(), "Health")
        layout.addWidget(self.sub_tabs, stretch=1)

        # One bar for Songs and Audiobooks-All — whichever list is showing.
        self.copy_actions = SelectionBar([self.songs_list, self.audiobooks_list], get_list=self._active_track_list)
        self.add_to_playlist_btn = self.copy_actions.add_button("Add to playlist…", self._add_selected_to_playlist)
        self.copy_actions.add_button("Edit tags", self._edit_tags_selected_track)
        self.copy_actions.add_button("Copy…", self._copy_selected_tracks)
        self.copy_actions.add_button("Delete…", self._remove_selected_tracks, danger=True)
        layout.addWidget(self.copy_actions)
        attach_context_menu(self.songs_list, self._build_track_menu)
        attach_context_menu(self.audiobooks_list, self._build_track_menu)

        self.dupe_actions = QWidget()
        dupe_row = QHBoxLayout(self.dupe_actions)
        dupe_row.setContentsMargins(0, 0, 0, 0)
        dupe_row.addStretch(1)
        auto_resolve_btn = QPushButton("Auto-resolve…")
        auto_resolve_btn.setToolTip("Keep the larger copy of every pair with matching title/artist")
        auto_resolve_btn.clicked.connect(self._auto_resolve_dupes)
        dupe_row.addWidget(auto_resolve_btn)
        resolve_btn = QPushButton("Resolve…")
        resolve_btn.clicked.connect(self._resolve_selected_dupe)
        dupe_row.addWidget(resolve_btn)
        dismiss_btn = QPushButton("Dismiss selected")
        dismiss_btn.clicked.connect(self._dismiss_selected_dupe)
        dupe_row.addWidget(dismiss_btn)
        layout.addWidget(self.dupe_actions)

        self.unsorted_actions = SelectionBar(
            [self.unsorted_list], hint="Select a file and fix its tags to file it into your library"
        )
        self.unsorted_actions.add_button("Fix tags", self._fix_selected_unsorted_tags)
        self.unsorted_actions.add_button("Delete…", self._remove_selected_unsorted, danger=True)
        layout.addWidget(self.unsorted_actions)

        def unsorted_menu(menu, item):
            if len(self.unsorted_list.selectedItems()) == 1:
                add_menu_action(menu, "Fix tags", self._fix_selected_unsorted_tags)
                menu.addSeparator()
            add_menu_action(menu, "Delete…", self._remove_selected_unsorted, danger=True)
        attach_context_menu(self.unsorted_list, unsorted_menu)

        self.sub_tabs.currentChanged.connect(self._update_sub_tab_actions)
        self._update_sub_tab_actions(self.sub_tabs.currentIndex())

        self.empty_hint = QLabel("")
        self.empty_hint.setObjectName("muted")
        self.empty_hint.setWordWrap(True)
        layout.addWidget(self.empty_hint)

        self.refresh()

    @staticmethod
    def _with_header(header: QWidget, body: QWidget) -> QWidget:
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        col.addWidget(header)
        col.addWidget(body, stretch=1)
        return page

    def show_section(self, kind: str):
        """Switch to one of SECTIONS — driven by the sidebar."""
        if kind in self.SECTIONS:
            self.sub_tabs.setCurrentIndex(self.SECTIONS.index(kind))

    def current_section(self) -> str:
        return self.SECTIONS[self.sub_tabs.currentIndex()]

    def _section_header(self) -> tuple[str, str]:
        c = self._counts
        kind = self.current_section()
        searching = bool(self.search_edit.text().strip())
        if kind == "songs" and searching:
            summary = f"{self._shown['songs']:,} of {c['songs']:,} songs match"
        elif kind == "songs":
            summary = f"{c['songs']:,} songs" + (f" · {c['playtime']}" if c["playtime"] else "")
        elif kind == "audiobooks" and searching:
            summary = f"{self._shown['audiobooks']:,} of {c['audiobooks']:,} files match"
        elif kind == "audiobooks":
            summary = f"{c['audiobooks']:,} files"
            if c["series"]:
                summary += f" · {c['series']} series"
        elif kind == "duplicates":
            summary = (f"{c['duplicates']:,} pairs that look like the same track"
                       if c["duplicates"] else "Nothing to review")
        elif kind == "unsorted":
            summary = (f"{c['unsorted']:,} files with missing or unusable tags"
                       if c["unsorted"] else "Everything's filed")
        else:
            summary = "Missing tags, art, and files that have drifted out of sync"
        return self.sub_tabs.tabText(self.sub_tabs.currentIndex()), summary

    def _emit_header(self):
        self.headerChanged.emit(*self._section_header())

    def _on_column_sort(self, key: str):
        field = COLUMN_SORT_FIELDS.get(key, "Title")
        if field == self._sort_field:
            self._sort_ascending = not self._sort_ascending
        else:
            self._sort_field = field
            # Newest first / longest first / most playlists first is the
            # useful starting direction for those; A→Z for text columns.
            self._sort_ascending = field not in ("Date Added", "Time", "In")
        self._sync_sort_headers()
        self._apply_filter()

    def _sync_sort_headers(self):
        key = next((k for k, f in COLUMN_SORT_FIELDS.items() if f == self._sort_field), "title")
        self.songs_header.set_sort(key, self._sort_ascending)
        self.audiobooks_header.set_sort(key, self._sort_ascending)

    def _make_stat_card(self, label_text: str, value_label: QLabel, value_color: str) -> RoundedFrame:
        frame = RoundedFrame(radius=12)
        inner = QVBoxLayout(frame)
        inner.setContentsMargins(14, 12, 14, 12)
        inner.setSpacing(8)
        # Label stays a uniform muted pixel caption — the color goes on the
        # number itself instead, one distinct accent per card, so they're
        # tellable apart at a glance without turning the labels into noise.
        inner.addWidget(DisplayLabel(label_text, point_size=12, color=MUTED, letter_spacing=2))
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
        view_label = QLabel("View")
        view_label.setObjectName("muted")
        toggle_row.addWidget(view_label)
        self.audiobooks_view_toggle = QComboBox()
        self.audiobooks_view_toggle.addItems(["Series", "All"])
        self.audiobooks_view_toggle.currentTextChanged.connect(self._on_audiobooks_view_changed)
        toggle_row.addWidget(self.audiobooks_view_toggle)
        toggle_row.addStretch(1)
        layout.addLayout(toggle_row)

        self.audiobooks_stack = QStackedWidget()
        self.audiobooks_shelf = AudiobookShelfWidget()
        self.audiobooks_shelf.seriesActivated.connect(self.audiobookSeriesActivated.emit)
        self.audiobooks_shelf.playRequested.connect(self.playRequested.emit)
        self.audiobooks_shelf.infoRequested.connect(self._show_track_info_by_id)
        self.audiobooks_shelf.seriesMenuRequested.connect(self._show_series_tile_menu)
        self.audiobooks_shelf.bookMenuRequested.connect(self._show_book_tile_menu)
        self.audiobooks_stack.addWidget(self.audiobooks_shelf)
        self.audiobooks_stack.addWidget(self._with_header(self.audiobooks_header, self.audiobooks_list))
        layout.addWidget(self.audiobooks_stack, stretch=1)

        return tab

    def _on_audiobooks_view_changed(self, text: str):
        self.audiobooks_stack.setCurrentIndex(1 if text == "All" else 0)
        self._update_sub_tab_actions(self.sub_tabs.currentIndex())

    def _update_sub_tab_actions(self, index: int):
        audiobooks_all = index == 1 and self.audiobooks_view_toggle.currentText() == "All"
        self.search_row.setVisible(index in (0, 1))
        self.copy_actions.setVisible(index == 0 or audiobooks_all)
        self.add_to_playlist_btn.setVisible(index == 0)  # SONGS only — playlists are a music-only concept
        self.dupe_actions.setVisible(index == 2)  # DUPLICATES
        self.unsorted_actions.setVisible(index == 3)  # UNSORTED
        if index == HEALTH_TAB_INDEX:
            self._refresh_health()
        self._emit_header()

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
        self.location_action.setText(f"Library folder: {root}" if root else "Library folder: not chosen yet")

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
            self.empty_hint.setText("No library yet. Open the ⋯ menu and choose Add sources to get started.")
            self.countsChanged.emit(0, 0, 0, 0)
            self._emit_header()
            self.libraryChanged.emit()
            return

        conn = libdb.open_db(library_root)
        membership = libdb.playlist_membership_counts(conn)

        music_tracks = [t for t in libdb.list_tracks(conn, "music") if not _is_unsorted(t["library_path"])]
        unsorted_tracks = [t for t in libdb.list_tracks(conn, "music") if _is_unsorted(t["library_path"])]
        audiobook_tracks = libdb.list_tracks(conn, "audiobook")
        dupes = libdb.list_unresolved_fuzzy_dupes(conn)

        self._music_tracks, self._audiobook_tracks, self._membership = music_tracks, audiobook_tracks, membership
        self._apply_filter(emit_header=False)
        self._populate_unsorted(unsorted_tracks)
        self._populate_dupes(conn, dupes)
        self._refresh_audiobook_shelf(conn, library_root)

        self.empty_hint.setText(
            "" if (music_tracks or audiobook_tracks) else "No library yet. Open the ⋯ menu and choose Add sources to get started."
        )
        self._counts.update(
            songs=len(music_tracks), audiobooks=len(audiobook_tracks), duplicates=self.duplicates_list.count(),
            unsorted=len(unsorted_tracks),
            playtime=_format_total_playtime(libdb.music_total_duration_seconds(conn)),
            series=len({(t["album"] or "").strip() for t in audiobook_tracks if (t["album"] or "").strip()}),
        )
        conn.close()
        self.countsChanged.emit(self._counts["songs"], self._counts["audiobooks"],
                                self._counts["duplicates"], self._counts["unsorted"])
        self._emit_header()
        self.libraryChanged.emit()

    def _apply_filter(self, emit_header: bool = True):
        """Re-filters and re-sorts the Songs / Audiobooks tables from the
        rows the last refresh() loaded. This is all a search keystroke or a
        column-header click does — no database reads, no shelf/cover work,
        and no libraryChanged (which would rebuild the sidebar's playlists)."""
        query = self.search_edit.text().strip().lower()
        music = sort_tracks(self._filter(self._music_tracks, query), self._membership,
                            self._sort_field, self._sort_ascending)
        books = sort_tracks(self._filter(self._audiobook_tracks, query), self._membership,
                            self._sort_field, self._sort_ascending)
        populate_track_list(self.songs_list, music, self._membership)
        populate_track_list(self.audiobooks_list, books, self._membership)
        self._shown = {"songs": len(music), "audiobooks": len(books)}
        if emit_header:
            self._emit_header()

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
        self._counts.update(songs=0, audiobooks=0, duplicates=0, unsorted=0, playtime="", series=0)
        self._music_tracks, self._audiobook_tracks, self._membership = [], [], {}
        self._shown = {"songs": 0, "audiobooks": 0}

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

    def _active_track_list(self):
        return self.songs_list if self.sub_tabs.currentIndex() == 0 else self.audiobooks_list

    def _build_track_menu(self, menu: QMenu, item: QListWidgetItem):
        """Right-click menu for a Songs / Audiobooks-All row."""
        is_songs = self.sub_tabs.currentIndex() == 0
        single = len(self._active_track_list().selectedItems()) == 1
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        if is_songs and single:
            add_menu_action(menu, "Play", lambda: self._play_from_songs(item))
        if is_songs:
            add_menu_action(menu, "Add to playlist…", self._add_selected_to_playlist)
        if single:
            add_menu_action(menu, "Edit tags", self._edit_tags_selected_track)
            add_menu_action(menu, "Track info", lambda: self._show_track_info_by_id(data["id"]))
        add_menu_action(menu, "Copy…", self._copy_selected_tracks)
        menu.addSeparator()
        add_menu_action(menu, "Delete…", self._remove_selected_tracks, danger=True)

    def _show_series_tile_menu(self, series_name: str, pos):
        menu = QMenu(self)
        add_menu_action(menu, "Open", lambda: self.audiobookSeriesActivated.emit(series_name))
        menu.addSeparator()
        add_menu_action(menu, "Delete series…", lambda: self._delete_series(series_name), danger=True)
        menu.exec(pos)

    def _delete_series(self, series_name: str):
        library_root = self._get_library_root()
        if library_root and delete_audiobook_series(self, library_root, series_name) > 0:
            self.refresh()

    def _show_book_tile_menu(self, entry: dict, pos):
        """⋯ / right-click on a standalone (no-series) audiobook tile."""
        library_root = self._get_library_root()
        if not library_root:
            return
        rel_path = str(Path(entry["path"]).relative_to(library_root))
        menu = QMenu(self)
        add_menu_action(menu, "Play", lambda: self.playRequested.emit(
            [{k: v for k, v in entry.items() if k != "art"}], 0))
        add_menu_action(menu, "Track info", lambda: self._show_track_info_by_id(entry["id"]))
        add_menu_action(menu, "Edit tags", lambda: self.fixTagsRequested.emit(entry["path"], entry["id"]))
        add_menu_action(menu, "Copy…", lambda: CopyTracksDialog(self, library_root, [rel_path]).exec())
        menu.addSeparator()

        def delete():
            if delete_library_tracks(self, library_root, [(entry["id"], rel_path)], noun="audiobook") > 0:
                self._standalone_audiobook_art_cache.pop(entry["id"], None)
                self.refresh()
        add_menu_action(menu, "Delete…", delete, danger=True)
        menu.exec(pos)

    def _remove_selected_tracks(self):
        items = self._active_track_list().selectedItems()
        if not items:
            QMessageBox.information(self, "Nothing selected", "Select one or more tracks first.")
            return
        library_root = self._get_library_root()
        if not library_root:
            return
        is_songs = self.sub_tabs.currentIndex() == 0
        tracks = [(i.data(Qt.ItemDataRole.UserRole)["id"], i.data(Qt.ItemDataRole.UserRole)["library_path"])
                  for i in items]
        if delete_library_tracks(self, library_root, tracks, noun="song" if is_songs else "audiobook") > 0:
            self.refresh()

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
        """Steps through the duplicates list one pair after another, starting
        at the selected pair (or the top if nothing's selected) — each choice
        opens the next pair's dialog straight away, until the list runs out
        or the user hits STOP. Pairs are re-read from the DB as we go, since
        deleting a track also drops any later pair that involved it."""
        count = self.duplicates_list.count()
        if count == 0:
            QMessageBox.information(self, "No duplicates", "There are no duplicate pairs to resolve.")
            return
        library_root = self._get_library_root()
        if not library_root:
            return
        start = max(self.duplicates_list.currentRow(), 0)
        dupe_ids = [self.duplicates_list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(start, count)]

        changed = False
        decision: dict = {}  # "delete permanently?" is asked at most once for the whole pass
        for index, dupe_id in enumerate(dupe_ids, start=1):
            conn = libdb.open_db(library_root)
            dupe_row = conn.execute(
                "SELECT * FROM fuzzy_dupes WHERE id = ? AND resolved = 0", (dupe_id,)
            ).fetchone()
            track_a = track_b = None
            if dupe_row:
                track_a = conn.execute("SELECT * FROM tracks WHERE id = ?", (dupe_row["track_id_a"],)).fetchone()
                track_b = conn.execute("SELECT * FROM tracks WHERE id = ?", (dupe_row["track_id_b"],)).fetchone()
            conn.close()
            if not track_a or not track_b:
                continue  # already handled by an earlier choice in this pass

            dialog = ResolveDuplicateDialog(
                self, library_root, track_a, track_b, position=f"({index} of {len(dupe_ids)})"
            )
            if not dialog.exec():
                break  # STOP / window closed
            if dialog.action == "keep":
                if not self._resolve_dupe(dialog.keep_id, dialog.remove_id, refresh=False, decision=decision):
                    break  # file in use — warning already shown, don't keep going
                changed = True
            elif dialog.action == "dismiss":
                conn = libdb.open_db(library_root)
                libdb.resolve_fuzzy_dupe(conn, dupe_id)
                conn.close()
                changed = True
            # "skip": just move on to the next pair

        if changed:
            self.refresh()

    def _auto_resolve_dupes(self):
        library_root = self._get_library_root()
        if not library_root:
            return
        conn = libdb.open_db(library_root)
        pairs = []
        for d in libdb.list_unresolved_fuzzy_dupes(conn):
            a = conn.execute("SELECT * FROM tracks WHERE id = ?", (d["track_id_a"],)).fetchone()
            b = conn.execute("SELECT * FROM tracks WHERE id = ?", (d["track_id_b"],)).fetchone()
            if a and b:
                pairs.append((d["id"], a, b))
        conn.close()
        if not pairs:
            QMessageBox.information(self, "No duplicates", "There are no duplicate pairs to resolve.")
            return

        dialog = AutoResolveDialog(self, library_root, pairs)
        if not dialog.exec() or not dialog.plan:
            return
        resolved, in_use = 0, []
        decision: dict = {}  # "delete permanently?" is asked at most once for the whole pass
        for entry in dialog.plan:
            if self._resolve_dupe(entry["keep"]["id"], entry["remove"]["id"], refresh=False, warn=False,
                                  decision=decision):
                resolved += 1
            else:
                in_use.append(entry["remove"]["library_path"])
        self.refresh()
        message = f"Resolved {resolved} duplicate pair(s)."
        remaining = self.duplicates_list.count()
        if remaining:
            message += f"\n\n{remaining} pair(s) still need a look. Use Resolve… for those."
        if in_use:
            message += ("\n\nThese files couldn't be removed and were left alone (nothing "
                        "changed for them):\n\n" + "\n".join(in_use))
        QMessageBox.information(self, "Auto-resolve finished", message)

    def _resolve_dupe(self, keep_id: int, remove_id: int, refresh: bool = True, warn: bool = True,
                      decision: dict | None = None) -> bool:
        """Moves the removed copy to the Trash and points its playlist
        entries at the kept one. `decision` is shared across a whole
        resolve/auto-resolve pass (see remove_files)."""
        library_root = self._get_library_root()
        conn = libdb.open_db(library_root)
        row = conn.execute("SELECT library_path FROM tracks WHERE id = ?", (remove_id,)).fetchone()
        if row:
            path = Path(library_root) / row["library_path"]
            error = remove_files(self, [path], decision)[path]
            if error:
                conn.close()
                if not warn:
                    return False
                QMessageBox.warning(
                    self, "Couldn't resolve",
                    "That file couldn't be removed (it may be open in another program, or you chose "
                    "not to delete it permanently). Nothing was changed.",
                )
                return False
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
        if refresh:
            self.refresh()
        return True

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
        tracks = [(i.data(Qt.ItemDataRole.UserRole)["track_id"], i.data(Qt.ItemDataRole.UserRole)["path"])
                  for i in items]
        if delete_library_tracks(self, library_root, tracks) > 0:
            self.refresh()

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
        rescan_btn = QPushButton("Rescan")
        rescan_btn.clicked.connect(self._health_rescan)
        header_row.addWidget(rescan_btn)
        layout.addLayout(header_row)

        stats_row = QHBoxLayout()
        stats_row.setSpacing(10)
        self.health_stat_values = {}
        for label, color in [
            ("Duplicates", GREEN),
            ("Missing tags", STAT_AMBER),
            ("Missing art", STAT_PURPLE),
            ("Missing files", RED),
            ("Orphaned files", ACCENT),
        ]:
            value_label = QLabel("0")
            self.health_stat_values[label] = value_label
            stats_row.addWidget(self._make_stat_card(label, value_label, color))
        layout.addLayout(stats_row)

        category_row = QHBoxLayout()
        category_label = QLabel("Show")
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

        self.health_fix_tags_btn = QPushButton("Fix tags")
        self.health_fix_tags_btn.clicked.connect(self._health_fix_tags)
        self.health_remove_btn = QPushButton("Remove from index")
        self.health_remove_btn.clicked.connect(self._health_remove_missing)
        self.health_adopt_btn = QPushButton("File into library")
        self.health_adopt_btn.clicked.connect(self._health_file_orphans)
        self.health_goto_dupes_btn = QPushButton("Go to duplicates")
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
        self.health_stat_values["Duplicates"].setText(str(len(dupe_entries)))
        self.health_stat_values["Missing tags"].setText(str(len(tag_entries)))
        self.health_stat_values["Missing art"].setText(str(len(art_entries)))
        self.health_stat_values["Missing files"].setText(str(len(missing_entries)))
        self.health_stat_values["Orphaned files"].setText(str(len(orphan_entries)))
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
