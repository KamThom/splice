"""Main application window: tabbed Merge / Split / YouTube / Tags modules plus a shared console."""

import json
import math
import shutil
import sys
from pathlib import Path

from PyQt6.QtCore import QSettings, Qt, QTimer, QUrl
from PyQt6.QtGui import QDesktopServices, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import tagging
from .styles import STYLESHEET
from .widgets import CoverArtThumbnail, DropListWidget, FilePickerList, LevelGauge, SplitDropZone, StatusDot
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


def panel(title: str) -> tuple[QFrame, QVBoxLayout]:
    """Bordered panel frame — used only for the console, which sits outside the tabs."""
    frame = QFrame()
    frame.setObjectName("panel")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 14)
    layout.setSpacing(10)
    label = QLabel(title)
    label.setObjectName("panelTitle")
    layout.addWidget(label)
    return frame, layout


def tab_content(title: str) -> tuple[QWidget, QVBoxLayout]:
    """Borderless content container for a tab page — QTabWidget::pane already
    supplies the surrounding border, so a nested panel() frame here would double it up."""
    widget = QWidget()
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(14, 12, 14, 14)
    layout.setSpacing(10)
    label = QLabel(title)
    label.setObjectName("panelTitle")
    layout.addWidget(label)
    return widget, layout


