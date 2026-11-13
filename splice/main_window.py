"""Main application window: tabbed Merge / Split / YouTube / Tags modules plus a shared console."""

import json
import math
import shutil
import sys
from pathlib import Path

from PyQt6.QtCore import QEvent, QPoint, QSettings, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QDesktopServices, QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import tagging
from .library import db as libdb
from .library.health import HealthScanWorker
from .library.loudness import LoudnessAnalysisWorker
from .library_view import AudiobookSeriesView, LibraryView, PlaylistView
from .player import TrackPlayer
from .sidebar import Sidebar
from .styles import STYLESHEET, build_palette, load_bundled_fonts
from .widgets import (
    ConsoleStrip,
    CoverArtThumbnail,
    DropListWidget,
    FilePickerList,
    LevelGauge,
    LogButton,
    NowPlayingBar,
    SplitDropZone,
)
from .workers import (
    FFMPEG,
    FFMPEG_INSTALL_HINT,
    MergeWorker,
    PlaylistExpandWorker,
    SplitWorker,
    TitleLookupWorker,
    YouTubeWorker,
    looks_like_playlist_url,
    probe_duration_seconds,
    ts,
)


class ConsoleOverlay(QFrame):
    """Floating log panel — the console's expanded state. Deliberately not
    added to root's QVBoxLayout (that's what lets it float over the
    Rail+Deck+content column instead of being pushed around by it) —
    MainWindow positions and raises it manually
    (see _reposition_console_overlay/_toggle_console). Reuses the "panel"
    QSS objectName for its bordered-surface look but builds its own layout
    rather than the old panel() helper, which always added a plain "Console"
    title label — this has its own ConsoleStrip header instead, so clicking
    anywhere in it collapses it back down."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("panel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(8)

        self.header = ConsoleStrip()
        self.header.set_expanded(True)
        layout.addWidget(self.header)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        font = QFont("Menlo")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.log_box.setFont(font)
        layout.addWidget(self.log_box, stretch=1)

        self.hide()

    def set_status(self, state: str, text: str):
        self.header.set_status(state, text)

    def set_task_text(self, text: str):
        self.header.set_task_text(text)

    def append_log(self, text: str):
        self.log_box.appendPlainText(text)


class ToolDrawer(QFrame):
    """Merge / Split / YouTube / Tags slide in here, over the right side of
    the content area, instead of replacing the page you were on — so the
    library stays visible (and you can drag songs from it into Merge or
    Tags). Not in any layout: MainWindow positions it (reposition()) and
    shows/hides it. Background jobs keep running while it's closed."""

    closeRequested = pyqtSignal()

    MIN_WIDTH = 480
    MAX_WIDTH = 620

    def __init__(self, parent):
        super().__init__(parent)
        self.setObjectName("toolDrawer")
        self.current_kind = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 18, 16)
        layout.setSpacing(12)

        header = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        self.title_label = QLabel("")
        self.title_label.setObjectName("drawerTitle")
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("pageSubtitle")
        title_col.addWidget(self.title_label)
        title_col.addWidget(self.summary_label)
        header.addLayout(title_col, stretch=1)
        close_btn = QPushButton("✕")
        close_btn.setObjectName("drawerClose")
        close_btn.setToolTip("Close (Esc)")
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.closeRequested.emit)
        header.addWidget(close_btn, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)

        self.stack = QStackedWidget()
        layout.addWidget(self.stack, stretch=1)
        self._pages: dict[str, QWidget] = {}

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(36)
        shadow.setOffset(-6, 0)
        shadow.setColor(QColor(0, 0, 0, 150))
        self.setGraphicsEffect(shadow)
        self.hide()

    def add_page(self, kind: str, page: QWidget):
        self._pages[kind] = page
        self.stack.addWidget(page)

    def show_tool(self, kind: str):
        self.current_kind = kind
        title, summary = TOOL_PAGES[kind]
        self.title_label.setText(title)
        self.summary_label.setText(summary)
        self.stack.setCurrentWidget(self._pages[kind])
        self.reposition()
        self.show()
        self.raise_()

    def close_drawer(self):
        self.current_kind = None
        self.hide()

    def reposition(self):
        host = self.parentWidget()
        width = max(self.MIN_WIDTH, min(self.MAX_WIDTH, int(host.width() * 0.46)))
        width = min(width, host.width())
        self.setGeometry(host.width() - width, 0, width, host.height())


# Tool pages: kind → (top bar title, top bar summary). The page itself no
# longer carries a title — the top bar shows it, same as every other page.
TOOL_PAGES = {
    "merge": ("Merge", "Combine multi-part audiobooks into one file"),
    "split": ("Split", "Chop files into fixed-length segments"),
    "youtube": ("YouTube", "Download videos and playlists as mp3"),
    "tags": ("Tags", "Edit title, artist, album, year, and artwork"),
}
LIBRARY_KINDS = ("songs", "audiobooks", "duplicates", "unsorted", "health")


def tab_content(title: str) -> tuple[QWidget, QVBoxLayout]:
    """Borderless content container for a tool page. `title` is kept for
    readability at the call sites; the visible title comes from the top bar
    (TOOL_PAGES)."""
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    return widget, layout


def progress_row(cancel_slot) -> tuple[QHBoxLayout, LevelGauge, QPushButton]:
    row = QHBoxLayout()
    gauge = LevelGauge()
    cancel_btn = QPushButton("Cancel")
    cancel_btn.setEnabled(False)
    cancel_btn.clicked.connect(cancel_slot)
    row.addWidget(gauge, stretch=1)
    row.addWidget(cancel_btn)
    return row, gauge, cancel_btn


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("Thomas", "Splice")
        # Remembered across launches; only asked for again if it's missing
        # or the folder has since been deleted/unmounted (_ensure_output_dir).
        saved_output = str(self.settings.value("output_dir", "") or "")
        self.output_dir = saved_output if saved_output and Path(saved_output).is_dir() else None
        self.library_root = str(self.settings.value("library_root", "") or "") or None
        self._pending_library_refile = None
        self.active_worker = None
        self.active_kind = None
        self._batch_tag_cancel_requested = False
        self._last_split_segments = []
        self._split_estimates = []
        self.youtube_active_worker = None
        self._youtube_lookup_workers = []
        self._current_download_title = None
        self._current_download_playlist_id = None
        self._current_download_kind = None
        self.player = TrackPlayer(self)
        self._loudness_worker = None
        self._health_worker = None
        self._playlist_mosaic_cache = {}  # playlist_id -> (first-4-track-paths signature, mosaic bytes|None)

        self.setWindowTitle("Splice")
        self.resize(900, 840)
        self._build_ui()
        self._load_persisted_batch_tag_defaults()
        self._load_persisted_youtube_quality()
        QTimer.singleShot(200, self._startup)
        QTimer.singleShot(350, self._load_persisted_youtube_queue)

    # ---- UI construction ----

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QHBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.sidebar = Sidebar()
        self.sidebar.navigate.connect(self._on_navigate)
        self.sidebar.createRequested.connect(self._create_playlist)
        self.sidebar.renameRequested.connect(self._rename_playlist)
        self.sidebar.tracksDropped.connect(self._add_dropped_tracks_to_playlist)
        self.now_playing_bar = NowPlayingBar()
        self.now_playing_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.sidebar.set_player(self.now_playing_bar)
        outer.addWidget(self.sidebar)

        right = QWidget()
        layout = QVBoxLayout(right)
        layout.setContentsMargins(22, 16, 22, 14)
        layout.setSpacing(12)
        layout.addWidget(self._build_top_bar())
        layout.addWidget(self._build_body(), stretch=1)
        outer.addWidget(right, stretch=1)

        # Not added to root's layout — that's what lets it float over the
        # Rail+Deck+content column instead of being pushed around by it.
        # MainWindow positions/raises it manually (_reposition_console_overlay).
        self.console_overlay = ConsoleOverlay(root)
        self.console_overlay.header.clicked.connect(self._toggle_console)

        self._wire_player()

    def _build_body(self) -> QWidget:
        self.body_widget = QWidget()
        content_layout = QVBoxLayout(self.body_widget)
        content_layout.setContentsMargins(0, 0, 0, 0)

        self.content_stack = QStackedWidget()
        self.page_library = LibraryView(self._get_library_root, self._set_library_root)
        self.page_library.fixTagsRequested.connect(self._fix_tags_for_path)
        self.page_library.libraryChanged.connect(self._refresh_sidebar_playlists)
        self.page_library.libraryChanged.connect(self._kick_off_loudness_analysis)
        self.page_library.healthRescanRequested.connect(self._kick_off_health_analysis)
        self.page_library.playlistActivated.connect(lambda pid, name: self._on_navigate("playlist", (pid, name)))
        self.page_library.audiobookSeriesActivated.connect(
            lambda name: self._on_navigate("audiobook_series", name)
        )
        self.page_library.libraryLocationChanged.connect(
            lambda path: self._write_log(f"[{ts()}] Library location set to: {path}")
        )
        self.page_playlist = PlaylistView(self._get_library_root)
        self.page_playlist.playlistsChanged.connect(self._refresh_sidebar_playlists)
        self.page_playlist.fixTagsRequested.connect(self._fix_tags_for_path)
        self.page_audiobook_series = AudiobookSeriesView(self._get_library_root)
        self.page_audiobook_series.fixTagsRequested.connect(self._fix_tags_for_path)
        self.page_audiobook_series.backRequested.connect(self._return_to_audiobooks)
        self.page_audiobook_series.libraryChanged.connect(lambda: self.page_library.refresh())
        self.page_merge = self._build_merge_panel()
        self.page_split = self._build_split_panel()
        self.page_youtube = self._build_youtube_panel()
        self.page_tags = self._build_metadata_panel()
        for page in (self.page_library, self.page_playlist, self.page_audiobook_series):
            self.content_stack.addWidget(page)
        content_layout.addWidget(self.content_stack)

        self.tool_drawer = ToolDrawer(self.body_widget)
        for kind, page in (("merge", self.page_merge), ("split", self.page_split),
                           ("youtube", self.page_youtube), ("tags", self.page_tags)):
            self.tool_drawer.add_page(kind, page)
        self.tool_drawer.closeRequested.connect(self._close_tool_drawer)
        self.body_widget.installEventFilter(self)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self._close_tool_drawer)

        self.page_library.countsChanged.connect(self.sidebar.set_counts)
        self.page_library.headerChanged.connect(self._on_library_header_changed)
        self.page_library.sub_tabs.currentChanged.connect(self._on_library_section_changed)
        self.library_more_btn.setMenu(self.page_library.library_menu)
        self.page_library.refresh()  # re-emit counts/header now that they're connected

        self._on_navigate("songs", None)
        self._refresh_sidebar_playlists()
        return self.body_widget

    def _get_library_root(self) -> str | None:
        return self.library_root

    def _set_library_root(self, path: str):
        self.library_root = path
        self.settings.setValue("library_root", path)

    def _refresh_sidebar_playlists(self):
        if not self.library_root or not Path(self.library_root).exists():
            self.sidebar.set_playlists([])
        else:
            from .library.playlists import find_playlist_artwork
            from .library_view import playlist_mosaic_art
            conn = libdb.open_db(self.library_root)
            rows = libdb.list_playlists(conn)
            playlists = []
            for p in rows:
                art_path = find_playlist_artwork(self.library_root, p["id"])
                if art_path:
                    art_bytes = art_path.read_bytes()
                else:
                    # No explicit artwork — fall back to an auto-mosaic of
                    # the playlist's own first few tracks' covers, cached
                    # per playlist so this doesn't re-read cover files on
                    # every refresh (this fires on every library search
                    # keystroke via libraryChanged, not just playlist edits).
                    ordered_paths = libdb.playlist_track_paths_in_order(conn, p["id"])
                    signature = tuple(ordered_paths[:4])
                    cached = self._playlist_mosaic_cache.get(p["id"])
                    if cached is not None and cached[0] == signature:
                        art_bytes = cached[1]
                    else:
                        art_bytes = playlist_mosaic_art(self.library_root, ordered_paths)
                        self._playlist_mosaic_cache[p["id"]] = (signature, art_bytes)
                playlists.append((p["id"], p["name"], art_bytes))
            conn.close()
            self.sidebar.set_playlists(playlists)
        self._refresh_playlist_combo(self.youtube_playlist_combo)
        self._refresh_playlist_combo(self.merge_playlist_combo)

    def _kick_off_loudness_analysis(self):
        """Analyzes whatever tracks haven't been measured yet — the same
        sweep naturally covers both "a build/download just added a few
        tracks" and "catch up the whole existing library", since it's just
        "measure whatever's missing" either way. Deliberately not wired to
        _set_status: that's a single shared indicator Merge/Split/Tags
        already treat as mutually exclusive, and YouTube downloads have
        their own separate progress widget specifically to avoid fighting
        over it — loudness analysis is a third independent background lane
        that can run for a long time, so it gets its own small readout on
        the console strip (task_label) instead of stomping on whatever
        else's status is showing. That readout is the only way to tell it's
        running/how far along — worth knowing it's there rather than
        wondering why nothing looks different while it works."""
        if not self.library_root or not Path(self.library_root).exists():
            return
        if self._health_worker is not None and self._health_worker.isRunning():
            return  # defer — health's finished_ok will re-check loudness when it's done
        if self._loudness_worker is not None and self._loudness_worker.isRunning():
            return
        conn = libdb.open_db(self.library_root)
        pending = conn.execute(
            "SELECT 1 FROM tracks WHERE kind = 'music' AND loudness_lufs IS NULL LIMIT 1"
        ).fetchone()
        conn.close()
        if not pending:
            self._kick_off_health_analysis()
            return
        self._loudness_worker = LoudnessAnalysisWorker(self.library_root)
        self._loudness_worker.log.connect(self._write_log)
        self._loudness_worker.progress.connect(self._on_loudness_progress)
        self._loudness_worker.finished_ok.connect(self._on_loudness_analysis_done)
        self._loudness_worker.start()

    def _on_loudness_progress(self, current: int, total: int):
        text = f"Analyzing loudness ({current:,}/{total:,})"
        self.console_strip.set_task_text(text)
        self.console_overlay.set_task_text(text)

    def _on_loudness_analysis_done(self, count: int):
        self.console_strip.set_task_text("")
        self.console_overlay.set_task_text("")
        if count:
            self._write_log(f"[{ts()}] Loudness analysis: measured {count} track(s)")
        self._kick_off_health_analysis()

    def _kick_off_health_analysis(self):
        """Checks embedded-art presence on whatever music tracks haven't been
        checked yet — same "measure whatever's missing" shape as loudness
        analysis, and deliberately chained to run only after loudness
        finishes (both directions: see the isRunning() guards below) rather
        than as a second concurrent lane, since both are "sweep the whole
        library once" background maintenance rather than independent
        concerns the way YouTube downloads vs. Merge/Split/Tags are — running
        them concurrently would mean two QThreads racing to write the one
        shared task_label."""
        if not self.library_root or not Path(self.library_root).exists():
            return
        if self._loudness_worker is not None and self._loudness_worker.isRunning():
            return  # defer — loudness's finished_ok will re-check health when it's done
        if self._health_worker is not None and self._health_worker.isRunning():
            return
        conn = libdb.open_db(self.library_root)
        pending = conn.execute("SELECT 1 FROM tracks WHERE kind = 'music' AND has_art IS NULL LIMIT 1").fetchone()
        conn.close()
        if not pending:
            return
        self._health_worker = HealthScanWorker(self.library_root)
        self._health_worker.log.connect(self._write_log)
        self._health_worker.progress.connect(self._on_health_progress)
        self._health_worker.finished_ok.connect(self._on_health_analysis_done)
        self._health_worker.start()

    def _on_health_progress(self, current: int, total: int):
        text = f"Checking art ({current:,}/{total:,})"
        self.console_strip.set_task_text(text)
        self.console_overlay.set_task_text(text)

    def _on_health_analysis_done(self, count: int):
        self.console_strip.set_task_text("")
        self.console_overlay.set_task_text("")
        if count:
            self._write_log(f"[{ts()}] Health scan: checked art on {count} track(s)")
        self.page_library.refresh_health_if_active()
        self._kick_off_loudness_analysis()

    def _refresh_playlist_combo(self, combo: QComboBox):
        """Repopulates a "which playlist to add this to" combo (options:
        no playlist / each existing playlist / create new) — shared by the
        YouTube tab's and Merge tab's own filing-destination pickers."""
        previous = combo.currentData() if combo.count() else None
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("No playlist (library only)", None)
        if self.library_root and Path(self.library_root).exists():
            conn = libdb.open_db(self.library_root)
            for p in libdb.list_playlists(conn):
                combo.addItem(p["name"], p["id"])
            conn.close()
        combo.addItem("+ New Playlist...", "__new__")
        idx = combo.findData(previous)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.blockSignals(False)

    def _on_youtube_destination_changed(self, index: int):
        is_music = self.youtube_destination_combo.itemData(index) == "music"
        self.youtube_playlist_combo.setEnabled(is_music)
        if not is_music:
            self.youtube_playlist_combo.setCurrentIndex(0)  # "No playlist (library only)" — playlists are music-only

    def _on_merge_destination_changed(self, index: int):
        is_music = self.merge_destination_combo.itemData(index) == "music"
        self.merge_playlist_combo.setEnabled(is_music)
        if not is_music:
            self.merge_playlist_combo.setCurrentIndex(0)  # "No playlist (library only)" — playlists are music-only

    def _on_playlist_combo_changed(self, combo: QComboBox, index: int):
        """Handles "+ New Playlist..." being picked in either the YouTube
        tab's or Merge tab's playlist combo — creates it and selects it."""
        if combo.itemData(index) != "__new__":
            return
        name, ok = QInputDialog.getText(self, "New Playlist", "Playlist name:")
        if ok and name.strip() and self.library_root:
            conn = libdb.open_db(self.library_root)
            playlist_id = libdb.get_or_create_playlist(conn, name.strip())
            conn.close()
            self._refresh_sidebar_playlists()
            idx = combo.findData(playlist_id)
            combo.setCurrentIndex(idx if idx >= 0 else 0)
        else:
            if ok and name.strip() and not self.library_root:
                QMessageBox.information(self, "No library yet", "Set up your library first (Library page -> gear menu -> ADD SOURCES).")
            combo.setCurrentIndex(0)

    def _create_playlist(self):
        if not self.library_root:
            QMessageBox.information(self, "No library yet", "Set up your library first (Library page -> gear menu -> ADD SOURCES).")
            return
        name, ok = QInputDialog.getText(self, "New Playlist", "Playlist name:")
        if not ok or not name.strip():
            return
        conn = libdb.open_db(self.library_root)
        libdb.get_or_create_playlist(conn, name.strip())
        conn.close()
        self._refresh_sidebar_playlists()

    def _add_dropped_tracks_to_playlist(self, playlist_id: int, track_ids: list):
        """Songs dragged from a library table onto a sidebar playlist.
        Playlists are music-only, so audiobooks are skipped; songs already
        on the playlist aren't added twice. Confirms on the Log button
        rather than with a popup, so dragging several batches stays quick."""
        if not self.library_root:
            return
        from .library.playlists import write_m3u8
        conn = libdb.open_db(self.library_root)
        prow = conn.execute("SELECT name FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
        if prow is None:
            conn.close()
            return
        existing = libdb.playlist_track_ids(conn, playlist_id)
        added = skipped_books = 0
        for track_id in track_ids:
            row = conn.execute("SELECT kind FROM tracks WHERE id = ?", (track_id,)).fetchone()
            if row is None:
                continue
            if row["kind"] != "music":
                skipped_books += 1
                continue
            if track_id in existing:
                continue
            libdb.add_playlist_track(conn, playlist_id, track_id, libdb.next_playlist_position(conn, playlist_id))
            existing.add(track_id)
            added += 1
        if added:
            write_m3u8(str(Path(self.library_root) / "Playlists"), prow["name"],
                       libdb.playlist_track_paths_in_order(conn, playlist_id), self.library_root)
        conn.close()

        name = prow["name"]
        if added:
            message = f"Added {added} song{'' if added == 1 else 's'} to {name}"
        elif skipped_books:
            message = "Playlists are for songs. Audiobooks weren't added"
        else:
            message = f"Already on {name}"
        self._set_status("done" if added else "standby", message)
        self.console_strip.flash(message)
        self._write_log(f"[{ts()}] {message}")
        if added:
            self._refresh_sidebar_playlists()  # cover mosaic may change
            self.page_library.refresh()  # "In" counts
            if self.content_stack.currentWidget() is self.page_playlist and self.page_playlist._playlist_id == playlist_id:
                self.page_playlist.refresh()

    def _rename_playlist(self, playlist_id: int, new_name: str):
        if not self.library_root:
            return
        from .library.playlists import write_m3u8
        conn = libdb.open_db(self.library_root)
        row = conn.execute("SELECT name FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
        old_name = row["name"] if row else None
        libdb.rename_playlist(conn, playlist_id, new_name)
        paths = libdb.playlist_track_paths_in_order(conn, playlist_id)
        conn.close()

        playlists_dir = Path(self.library_root) / "Playlists"
        if old_name and old_name != new_name:
            old_m3u8 = playlists_dir / f"{old_name}.m3u8"
            if old_m3u8.exists():
                old_m3u8.unlink()
        write_m3u8(str(playlists_dir), new_name, paths, self.library_root)

        self._refresh_sidebar_playlists()
        if self.content_stack.currentWidget() is self.page_playlist:
            self.page_playlist.show_playlist(playlist_id, new_name)

    def _fix_tags_for_path(self, path: str, track_id: int):
        self._on_navigate("tags", None)
        self._load_tag_file_from_path(path)
        # Loading always clears this; set it after, so saving knows to re-file
        # the track back into the library instead of just rewriting its ID3 tags.
        self._pending_library_refile = track_id
        # Contextual entry (Fix Tags) always means the Single File sub-tab —
        # force it back in case Batch Apply was left active from an earlier
        # rail visit, so this never lands on the wrong sub-tab.
        self.tag_mode_tabs.setCurrentIndex(0)

    def _open_batch_tags_fresh(self):
        """The Tags rail button's own click — unlike the contextual entry
        points (Fix Tags, Send Segments to Tags), which load specific state
        on purpose, opening Tags cold should never show a previous session's
        leftover files."""
        self.batch_tag_list.clear()
        self.tag_mode_tabs.setCurrentIndex(1)

    def _on_navigate(self, kind: str, payload):
        """Single entry point for every page change — sidebar clicks, top
        bar tool buttons, and programmatic jumps (Fix Tags, Send Segments
        to Tags, Back from a series). Keeps the sidebar highlight, tool
        button state and top bar title in step with what's showing."""
        if kind == "library":
            kind = "songs"

        if kind == "playlist":
            playlist_id, name = payload
            self.page_playlist.show_playlist(playlist_id, name)
            self.content_stack.setCurrentWidget(self.page_playlist)
            self.sidebar.select_playlist(playlist_id)
            self._set_page_header("")  # the playlist page has its own big header
        elif kind == "audiobook_series":
            self.page_audiobook_series.show_series(payload)  # payload = series (Album tag) name
            self.content_stack.setCurrentWidget(self.page_audiobook_series)
            self.sidebar.select("audiobooks")
            self._set_page_header("")
        elif kind in LIBRARY_KINDS:
            self.content_stack.setCurrentWidget(self.page_library)
            self.page_library.show_section(kind)
            self.sidebar.select(kind)
            self._set_page_header(*self.page_library._section_header())
        elif kind in TOOL_PAGES:
            # Tools open in the drawer over whatever page you're on.
            self.tool_drawer.show_tool(kind)
            self._sync_tool_buttons()
            return
        else:
            return

        self.library_more_btn.setVisible(kind in LIBRARY_KINDS)
        self._sync_tool_buttons()

    def _on_tool_button(self, kind: str):
        """Top bar tool button: opens that tool in the drawer, or closes the
        drawer if that tool is already showing."""
        if self.tool_drawer.isVisible() and self.tool_drawer.current_kind == kind:
            self._close_tool_drawer()
            return
        if kind == "tags":
            # Opening Tags cold should never show a previous session's
            # leftover batch files; the contextual entry points (Fix Tags,
            # Send Segments to Tags) navigate directly instead.
            self._open_batch_tags_fresh()
        self._on_navigate(kind, None)

    def _update_youtube_badge(self):
        """"YouTube 3" on the top bar button while downloads are queued or
        running, so progress is visible with the drawer closed."""
        if not hasattr(self, "tool_buttons"):
            return
        try:
            pending = self.youtube_queue_list.count() + (1 if self.youtube_active_worker is not None else 0)
        except RuntimeError:
            return  # window is shutting down and the queue list is already gone
        btn = self.tool_buttons["youtube"]
        btn.setText(f"YouTube  {pending}" if pending else "YouTube")
        btn.setToolTip(f"{pending} download(s) left" if pending else TOOL_PAGES["youtube"][1])

    def _close_tool_drawer(self):
        self.tool_drawer.close_drawer()
        self._sync_tool_buttons()

    def _sync_tool_buttons(self):
        open_kind = self.tool_drawer.current_kind if self.tool_drawer.isVisible() else None
        for k, btn in self.tool_buttons.items():
            btn.setChecked(k == open_kind)

    def eventFilter(self, obj, event):
        if obj is self.body_widget and event.type() == QEvent.Type.Resize:
            if self.tool_drawer.isVisible():
                self.tool_drawer.reposition()
            if self.console_overlay.isVisible():
                self._reposition_console_overlay()
        return super().eventFilter(obj, event)

    def _return_to_audiobooks(self):
        """The audiobook series view's back button — returns to Library on
        the Audiobooks tab, with the SERIES sub-view active (where you'd
        have come from to get here in the first place)."""
        self._on_navigate("audiobooks", None)
        self.page_library.audiobooks_view_toggle.setCurrentText("Series")
        self.content_stack.setCurrentWidget(self.page_library)

    def _build_top_bar(self) -> QWidget:
        """Page title + one-line summary on the left; the tools (used a lot,
        but not part of browsing, so not in the sidebar) and the library /
        output menus on the right."""
        bar = QWidget()
        bar.setObjectName("topBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        self.page_title = QLabel("")
        self.page_title.setObjectName("pageTitle")
        self.page_subtitle = QLabel("")
        self.page_subtitle.setObjectName("pageSubtitle")
        title_col.addWidget(self.page_title)
        title_col.addWidget(self.page_subtitle)
        row.addLayout(title_col, stretch=1)

        self.tool_buttons: dict[str, QPushButton] = {}
        for kind, (label, summary) in TOOL_PAGES.items():
            btn = QPushButton(label)
            btn.setObjectName("toolButton")
            btn.setCheckable(True)
            btn.setToolTip(summary)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, k=kind: self._on_tool_button(k))
            row.addWidget(btn)
            self.tool_buttons[kind] = btn

        row.addSpacing(6)
        # Status dot + log toggle (used to be a full-width strip at the bottom).
        self.console_strip = self._build_console()
        row.addWidget(self.console_strip)
        # Library-wide actions (Add sources, Export, library folder) — the
        # menu itself belongs to LibraryView and is attached in _build_body.
        self.library_more_btn = QPushButton("⋯")
        self.library_more_btn.setObjectName("iconButton")
        self.library_more_btn.setToolTip("Library: add sources, export, library folder")
        row.addWidget(self.library_more_btn)

        # Output folder for Merge/Split/YouTube — occasional, so it's a menu.
        self.output_settings_btn = QPushButton("Output")
        self.output_settings_btn.setObjectName("iconButton")
        self.output_menu = QMenu(self)
        self.output_location_action = QAction("", self)
        self.output_location_action.setEnabled(False)
        choose_action = QAction("Choose folder…", self)
        choose_action.triggered.connect(self._choose_output_dir)
        open_action = QAction("Open folder", self)
        open_action.triggered.connect(self._open_output_dir)
        self.output_menu.addAction(self.output_location_action)
        self.output_menu.addSeparator()
        self.output_menu.addAction(choose_action)
        self.output_menu.addAction(open_action)
        self.output_settings_btn.setMenu(self.output_menu)
        row.addWidget(self.output_settings_btn)

        self._refresh_output_label()
        return bar

    def _set_page_header(self, title: str, subtitle: str = ""):
        self.page_title.setText(title)
        self.page_title.setVisible(bool(title))
        self.page_subtitle.setText(subtitle)
        self.page_subtitle.setVisible(bool(subtitle))

    def _on_library_header_changed(self, title: str, subtitle: str):
        if self.content_stack.currentWidget() is self.page_library:
            self._set_page_header(title, subtitle)

    def _on_library_section_changed(self, index: int):
        """Keeps the sidebar highlight right when the section changes from
        inside the library (e.g. Health's "Go to duplicates")."""
        if self.content_stack.currentWidget() is self.page_library:
            self.sidebar.select(self.page_library.current_section())

    def _refresh_output_label(self):
        text = self.output_dir if self.output_dir else "(no folder chosen yet)"
        self.output_location_action.setText(f"Output folder: {text}")
        self.output_settings_btn.setToolTip(text)

    def _build_merge_panel(self):
        widget, layout = tab_content("MERGE MODULE — combine multi-part audiobooks")

        hint = QLabel("Drag mp3s in, or click empty space in the list to browse — then drag to reorder. "
                      "Output is stream-copied (re-encodes only if formats mismatch).")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.merge_list = DropListWidget()
        self.merge_list.setMinimumHeight(110)
        layout.addWidget(self.merge_list)

        btn_row = QHBoxLayout()
        up_btn = QPushButton("▲ Up")
        down_btn = QPushButton("▼ Down")
        remove_btn = QPushButton("Remove")
        clear_btn = QPushButton("Clear")
        self.merge_btn = QPushButton("Merge")
        self.merge_btn.setObjectName("primary")

        up_btn.clicked.connect(lambda: self._move_selected(-1))
        down_btn.clicked.connect(lambda: self._move_selected(1))
        remove_btn.clicked.connect(self._remove_selected)
        clear_btn.clicked.connect(self.merge_list.clear)
        self.merge_btn.clicked.connect(self._run_merge)

        for b in (up_btn, down_btn, remove_btn, clear_btn):
            btn_row.addWidget(b)
        btn_row.addStretch(1)
        btn_row.addWidget(self.merge_btn)
        layout.addLayout(btn_row)

        dest_row = QHBoxLayout()
        dest_label = QLabel("File as:")
        dest_label.setObjectName("muted")
        dest_row.addWidget(dest_label)
        self.merge_destination_combo = QComboBox()
        self.merge_destination_combo.addItem("Music (library)", "music")
        self.merge_destination_combo.addItem("Audiobook (library)", "audiobook")
        self.merge_destination_combo.addItem("Output folder only", "output_only")
        self.merge_destination_combo.setCurrentIndex(1)  # Audiobook — matches this
        # panel's own "combine multi-part audiobooks" framing; Music/Output
        # folder only are still one click away for other uses of Merge.
        self.merge_destination_combo.currentIndexChanged.connect(self._on_merge_destination_changed)
        dest_row.addWidget(self.merge_destination_combo)

        merge_playlist_label = QLabel("Add to:")
        merge_playlist_label.setObjectName("muted")
        dest_row.addWidget(merge_playlist_label)
        self.merge_playlist_combo = QComboBox()
        self._refresh_playlist_combo(self.merge_playlist_combo)
        self.merge_playlist_combo.setEnabled(False)  # default destination is Audiobook, not Music
        self.merge_playlist_combo.currentIndexChanged.connect(
            lambda idx: self._on_playlist_combo_changed(self.merge_playlist_combo, idx)
        )
        dest_row.addWidget(self.merge_playlist_combo)
        layout.addLayout(dest_row)

        self.merge_then_split_check = QCheckBox("Then split into segments (uses the Split tab's segment length)")
        layout.addWidget(self.merge_then_split_check)

        row, self.merge_progress, self.merge_cancel_btn = progress_row(self._cancel_active)
        layout.addLayout(row)

        layout.addStretch(1)
        return widget

    def _build_split_panel(self):
        widget, layout = tab_content("SPLIT MODULE — chop into fixed-length segments")

        settings_row = QHBoxLayout()
        seg_label = QLabel("Segment length:")
        self.minutes_spin = QDoubleSpinBox()
        self.minutes_spin.setRange(0.1, 120.0)
        self.minutes_spin.setDecimals(1)
        self.minutes_spin.setSingleStep(0.5)
        self.minutes_spin.setValue(2.0)
        self.minutes_spin.setSuffix(" min")
        self.minutes_spin.setFixedWidth(100)
        settings_row.addWidget(seg_label)
        settings_row.addWidget(self.minutes_spin)
        settings_row.addStretch(1)
        layout.addLayout(settings_row)

        self.split_zone = SplitDropZone()
        self.split_zone.filesDropped.connect(self._run_split)
        layout.addWidget(self.split_zone)

        self.split_file_label = QLabel("")
        self.split_file_label.setObjectName("muted")
        layout.addWidget(self.split_file_label)

        row, self.split_progress, self.split_cancel_btn = progress_row(self._cancel_active)
        layout.addLayout(row)

        self.send_to_tags_btn = QPushButton("Send segments to Tags")
        self.send_to_tags_btn.setEnabled(False)
        self.send_to_tags_btn.clicked.connect(self._send_split_segments_to_tags)
        layout.addWidget(self.send_to_tags_btn)

        layout.addStretch(1)
        return widget

    def _build_youtube_panel(self):
        widget, layout = tab_content("YOUTUBE MODULE — download and convert to mp3")

        hint = QLabel("Paste one or more YouTube URLs, one per line — single videos or playlist links both "
                       "work (playlists expand into every video). Everything queues up, so you can keep "
                       "pasting more while downloads are already running.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.youtube_url_edit = QPlainTextEdit()
        self.youtube_url_edit.setPlaceholderText(
            "https://www.youtube.com/watch?v=...\nhttps://www.youtube.com/playlist?list=..."
        )
        self.youtube_url_edit.setFixedHeight(70)
        layout.addWidget(self.youtube_url_edit)

        # Options stacked one per row (label | dropdown) so they fit the
        # drawer's width; the main button sits under them.
        options = QFormLayout()
        options.setHorizontalSpacing(12)
        options.setVerticalSpacing(8)
        options.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.youtube_download_btn = QPushButton("Add to queue")
        self.youtube_download_btn.setObjectName("primary")
        self.youtube_download_btn.clicked.connect(self._add_to_youtube_queue)
        quality_label = QLabel("Quality")
        quality_label.setObjectName("muted")
        self.youtube_quality_combo = QComboBox()
        self.youtube_quality_combo.addItem("Best (VBR, largest files)", "0")
        self.youtube_quality_combo.addItem("320 kbps", "320")
        self.youtube_quality_combo.addItem("256 kbps", "256")
        self.youtube_quality_combo.addItem("192 kbps (default)", "192")
        self.youtube_quality_combo.addItem("128 kbps (smaller files)", "128")
        self.youtube_quality_combo.setCurrentIndex(3)
        self.youtube_quality_combo.currentIndexChanged.connect(self._persist_youtube_quality)
        options.addRow(quality_label, self.youtube_quality_combo)

        destination_label = QLabel("File as")
        destination_label.setObjectName("muted")
        self.youtube_destination_combo = QComboBox()
        self.youtube_destination_combo.addItem("Music (library)", "music")
        self.youtube_destination_combo.addItem("Audiobook (library)", "audiobook")
        self.youtube_destination_combo.addItem("Output folder only", "output_only")
        self.youtube_destination_combo.currentIndexChanged.connect(self._on_youtube_destination_changed)
        options.addRow(destination_label, self.youtube_destination_combo)

        playlist_label = QLabel("Add to playlist")
        playlist_label.setObjectName("muted")
        self.youtube_playlist_combo = QComboBox()
        self._refresh_playlist_combo(self.youtube_playlist_combo)
        self.youtube_playlist_combo.currentIndexChanged.connect(
            lambda idx: self._on_playlist_combo_changed(self.youtube_playlist_combo, idx)
        )
        options.addRow(playlist_label, self.youtube_playlist_combo)
        layout.addLayout(options)
        add_row = QHBoxLayout()
        add_row.addStretch(1)
        add_row.addWidget(self.youtube_download_btn)
        layout.addLayout(add_row)

        self.youtube_now_label = QLabel("Queue idle")
        self.youtube_now_label.setObjectName("muted")
        self.youtube_now_label.setWordWrap(True)
        layout.addWidget(self.youtube_now_label)

        progress, self.youtube_progress, self.youtube_cancel_btn = progress_row(self._cancel_youtube_current)
        self.youtube_cancel_btn.setEnabled(False)
        layout.addLayout(progress)

        queue_label = QLabel("Queued (not yet started):")
        queue_label.setObjectName("muted")
        layout.addWidget(queue_label)

        self.youtube_queue_list = QListWidget()
        self.youtube_queue_list.setObjectName("filePicker")
        self.youtube_queue_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.youtube_queue_list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.youtube_queue_list.setMinimumHeight(90)
        self.youtube_queue_list.model().rowsMoved.connect(lambda *a: self._persist_youtube_queue())
        for sig in (self.youtube_queue_list.model().rowsInserted, self.youtube_queue_list.model().rowsRemoved,
                    self.youtube_queue_list.model().modelReset):
            sig.connect(lambda *a: self._update_youtube_badge())
        layout.addWidget(self.youtube_queue_list)

        queue_btn_row = QHBoxLayout()
        remove_btn = QPushButton("Remove selected")
        remove_btn.clicked.connect(self._remove_selected_youtube_queue)
        clear_btn = QPushButton("Clear queue")
        clear_btn.clicked.connect(self._clear_youtube_queue)
        queue_btn_row.addWidget(remove_btn)
        queue_btn_row.addWidget(clear_btn)
        queue_btn_row.addStretch(1)
        layout.addLayout(queue_btn_row)

        layout.addStretch(1)
        return widget

    def _build_metadata_panel(self):
        widget, layout = tab_content("TAGS MODULE — edit ID3 metadata (title, artist, album, year, art)")

        self.tag_mode_tabs = QTabWidget()
        self.tag_mode_tabs.addTab(self._build_single_tag_panel(), "Single file")
        self.tag_mode_tabs.addTab(self._build_batch_tag_panel(), "Batch apply")
        layout.addWidget(self.tag_mode_tabs)

        return widget

    def _build_single_tag_panel(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(8)

        load_row = QHBoxLayout()
        self.single_tag_path_label = QLabel("(no file loaded)")
        self.single_tag_path_label.setObjectName("muted")
        self.single_tag_path_label.setWordWrap(True)
        load_btn = QPushButton("Load mp3")
        load_btn.clicked.connect(self._load_single_tag_file)
        load_row.addWidget(self.single_tag_path_label, stretch=1)
        load_row.addWidget(load_btn)
        layout.addLayout(load_row)

        form_row = QHBoxLayout()

        fields_col = QVBoxLayout()
        self.tag_title_edit = self._labeled_line_edit(fields_col, "Title")
        self.tag_artist_edit = self._labeled_line_edit(fields_col, "Artist / Author")
        self.tag_album_edit = self._labeled_line_edit(fields_col, "Album")
        year_track_row = QHBoxLayout()
        self.tag_year_edit = QLineEdit()
        self.tag_year_edit.setPlaceholderText("Year")
        self.tag_year_edit.setFixedWidth(90)
        self.tag_track_edit = QLineEdit()
        self.tag_track_edit.setPlaceholderText("Track #")
        self.tag_track_edit.setFixedWidth(90)
        year_track_row.addWidget(self.tag_year_edit)
        year_track_row.addWidget(self.tag_track_edit)
        year_track_row.addStretch(1)
        fields_col.addLayout(year_track_row)
        form_row.addLayout(fields_col, stretch=1)

        art_col = QVBoxLayout()
        self.single_tag_art = CoverArtThumbnail()
        art_col.addWidget(self.single_tag_art)
        art_btn = QPushButton("Choose artwork")
        art_btn.clicked.connect(self._choose_single_tag_art)
        art_col.addWidget(art_btn)
        art_col.addStretch(1)
        form_row.addLayout(art_col)

        layout.addLayout(form_row)

        self.single_tag_art_bytes = None
        self.single_tag_art_mime = None

        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self.tag_save_btn = QPushButton("Save")
        self.tag_save_btn.setObjectName("primary")
        self.tag_save_btn.setEnabled(False)
        self.tag_save_btn.clicked.connect(self._save_single_tag_file)
        save_row.addWidget(self.tag_save_btn)
        layout.addLayout(save_row)

        layout.addStretch(1)
        return widget

    def _labeled_line_edit(self, layout: QVBoxLayout, label_text: str) -> QLineEdit:
        label = QLabel(label_text)
        label.setObjectName("muted")
        edit = QLineEdit()
        layout.addWidget(label)
        layout.addWidget(edit)
        return edit

    def _build_batch_tag_panel(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(8)

        hint = QLabel("Drag mp3s in, click empty space in the list to browse, or use Add files "
                       "(e.g. all segments from one split), set shared fields, then apply. "
                       "Title is left untouched; track numbers are set 1, 2, 3... in list order.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.batch_tag_list = FilePickerList()
        layout.addWidget(self.batch_tag_list)

        list_btn_row = QHBoxLayout()
        add_btn = QPushButton("Add files")
        add_btn.clicked.connect(self._add_batch_tag_files)
        remove_btn = QPushButton("Remove selected")
        remove_btn.clicked.connect(self.batch_tag_list.remove_selected)
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self.batch_tag_list.clear)
        list_btn_row.addWidget(add_btn)
        list_btn_row.addWidget(remove_btn)
        list_btn_row.addWidget(clear_btn)
        list_btn_row.addStretch(1)
        layout.addLayout(list_btn_row)

        form_row = QHBoxLayout()
        fields_col = QVBoxLayout()
        self.batch_artist_edit = self._labeled_line_edit(fields_col, "Artist / Author")
        self.batch_album_edit = self._labeled_line_edit(fields_col, "Album")
        self.batch_year_edit = self._labeled_line_edit(fields_col, "Year")
        form_row.addLayout(fields_col, stretch=1)

        art_col = QVBoxLayout()
        self.batch_tag_art = CoverArtThumbnail()
        art_col.addWidget(self.batch_tag_art)
        art_btn = QPushButton("Choose artwork")
        art_btn.clicked.connect(self._choose_batch_tag_art)
        art_col.addWidget(art_btn)
        art_col.addStretch(1)
        form_row.addLayout(art_col)
        layout.addLayout(form_row)

        self.batch_tag_art_bytes = None
        self.batch_tag_art_mime = None

        apply_row = QHBoxLayout()
        self.batch_apply_btn = QPushButton("Apply to all")
        self.batch_apply_btn.setObjectName("primary")
        self.batch_apply_btn.clicked.connect(self._apply_batch_tags)
        apply_row.addStretch(1)
        apply_row.addWidget(self.batch_apply_btn)
        layout.addLayout(apply_row)

        progress, self.batch_tag_progress, self.batch_tag_cancel_btn = progress_row(self._cancel_batch_tags)
        layout.addLayout(progress)

        layout.addStretch(1)
        return widget

    def _build_console(self) -> LogButton:
        button = LogButton()
        button.clicked.connect(self._toggle_console)
        return button

    # ---- shared behavior ----

    def _toggle_console(self):
        expanding = not self.console_overlay.isVisible()
        if expanding:
            self._reposition_console_overlay()
            self.console_overlay.show()
            self.console_overlay.raise_()
        else:
            self.console_overlay.hide()
        self.console_strip.set_expanded(expanding)

    def _reposition_console_overlay(self):
        # Float over the bottom of the content area (not the sidebar).
        top_left = self.body_widget.mapTo(self.centralWidget(), QPoint(0, 0))
        width = self.body_widget.width()
        height = max(60, min(280, self.body_widget.height()))
        y = top_left.y() + self.body_widget.height() - height
        self.console_overlay.setGeometry(top_left.x(), y, width, height)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "console_overlay") and self.console_overlay.isVisible():
            self._reposition_console_overlay()

    def _write_log(self, text: str):
        self.console_overlay.append_log(text)

    def _set_status(self, state: str, text: str):
        self.console_strip.set_status(state, text)
        self.console_overlay.set_status(state, text)

    # ---- playback ----

    def _wire_player(self):
        self.page_library.playRequested.connect(self._play_from_queue)
        self.page_playlist.playRequested.connect(self._play_from_queue)
        self.page_audiobook_series.playRequested.connect(self._play_from_queue)

        self.now_playing_bar.playPauseClicked.connect(self.player.toggle_play_pause)
        self.now_playing_bar.nextClicked.connect(self.player.next)
        self.now_playing_bar.prevClicked.connect(self.player.prev)
        self.now_playing_bar.shuffleToggled.connect(self.player.set_shuffle)
        self.now_playing_bar.seekRequested.connect(self.player.seek)
        self.now_playing_bar.volumeChanged.connect(self.player.set_volume)

        self.player.positionChanged.connect(self.now_playing_bar.set_position)
        self.player.durationChanged.connect(self.now_playing_bar.set_duration)
        self.player.playbackStateChanged.connect(lambda s: self.now_playing_bar.set_playing(s == "playing"))
        self.player.shuffleChanged.connect(self.now_playing_bar.set_shuffle)
        self.player.nowPlayingChanged.connect(self._on_now_playing_changed)
        self.player.playbackError.connect(lambda msg: self._write_log(f"[{ts()}] Playback error: {msg}"))

    def _play_from_queue(self, tracks: list, index: int):
        self.player.play_queue(tracks, index)

    def _on_now_playing_changed(self, track: dict):
        self.now_playing_bar.set_track_info(track["title"], track["artist"], track.get("art"))
        self.page_library.set_now_playing_track(track["id"])
        self.page_playlist.set_now_playing_track(track["id"])
        self.page_audiobook_series.set_now_playing_track(track["id"])
        # Book/chapter order shouldn't get scrambled — TrackPlayer already
        # guarantees an audiobook queue is never actually shuffled
        # regardless of this button's state; disabling it too is the
        # visible half of that, not the correctness guarantee.
        self.now_playing_bar.set_shuffle_enabled(track.get("kind") != "audiobook")

    def _choose_output_dir(self):
        start_dir = self.output_dir or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose output folder", start_dir)
        if chosen:
            self.output_dir = chosen
            self.settings.setValue("output_dir", chosen)
            self._refresh_output_label()
            self._pump_youtube_queue()  # un-pauses a queue that was waiting on a folder

    def _open_output_dir(self):
        if not self.output_dir:
            return
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.output_dir))

    def _check_ffmpeg(self):
        if not shutil.which(FFMPEG):
            QMessageBox.critical(self, "ffmpeg not found", FFMPEG_INSTALL_HINT)

    def closeEvent(self, event):
        """Cancels every worker still running when the window closes. Just
        setting a cancel flag isn't enough on its own — a worker's own
        ffmpeg subprocess (loudness analysis on a large file, an export
        mid-encode) can still be in flight, and Windows does NOT kill child
        processes when their parent exits: an unmanaged one just keeps
        running as an orphan, still holding whatever file it has open,
        indefinitely. Each worker's cancel() now also kills its own live
        subprocess (see LoudnessAnalysisWorker/CopyTracksWorker/ExportWorker),
        so calling cancel() here is what actually prevents that instead of
        just stopping the QThread loop between files."""
        for worker in (self._loudness_worker, self._health_worker, self.active_worker, self.youtube_active_worker):
            if worker is not None and worker.isRunning():
                worker.cancel()
        super().closeEvent(event)

    def _startup(self):
        self._check_ffmpeg()
        # No eager output-folder prompt here — Merge/Split/YouTube each prompt
        # lazily, only when actually needed, via _prompt_for_output_dir below.
        # Explicit call required, not just the libraryChanged connection below
        # — LibraryView.__init__ already emits libraryChanged once during
        # _build_body(), before anything's connected to it, so relying on the
        # connection alone would mean the one-time catch-up pass over an
        # existing library never starts on a fresh launch (same reason
        # _refresh_sidebar_playlists() needs its own explicit call too).
        self._kick_off_loudness_analysis()

    def _ensure_output_dir(self) -> bool:
        """True once there's a usable output folder: the remembered one if it
        still exists, otherwise ask (pre-filled with the last choice). False
        if the user cancels — callers abort whatever needed the folder."""
        if self.output_dir and Path(self.output_dir).is_dir():
            return True
        self.output_dir = self._prompt_for_output_dir()
        self._refresh_output_label()
        return bool(self.output_dir)

    def _prompt_for_output_dir(self) -> str | None:
        """Cancel-friendly: returns None if the user backs out instead of forcing
        a choice. Callers must handle None by aborting the action that needed it —
        this is only ever called lazily, not at startup, so there's nothing to
        force the user into."""
        start_dir = self.settings.value("output_dir", str(Path.home()))
        chosen = QFileDialog.getExistingDirectory(self, "Choose output folder", start_dir)
        if not chosen:
            return None
        self.settings.setValue("output_dir", chosen)
        return chosen

    def _set_busy(self, busy: bool, kind: str | None = None):
        # Merge/Split/batch-tag remain mutually exclusive with each other, one
        # at a time. YouTube is a separate, independent lane (its own queue)
        # and is deliberately not gated by this — see the youtube_* methods.
        self.merge_btn.setEnabled(not busy)
        self.split_zone.setEnabled(not busy)
        self.batch_apply_btn.setEnabled(not busy)

        self.merge_cancel_btn.setEnabled(busy and kind == "merge")
        self.split_cancel_btn.setEnabled(busy and kind == "split")
        self.batch_tag_cancel_btn.setEnabled(busy and kind == "batch_tag")

        if busy:
            self._set_status("busy", "Processing")
        else:
            self._set_status("standby", "Standby")
            self.active_worker = None
            self.active_kind = None

    def _cancel_active(self):
        if self.active_worker is not None:
            self.active_worker.cancel()

    # ---- merge behavior ----

    def _move_selected(self, delta: int):
        row = self.merge_list.currentRow()
        if row < 0:
            return
        new_row = row + delta
        if not (0 <= new_row < self.merge_list.count()):
            return
        item = self.merge_list.takeItem(row)
        self.merge_list.insertItem(new_row, item)
        self.merge_list.setCurrentRow(new_row)

    def _remove_selected(self):
        for item in self.merge_list.selectedItems():
            self.merge_list.takeItem(self.merge_list.row(item))

    def _run_merge(self):
        if not self._ensure_output_dir():
            return
        files = self.merge_list.paths()
        if len(files) < 2:
            QMessageBox.warning(self, "Need at least 2 files", "Add two or more mp3 parts to merge.")
            return

        default_name = f"{Path(files[0]).stem}_merged"
        name, ok = QInputDialog.getText(self, "Name merged file", "File name (without extension):", text=default_name)
        if not ok:
            return
        name = name.strip() or default_name

        self.merge_progress.set_idle()
        self._set_busy(True, "merge")
        self.merge_worker = MergeWorker(files, self.output_dir, output_name=name)
        self.active_worker = self.merge_worker
        self.merge_worker.log.connect(self._write_log)
        self.merge_worker.progress.connect(self.merge_progress.set_progress)
        self.merge_worker.finished_ok.connect(self._on_merge_ok)
        self.merge_worker.finished_err.connect(self._on_merge_err)
        self.merge_worker.cancelled.connect(self._on_merge_cancelled)
        self.merge_worker.start()

    def _on_merge_ok(self, out_path: str):
        self.merge_progress.set_done("Merge complete")
        self._set_status("done", "Merge complete")
        self._set_busy(False)
        self.merge_list.clear()

        kind = self.merge_destination_combo.currentData()
        playlist_id = self.merge_playlist_combo.currentData() if kind == "music" else None
        self._file_into_library(out_path, playlist_id, kind)

        if self.merge_then_split_check.isChecked():
            self._on_navigate("split", None)
            self._run_split([out_path])

    def _on_merge_err(self, err: str):
        self.merge_progress.set_error("Merge failed")
        self._set_status("error", "Merge failed")
        self._set_busy(False)

    def _on_merge_cancelled(self):
        self.merge_progress.set_cancelled("Cancelled")
        self._set_status("error", "Merge cancelled")
        self._set_busy(False)

    # ---- split behavior ----

    def _run_split(self, files: list[str]):
        if not self._ensure_output_dir():
            return
        segment_seconds = int(self.minutes_spin.value() * 60)

        self._split_estimates = []
        total_estimate = 0
        unknown = False
        for f in files:
            duration = probe_duration_seconds(f)
            if duration is None:
                self._split_estimates.append(None)
                unknown = True
            else:
                est = max(1, math.ceil(duration / segment_seconds))
                self._split_estimates.append(est)
                total_estimate += est
        if unknown:
            self._write_log(f"[{ts()}] Estimated segments: {total_estimate}+ (duration unknown for some files)")
        else:
            self._write_log(f"[{ts()}] Estimated segments: ≈{total_estimate} total across {len(files)} file(s)")

        self.split_progress.set_idle()
        self.split_file_label.setText("")
        self.send_to_tags_btn.setEnabled(False)
        self._set_busy(True, "split")
        self.split_worker = SplitWorker(files, self.output_dir, segment_seconds)
        self.active_worker = self.split_worker
        self.split_worker.log.connect(self._write_log)
        self.split_worker.progress.connect(self.split_progress.set_progress)
        self.split_worker.file_progress.connect(self._on_split_file_progress)
        self.split_worker.finished_all.connect(self._on_split_done)
        self.split_worker.cancelled.connect(self._on_split_cancelled)
        self.split_worker.start()

    def _on_split_file_progress(self, index: int, total: int, name: str):
        estimate = self._split_estimates[index - 1] if index - 1 < len(self._split_estimates) else None
        suffix = f"  (≈{estimate} segments)" if estimate else ""
        self.split_file_label.setText(f"File {index} of {total}: {name}{suffix}")

    def _on_split_done(self, segment_paths: list):
        self.split_progress.set_done("Split complete")
        self._set_status("done", "Split complete")
        self._set_busy(False)
        self._last_split_segments = segment_paths
        self.send_to_tags_btn.setEnabled(bool(segment_paths))

    def _on_split_cancelled(self):
        self.split_progress.set_cancelled("Cancelled")
        self._set_status("error", "Split cancelled")
        self._set_busy(False)

    def _send_split_segments_to_tags(self):
        if not self._last_split_segments:
            return
        self.batch_tag_list.add_paths(self._last_split_segments)
        self._on_navigate("tags", None)
        self.tag_mode_tabs.setCurrentIndex(1)  # BATCH APPLY sub-tab
        self._write_log(f"[{ts()}] Sent {len(self._last_split_segments)} segment(s) to Tags -> Batch Apply.")

    # ---- youtube behavior (an independent queue — not gated by _set_busy) ----
    #
    # Each queue item's UserRole data is a dict: {"url", "title", "quality"}.
    # "title" starts as the raw URL for plain video links and is upgraded in
    # place once a background TitleLookupWorker resolves it; playlist entries
    # already have real titles from the flat playlist listing.

    def _add_to_youtube_queue(self):
        text = self.youtube_url_edit.toPlainText().strip()
        if not text:
            QMessageBox.warning(self, "No URL", "Paste one or more YouTube URLs first.")
            return
        if not self._ensure_output_dir():
            return

        urls = [line.strip() for line in text.splitlines() if line.strip()]
        self.youtube_url_edit.clear()
        quality = self.youtube_quality_combo.currentData()
        kind = self.youtube_destination_combo.currentData()
        playlist_id = self.youtube_playlist_combo.currentData() if kind == "music" else None
        if playlist_id == "__new__":
            playlist_id = None  # shouldn't happen (combo resets after creating), but never queue with a sentinel

        for url in urls:
            if looks_like_playlist_url(url):
                self._write_log(f"[{ts()}] Expanding playlist: {url}")
                expander = PlaylistExpandWorker(url)
                self._youtube_lookup_workers.append(expander)
                expander.expanded.connect(
                    lambda results, q=quality, p=playlist_id, k=kind: self._on_playlist_expanded(results, q, p, k)
                )
                expander.failed.connect(self._on_playlist_expand_failed)
                expander.finished.connect(lambda w=expander: self._forget_lookup_worker(w))
                expander.start()
            else:
                self._enqueue_youtube_url(url, url, quality, playlist_id, kind)
                lookup = TitleLookupWorker(url)
                self._youtube_lookup_workers.append(lookup)
                lookup.resolved.connect(self._on_title_resolved)
                lookup.failed.connect(self._on_title_lookup_failed)
                lookup.finished.connect(lambda w=lookup: self._forget_lookup_worker(w))
                lookup.start()

        self._pump_youtube_queue()

    def _forget_lookup_worker(self, worker):
        if worker in self._youtube_lookup_workers:
            self._youtube_lookup_workers.remove(worker)

    def _on_playlist_expanded(self, results: list, quality: str, playlist_id, kind: str = "music"):
        self._write_log(f"[{ts()}]   Found {len(results)} video(s) in playlist.")
        for url, title in results:
            self._enqueue_youtube_url(url, title, quality, playlist_id, kind)
        self._pump_youtube_queue()

    def _on_playlist_expand_failed(self, err: str):
        self._write_log(f"[{ts()}]   FAILED to read playlist: {err}")

    def _on_title_lookup_failed(self, url: str, err: str):
        # Non-fatal: the item just keeps showing its URL as the display text.
        self._write_log(f"[{ts()}]   Couldn't look up title for {url}: {err}")

    def _on_title_resolved(self, url: str, title: str):
        for i in range(self.youtube_queue_list.count()):
            item = self.youtube_queue_list.item(i)
            data = item.data(Qt.ItemDataRole.UserRole)
            if data["url"] == url:
                data["title"] = title
                item.setData(Qt.ItemDataRole.UserRole, data)
                item.setText(title)
                self._persist_youtube_queue()
                return
        if self.youtube_active_worker is not None and self.youtube_active_worker.url == url:
            self._current_download_title = title
            self.youtube_now_label.setText(f"Downloading: {title}")

    def _enqueue_youtube_url(self, url: str, title: str, quality: str, playlist_id=None, kind: str = "music"):
        if self.youtube_active_worker is not None and self.youtube_active_worker.url == url:
            return
        for i in range(self.youtube_queue_list.count()):
            if self.youtube_queue_list.item(i).data(Qt.ItemDataRole.UserRole)["url"] == url:
                return
        item = QListWidgetItem(title)
        item.setData(Qt.ItemDataRole.UserRole,
                     {"url": url, "title": title, "quality": quality, "playlist_id": playlist_id, "kind": kind})
        self.youtube_queue_list.addItem(item)
        self._persist_youtube_queue()

    def _remove_selected_youtube_queue(self):
        for item in self.youtube_queue_list.selectedItems():
            self.youtube_queue_list.takeItem(self.youtube_queue_list.row(item))
        self._persist_youtube_queue()

    def _clear_youtube_queue(self):
        count = self.youtube_queue_list.count()
        self.youtube_queue_list.clear()
        if count:
            self._write_log(f"[{ts()}] Cleared {count} queued item(s).")
        self._persist_youtube_queue()

    def _pump_youtube_queue(self):
        if self.youtube_active_worker is None and self.youtube_queue_list.count() > 0:
            if not self._ensure_output_dir():
                # e.g. resuming last session's queue with no usable folder —
                # keep everything queued rather than downloading to nowhere.
                self.youtube_now_label.setText("Queue paused: choose an output folder (Output menu) to continue")
                return
            item = self.youtube_queue_list.takeItem(0)
            data = item.data(Qt.ItemDataRole.UserRole)
            self._current_download_title = data["title"]
            self._current_download_playlist_id = data.get("playlist_id")
            self._current_download_kind = data.get("kind", "music")
            self.youtube_now_label.setText(f"Downloading: {data['title']}")
            self.youtube_progress.set_idle()
            self.youtube_worker = YouTubeWorker(data["url"], self.output_dir, data.get("quality", "192"))
            self.youtube_active_worker = self.youtube_worker
            self._update_youtube_badge()
            self.youtube_worker.log.connect(self._write_log)
            self.youtube_worker.progress.connect(self.youtube_progress.set_progress)
            self.youtube_worker.finished_ok.connect(self._on_youtube_ok)
            self.youtube_worker.finished_err.connect(self._on_youtube_err)
            self.youtube_worker.cancelled.connect(self._on_youtube_cancelled)
            self.youtube_worker.start()
        elif self.youtube_active_worker is None:
            self._current_download_title = None
            self._current_download_playlist_id = None
            self._current_download_kind = None
            self.youtube_now_label.setText("Queue idle")

        self.youtube_cancel_btn.setEnabled(self.youtube_active_worker is not None)
        self._persist_youtube_queue()

    def _cancel_youtube_current(self):
        if self.youtube_active_worker is not None:
            self.youtube_active_worker.cancel()

    def _on_youtube_ok(self, out_path: str):
        self.youtube_progress.set_done("Complete")
        self.youtube_active_worker = None
        self._update_youtube_badge()
        self._file_into_library(out_path, self._current_download_playlist_id, self._current_download_kind or "music")
        self._pump_youtube_queue()

    def _file_into_library(self, out_path: str, playlist_id, kind: str = "music"):
        """Files a completed file (a YouTube download, or a Merge output)
        according to the chosen destination: into the library as music
        (optionally into a playlist) or as an audiobook, or left as-is with
        no library filing at all. Music/audiobook filing is skipped quietly
        if no library is set up yet; output-folder-only needs no library."""
        if kind == "output_only":
            self._write_log(f"[{ts()}] Left in output folder: {Path(out_path).name}")
            return
        if not self.library_root:
            return
        from .library.migrate import file_single_track
        from .library.playlists import write_m3u8

        track_id, status = file_single_track(self.library_root, out_path, kind=kind)
        if track_id is None:
            self._write_log(f"[{ts()}] Could not file download into library: {status}")
            return
        if status == "added":
            self._write_log(f"[{ts()}] Filed into library ({kind}): {Path(out_path).name}")
        # libraryChanged (and therefore _kick_off_loudness_analysis) only
        # fires from page_library.refresh() — which this method only calls
        # conditionally, below, if Library happens to be the visible page —
        # so a download filed while the user's on another tab still needs
        # its own explicit trigger.
        self._kick_off_loudness_analysis()

        if kind == "music" and playlist_id is not None:
            conn = libdb.open_db(self.library_root)
            position = libdb.next_playlist_position(conn, playlist_id)
            libdb.add_playlist_track(conn, playlist_id, track_id, position)
            row = conn.execute("SELECT name FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
            paths = libdb.playlist_track_paths_in_order(conn, playlist_id)
            conn.close()
            if row:
                write_m3u8(str(Path(self.library_root) / "Playlists"), row["name"], paths, self.library_root)
                self._write_log(f"[{ts()}] Added to playlist: {row['name']}")
            if self.content_stack.currentWidget() is self.page_playlist:
                self.page_playlist.refresh()

        self._refresh_sidebar_playlists()
        if self.content_stack.currentWidget() is self.page_library:
            self.page_library.refresh()

    def _on_youtube_err(self, err: str):
        self.youtube_progress.set_error("Failed")
        self.youtube_active_worker = None
        self._update_youtube_badge()
        self._pump_youtube_queue()

    def _on_youtube_cancelled(self):
        self.youtube_progress.set_cancelled("Cancelled")
        self.youtube_active_worker = None
        self._update_youtube_badge()
        self._pump_youtube_queue()

    def _persist_youtube_quality(self):
        self.settings.setValue("youtube_quality", self.youtube_quality_combo.currentData())

    def _load_persisted_youtube_quality(self):
        saved = self.settings.value("youtube_quality", None)
        if saved is None:
            return
        idx = self.youtube_quality_combo.findData(str(saved))
        if idx >= 0:
            self.youtube_quality_combo.setCurrentIndex(idx)

    def _persist_youtube_queue(self):
        entries = []
        if self.youtube_active_worker is not None:
            entries.append({
                "url": self.youtube_active_worker.url,
                "title": self._current_download_title or self.youtube_active_worker.url,
                "quality": self.youtube_active_worker.quality,
                "playlist_id": self._current_download_playlist_id,
                "kind": self._current_download_kind or "music",
            })
        for i in range(self.youtube_queue_list.count()):
            entries.append(self.youtube_queue_list.item(i).data(Qt.ItemDataRole.UserRole))
        self.settings.setValue("youtube_queue", json.dumps(entries))

    def _load_persisted_youtube_queue(self):
        raw = self.settings.value("youtube_queue", "")
        if not raw:
            return
        try:
            entries = json.loads(raw)
        except (ValueError, TypeError):
            entries = []
        if not entries:
            return

        reply = QMessageBox.question(
            self,
            "Resume YouTube downloads?",
            f"Splice found {len(entries)} YouTube download(s) left over from last time. Resume them?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if reply == QMessageBox.StandardButton.Yes:
            for entry in entries:
                url = entry.get("url")
                if not url:
                    continue
                title = entry.get("title") or url
                quality = entry.get("quality", "192")
                playlist_id = entry.get("playlist_id")
                kind = entry.get("kind", "music")
                item = QListWidgetItem(title)
                item.setData(Qt.ItemDataRole.UserRole,
                             {"url": url, "title": title, "quality": quality, "playlist_id": playlist_id, "kind": kind})
                self.youtube_queue_list.addItem(item)
            self._pump_youtube_queue()
        else:
            self.settings.remove("youtube_queue")

    # ---- tagging behavior: single file ----

    def _load_single_tag_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load mp3", self.output_dir or str(Path.home()), "MP3 files (*.mp3)")
        if not path:
            return
        self._load_tag_file_from_path(path)

    def _load_tag_file_from_path(self, path: str):
        """Populates the single-file tag editor from a known path — shared by the
        LOAD MP3 file dialog and the Library view's "Fix Tags" shortcut."""
        self._pending_library_refile = None  # loading any file clears a stale pending refile
        self.single_tag_path = path
        self.single_tag_path_label.setText(Path(path).name)
        self.single_tag_path_label.setObjectName("")

        tags = tagging.read_tags(path)
        self.tag_title_edit.setText(tags["title"])
        self.tag_artist_edit.setText(tags["artist"])
        self.tag_album_edit.setText(tags["album"])
        self.tag_year_edit.setText(tags["date"])
        self.tag_track_edit.setText(tags["tracknumber"])
        self.single_tag_art.set_art(tags["art"])
        self.single_tag_art_bytes = None
        self.single_tag_art_mime = None
        self.tag_save_btn.setEnabled(True)

    def _choose_single_tag_art(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose artwork", str(Path.home()), "Images (*.jpg *.jpeg *.png)")
        if not path:
            return
        self.single_tag_art_bytes = Path(path).read_bytes()
        self.single_tag_art_mime = tagging.guess_mime(path)
        self.single_tag_art.set_art(self.single_tag_art_bytes)

    def _save_single_tag_file(self):
        if not getattr(self, "single_tag_path", None):
            return
        fields = {
            "title": self.tag_title_edit.text(),
            "artist": self.tag_artist_edit.text(),
            "album": self.tag_album_edit.text(),
            "date": self.tag_year_edit.text(),
            "tracknumber": self.tag_track_edit.text(),
        }
        try:
            tagging.write_tags(self.single_tag_path, fields, self.single_tag_art_bytes, self.single_tag_art_mime)
        except Exception as e:
            self._write_log(f"[{ts()}] TAGS FAILED: {e}")
            self._set_status("error", "Tag save failed")
            return
        self._write_log(f"[{ts()}] Tags saved -> {Path(self.single_tag_path).name}")
        self._set_status("done", "Tags saved")

        if self._pending_library_refile is not None:
            track_id = self._pending_library_refile
            self._pending_library_refile = None
            self._refile_library_track(track_id)

    def _refile_library_track(self, track_id: int):
        """After saving tags for a library track, re-file it into its correct
        Artist/Album folder if the new tags changed where it belongs, and
        refresh the index. Originally built so a track actually leaves
        Unsorted once it's properly tagged (editing ID3 tags alone doesn't
        move the file or update the database); reused for editing tags on
        tracks already filed into Songs/Audiobooks or a playlist, since
        retagging can change a music track's destination folder either way.
        If the track belongs to any playlists and got moved, their .m3u8
        files are rewritten too, since they store paths, not track ids."""
        if not self.library_root:
            return
        from .library.migrate import refile_track
        try:
            moved = refile_track(self.library_root, track_id)
        except Exception as e:
            self._write_log(f"[{ts()}] Could not re-file library track: {e}")
            return
        if moved:
            self._write_log(f"[{ts()}] Re-filed into the library.")
            from .library.playlists import write_m3u8
            conn = libdb.open_db(self.library_root)
            for playlist_id in libdb.playlists_containing_track(conn, track_id):
                row = conn.execute("SELECT name FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
                if row:
                    paths = libdb.playlist_track_paths_in_order(conn, playlist_id)
                    write_m3u8(str(Path(self.library_root) / "Playlists"), row["name"], paths, self.library_root)
            conn.close()
        self.page_library.refresh()
        if self.content_stack.currentWidget() is self.page_playlist:
            self.page_playlist.refresh()

    # ---- tagging behavior: batch ----

    def _add_batch_tag_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Add mp3 files", self.output_dir or str(Path.home()), "MP3 files (*.mp3)")
        if paths:
            self.batch_tag_list.add_paths(paths)

    def _choose_batch_tag_art(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose artwork", str(Path.home()), "Images (*.jpg *.jpeg *.png)")
        if not path:
            return
        self.batch_tag_art_bytes = Path(path).read_bytes()
        self.batch_tag_art_mime = tagging.guess_mime(path)
        self.batch_tag_art.set_art(self.batch_tag_art_bytes)
        self.settings.setValue("batch_tag_art_path", path)

    def _load_persisted_batch_tag_defaults(self):
        self.batch_artist_edit.setText(str(self.settings.value("batch_tag_artist", "") or ""))
        self.batch_album_edit.setText(str(self.settings.value("batch_tag_album", "") or ""))
        self.batch_year_edit.setText(str(self.settings.value("batch_tag_year", "") or ""))

        art_path = str(self.settings.value("batch_tag_art_path", "") or "")
        if art_path and Path(art_path).exists():
            try:
                data = Path(art_path).read_bytes()
            except OSError:
                return
            self.batch_tag_art_bytes = data
            self.batch_tag_art_mime = tagging.guess_mime(art_path)
            self.batch_tag_art.set_art(data)

    def _cancel_batch_tags(self):
        self._batch_tag_cancel_requested = True

    def _apply_batch_tags(self):
        paths = self.batch_tag_list.paths()
        if not paths:
            QMessageBox.warning(self, "No files", "Add mp3 files to tag first.")
            return
        fields = {
            "artist": self.batch_artist_edit.text(),
            "album": self.batch_album_edit.text(),
            "date": self.batch_year_edit.text(),
        }
        self.settings.setValue("batch_tag_artist", fields["artist"])
        self.settings.setValue("batch_tag_album", fields["album"])
        self.settings.setValue("batch_tag_year", fields["date"])
        self._batch_tag_cancel_requested = False
        self.batch_tag_progress.set_idle()
        self._set_busy(True, "batch_tag")

        total = len(paths)
        cancelled = False
        for i, path in enumerate(paths, start=1):
            QApplication.processEvents()
            if self._batch_tag_cancel_requested:
                cancelled = True
                break
            try:
                tagging.write_tags(path, {**fields, "tracknumber": str(i)}, self.batch_tag_art_bytes, self.batch_tag_art_mime)
            except Exception as e:
                self._write_log(f"[{ts()}]   FAILED {Path(path).name}: {e}")
                continue
            self._write_log(f"[{ts()}]   Tagged ({i}/{total}): {Path(path).name}")
            self.batch_tag_progress.set_progress(int(i / total * 100))

        if cancelled:
            self.batch_tag_progress.set_cancelled("Cancelled")
            self._write_log(f"[{ts()}]   Cancelled — already-tagged files above were left as tagged.")
            self._set_status("error", "Batch tagging cancelled")
        else:
            self.batch_tag_progress.set_done("Complete")
            self._set_status("done", "Batch tagging complete")
        self._set_busy(False)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")  # ensures the stylesheet renders consistently instead of
    # fighting the native macOS/Windows widget chrome (source of stray bezel artifacts)
    load_bundled_fonts()
    app.setPalette(build_palette())
    app.setStyleSheet(STYLESHEET)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