def progress_row(cancel_slot) -> tuple[QHBoxLayout, LevelGauge, QPushButton]:
    row = QHBoxLayout()
    gauge = LevelGauge()
    cancel_btn = QPushButton("CANCEL")
    cancel_btn.setEnabled(False)
    cancel_btn.clicked.connect(cancel_slot)
    row.addWidget(gauge, stretch=1)
    row.addWidget(cancel_btn)
    return row, gauge, cancel_btn


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("Thomas", "Splice")
        self.output_dir = None
        self.active_worker = None
        self.active_kind = None
        self._batch_tag_cancel_requested = False
        self._last_split_segments = []
        self._split_estimates = []
        self.youtube_active_worker = None
        self._youtube_lookup_workers = []
        self._current_download_title = None

        self.setWindowTitle("Splice")
        self.resize(640, 840)
        self._build_ui()
        self._load_persisted_batch_tag_defaults()
        self._load_persisted_youtube_quality()
        QTimer.singleShot(200, self._startup)
        QTimer.singleShot(350, self._load_persisted_youtube_queue)

    # ---- UI construction ----

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        layout.addLayout(self._build_header())
        layout.addLayout(self._build_output_bar())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_merge_panel(), "MERGE")
        self.tabs.addTab(self._build_split_panel(), "SPLIT")
        self.tabs.addTab(self._build_youtube_panel(), "YOUTUBE")
        self.tabs.addTab(self._build_metadata_panel(), "TAGS")
        layout.addWidget(self.tabs)

        layout.addWidget(self._build_console(), stretch=1)

    def _build_header(self):
        row = QVBoxLayout()
        row.setSpacing(2)
        title = QLabel("SPLICE")
        title.setObjectName("appTitle")
        subtitle = QLabel("AUDIO SPLICING UNIT // MK.III")
        subtitle.setObjectName("appSubtitle")
        rule = QFrame()
        rule.setObjectName("rule")
        row.addWidget(title)
        row.addWidget(subtitle)
        row.addSpacing(6)
        row.addWidget(rule)
        return row

    def _build_output_bar(self):
        row = QHBoxLayout()
        self.output_btn = QPushButton()
        self.output_btn.setObjectName("outputBar")
        self.output_btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.output_btn.clicked.connect(self._choose_output_dir)
        self._refresh_output_label()

        open_btn = QPushButton("OPEN")
        open_btn.setFixedWidth(70)
        open_btn.clicked.connect(self._open_output_dir)

        row.addWidget(self.output_btn, stretch=1)
        row.addWidget(open_btn)
        return row

    def _refresh_output_label(self):
        if self.output_dir:
            self.output_btn.setText(f"OUTPUT ▸  {self.output_dir}   (click to change)")
        else:
            self.output_btn.setText("OUTPUT ▸  (no folder chosen yet)   (click to change)")

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
        up_btn = QPushButton("▲ UP")
        down_btn = QPushButton("▼ DOWN")
        remove_btn = QPushButton("REMOVE")
        clear_btn = QPushButton("CLEAR")
        self.merge_btn = QPushButton("MERGE")
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

        self.send_to_tags_btn = QPushButton("SEND SEGMENTS TO TAGS")
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

        add_row = QHBoxLayout()
        self.youtube_download_btn = QPushButton("ADD TO QUEUE")
        self.youtube_download_btn.setObjectName("primary")
        self.youtube_download_btn.clicked.connect(self._add_to_youtube_queue)
        add_row.addWidget(self.youtube_download_btn)
        add_row.addStretch(1)
        quality_label = QLabel("Quality:")
        quality_label.setObjectName("muted")
        add_row.addWidget(quality_label)
        self.youtube_quality_combo = QComboBox()
        self.youtube_quality_combo.addItem("Best (VBR, largest files)", "0")
        self.youtube_quality_combo.addItem("320 kbps", "320")
        self.youtube_quality_combo.addItem("256 kbps", "256")
        self.youtube_quality_combo.addItem("192 kbps (default)", "192")
        self.youtube_quality_combo.addItem("128 kbps (smaller files)", "128")
        self.youtube_quality_combo.setCurrentIndex(3)
        self.youtube_quality_combo.currentIndexChanged.connect(self._persist_youtube_quality)
        add_row.addWidget(self.youtube_quality_combo)
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
        self.youtube_queue_list.setMaximumHeight(100)
        self.youtube_queue_list.model().rowsMoved.connect(lambda *a: self._persist_youtube_queue())
        layout.addWidget(self.youtube_queue_list)

        queue_btn_row = QHBoxLayout()
        remove_btn = QPushButton("REMOVE SELECTED")
        remove_btn.clicked.connect(self._remove_selected_youtube_queue)
        clear_btn = QPushButton("CLEAR QUEUE")
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
        self.tag_mode_tabs.addTab(self._build_single_tag_panel(), "SINGLE FILE")
        self.tag_mode_tabs.addTab(self._build_batch_tag_panel(), "BATCH APPLY")
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
        load_btn = QPushButton("LOAD MP3")
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
        art_btn = QPushButton("CHOOSE ARTWORK")
        art_btn.clicked.connect(self._choose_single_tag_art)
        art_col.addWidget(art_btn)
        art_col.addStretch(1)
        form_row.addLayout(art_col)

        layout.addLayout(form_row)

        self.single_tag_art_bytes = None
        self.single_tag_art_mime = None

        save_row = QHBoxLayout()
        save_row.addStretch(1)
        self.tag_save_btn = QPushButton("SAVE")
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

        hint = QLabel("Drag mp3s in, click empty space in the list to browse, or use ADD FILES "
                       "(e.g. all segments from one split), set shared fields, then apply. "
                       "Title is left untouched; track numbers are set 1, 2, 3... in list order.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.batch_tag_list = FilePickerList()
        layout.addWidget(self.batch_tag_list)

        list_btn_row = QHBoxLayout()
        add_btn = QPushButton("ADD FILES")
        add_btn.clicked.connect(self._add_batch_tag_files)
        remove_btn = QPushButton("REMOVE SELECTED")
        remove_btn.clicked.connect(self.batch_tag_list.remove_selected)
        clear_btn = QPushButton("CLEAR")
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
        art_btn = QPushButton("CHOOSE ARTWORK")
        art_btn.clicked.connect(self._choose_batch_tag_art)
        art_col.addWidget(art_btn)
        art_col.addStretch(1)
        form_row.addLayout(art_col)
        layout.addLayout(form_row)

        self.batch_tag_art_bytes = None
        self.batch_tag_art_mime = None

        apply_row = QHBoxLayout()
        self.batch_apply_btn = QPushButton("APPLY TO ALL")
        self.batch_apply_btn.setObjectName("primary")
        self.batch_apply_btn.clicked.connect(self._apply_batch_tags)
        apply_row.addStretch(1)
        apply_row.addWidget(self.batch_apply_btn)
        layout.addLayout(apply_row)

        progress, self.batch_tag_progress, self.batch_tag_cancel_btn = progress_row(self._cancel_batch_tags)
        layout.addLayout(progress)

        layout.addStretch(1)
        return widget

    def _build_console(self):
        frame, layout = panel("CONSOLE")
        status_row = QHBoxLayout()
        self.status_dot = StatusDot()
        self.status_label = QLabel("STANDBY")
        self.status_label.setObjectName("muted")
        status_row.addWidget(self.status_dot)
        status_row.addWidget(self.status_label)
        status_row.addStretch(1)
        layout.addLayout(status_row)

        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        font = QFont("Menlo")
        font.setStyleHint(QFont.StyleHint.Monospace)
        self.log_box.setFont(font)
        layout.addWidget(self.log_box)
        return frame

    # ---- shared behavior ----

    def _write_log(self, text: str):
        self.log_box.appendPlainText(text)

    def _set_status(self, state: str, text: str):
        self.status_dot.set_state(state)
        self.status_label.setText(text)

    def _choose_output_dir(self):
        start_dir = self.output_dir or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Choose output folder", start_dir)
        if chosen:
            self.output_dir = chosen
            self.settings.setValue("output_dir", chosen)
            self._refresh_output_label()

    def _open_output_dir(self):
        if not self.output_dir:
            return
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.output_dir))

    def _check_ffmpeg(self):
        if not shutil.which(FFMPEG):
            QMessageBox.critical(self, "ffmpeg not found", FFMPEG_INSTALL_HINT)

    def _startup(self):
        self._check_ffmpeg()
        self.output_dir = self._prompt_for_output_dir()
        self._refresh_output_label()

    def _prompt_for_output_dir(self) -> str:
        start_dir = self.settings.value("output_dir", str(Path.home()))
        while True:
            chosen = QFileDialog.getExistingDirectory(self, "Choose output folder for Splice", start_dir)
            if chosen:
                self.settings.setValue("output_dir", chosen)
                return chosen
            reply = QMessageBox.question(
                self,
                "No folder chosen",
                "Splice needs an output folder to continue. Choose one now?",
                QMessageBox.StandardButton.Retry | QMessageBox.StandardButton.Close,
                QMessageBox.StandardButton.Retry,
            )
            if reply == QMessageBox.StandardButton.Close:
                sys.exit(0)

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
            self._set_status("busy", "PROCESSING")
        else:
            self._set_status("standby", "STANDBY")
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
        if not self.output_dir:
            self.output_dir = self._prompt_for_output_dir()
            self._refresh_output_label()
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
        self.merge_progress.set_done("MERGE COMPLETE")
        self._set_status("done", "MERGE COMPLETE")
        self._set_busy(False)
        self.merge_list.clear()

        if self.merge_then_split_check.isChecked():
            self.tabs.setCurrentIndex(1)  # SPLIT tab
            self._run_split([out_path])

    def _on_merge_err(self, err: str):
        self.merge_progress.set_error("MERGE FAILED")
        self._set_status("error", "MERGE FAILED")
        self._set_busy(False)

    def _on_merge_cancelled(self):
        self.merge_progress.set_cancelled("CANCELLED")
        self._set_status("error", "MERGE CANCELLED")
        self._set_busy(False)

    # ---- split behavior ----

    def _run_split(self, files: list[str]):
        if not self.output_dir:
            self.output_dir = self._prompt_for_output_dir()
            self._refresh_output_label()
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
        self.split_file_label.setText(f"FILE {index} / {total} — {name}{suffix}")

    def _on_split_done(self, segment_paths: list):
        self.split_progress.set_done("SPLIT COMPLETE")
        self._set_status("done", "SPLIT COMPLETE")
        self._set_busy(False)
        self._last_split_segments = segment_paths
        self.send_to_tags_btn.setEnabled(bool(segment_paths))

    def _on_split_cancelled(self):
        self.split_progress.set_cancelled("CANCELLED")
        self._set_status("error", "SPLIT CANCELLED")
        self._set_busy(False)

    def _send_split_segments_to_tags(self):
        if not self._last_split_segments:
            return
        self.batch_tag_list.add_paths(self._last_split_segments)
        self.tabs.setCurrentIndex(3)  # TAGS tab
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
        if not self.output_dir:
            self.output_dir = self._prompt_for_output_dir()
            self._refresh_output_label()

        urls = [line.strip() for line in text.splitlines() if line.strip()]
        self.youtube_url_edit.clear()
        quality = self.youtube_quality_combo.currentData()

        for url in urls:
            if looks_like_playlist_url(url):
                self._write_log(f"[{ts()}] Expanding playlist: {url}")
                expander = PlaylistExpandWorker(url)
                self._youtube_lookup_workers.append(expander)
                expander.expanded.connect(lambda results, q=quality: self._on_playlist_expanded(results, q))
                expander.failed.connect(self._on_playlist_expand_failed)
                expander.finished.connect(lambda w=expander: self._forget_lookup_worker(w))
                expander.start()
            else:
                self._enqueue_youtube_url(url, url, quality)
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

    def _on_playlist_expanded(self, results: list, quality: str):
        self._write_log(f"[{ts()}]   Found {len(results)} video(s) in playlist.")
        for url, title in results:
            self._enqueue_youtube_url(url, title, quality)
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

    def _enqueue_youtube_url(self, url: str, title: str, quality: str):
        if self.youtube_active_worker is not None and self.youtube_active_worker.url == url:
            return
        for i in range(self.youtube_queue_list.count()):
            if self.youtube_queue_list.item(i).data(Qt.ItemDataRole.UserRole)["url"] == url:
                return
        item = QListWidgetItem(title)
        item.setData(Qt.ItemDataRole.UserRole, {"url": url, "title": title, "quality": quality})
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
            item = self.youtube_queue_list.takeItem(0)
            data = item.data(Qt.ItemDataRole.UserRole)
            self._current_download_title = data["title"]
            self.youtube_now_label.setText(f"Downloading: {data['title']}")
            self.youtube_progress.set_idle()
            self.youtube_worker = YouTubeWorker(data["url"], self.output_dir, data.get("quality", "192"))
            self.youtube_active_worker = self.youtube_worker
            self.youtube_worker.log.connect(self._write_log)
            self.youtube_worker.progress.connect(self.youtube_progress.set_progress)
            self.youtube_worker.finished_ok.connect(self._on_youtube_ok)
            self.youtube_worker.finished_err.connect(self._on_youtube_err)
            self.youtube_worker.cancelled.connect(self._on_youtube_cancelled)
            self.youtube_worker.start()
        elif self.youtube_active_worker is None:
            self._current_download_title = None
            self.youtube_now_label.setText("Queue idle")

        self.youtube_cancel_btn.setEnabled(self.youtube_active_worker is not None)
        self._persist_youtube_queue()

    def _cancel_youtube_current(self):
        if self.youtube_active_worker is not None:
            self.youtube_active_worker.cancel()

    def _on_youtube_ok(self, out_path: str):
        self.youtube_progress.set_done("COMPLETE")
        self.youtube_active_worker = None
        self._pump_youtube_queue()

    def _on_youtube_err(self, err: str):
        self.youtube_progress.set_error("FAILED")
        self.youtube_active_worker = None
        self._pump_youtube_queue()

    def _on_youtube_cancelled(self):
        self.youtube_progress.set_cancelled("CANCELLED")
        self.youtube_active_worker = None
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
                item = QListWidgetItem(title)
                item.setData(Qt.ItemDataRole.UserRole, {"url": url, "title": title, "quality": quality})
                self.youtube_queue_list.addItem(item)
            self._pump_youtube_queue()
        else:
            self.settings.remove("youtube_queue")

    # ---- tagging behavior: single file ----

    def _load_single_tag_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load mp3", self.output_dir or str(Path.home()), "MP3 files (*.mp3)")
        if not path:
            return
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
            self._set_status("error", "TAG SAVE FAILED")
            return
        self._write_log(f"[{ts()}] Tags saved -> {Path(self.single_tag_path).name}")
        self._set_status("done", "TAGS SAVED")

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
            self.batch_tag_progress.set_cancelled("CANCELLED")
            self._write_log(f"[{ts()}]   Cancelled — already-tagged files above were left as tagged.")
            self._set_status("error", "BATCH TAGGING CANCELLED")
        else:
            self.batch_tag_progress.set_done("COMPLETE")
            self._set_status("done", "BATCH TAGGING COMPLETE")
        self._set_busy(False)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")  # ensures the stylesheet renders consistently instead of
    # fighting the native macOS/Windows widget chrome (source of stray bezel artifacts)
    app.setStyleSheet(STYLESHEET)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
