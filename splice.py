#!/usr/bin/env python3
"""Splice — merge and split mp3 audiobooks into fixed-length segments."""

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from PyQt6.QtCore import QSettings, Qt, QThread, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QDragEnterEvent, QDropEvent, QFont
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

FFMPEG = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe") or "ffmpeg"
FFMPEG_INSTALL_HINT = (
    "ffmpeg was not found on your PATH.\n\n"
    "macOS:   brew install ffmpeg\n"
    "Windows: winget install ffmpeg  (or choco install ffmpeg / scoop install ffmpeg)\n\n"
    "Then restart this app."
)
DEFAULT_OUTPUT_DIR = str(Path.home() / "Documents" / "Splice")

# --- "Odyssey" retro-futurism palette — warm, rounded, analog-console feel ---
# (deliberately distinct from the wiki's Field Dossier brand colors)
BG = "#43403A"          # warm taupe chassis, lighter than a typical dark theme
SURFACE = "#57534B"      # panel surface, one step up from chassis
CONSOLE = "#2B2924"      # CRT-readout well — stays dark on purpose for phosphor contrast
INK = "#F6EFDD"          # warm cream text
MUTED = "#C9C0AC"        # muted cream for secondary text
BORDER = "#7A7568"       # soft warm border, no hard black edges
ORANGE = "#FF7A33"       # primary accent — hazard-tape orange, warmed up
TEAL = "#3FB8A6"         # secondary accent — retro console teal
GREEN = "#8FD19E"        # success — soft phosphor green, not neon
RED = "#E0664F"          # error — warm rust red

RADIUS = 10
RADIUS_SM = 7

STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {BG};
    color: {INK};
    font-family: "Space Mono", "Menlo", "Consolas", "Courier New", monospace;
    font-size: 12px;
}}
QFrame#panel {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QLabel {{
    background: transparent;
}}
QLabel#panelTitle {{
    color: {TEAL};
    font-weight: bold;
    font-size: 13px;
    letter-spacing: 1px;
}}
QLabel#appTitle {{
    color: {ORANGE};
    font-weight: bold;
    font-size: 26px;
    letter-spacing: 2px;
}}
QLabel#appSubtitle {{
    color: {MUTED};
    font-size: 11px;
    letter-spacing: 3px;
}}
QLabel#muted {{
    color: {MUTED};
}}
QFrame#rule {{
    background-color: {TEAL};
    max-height: 3px;
    min-height: 3px;
    border-radius: 1px;
}}
QPushButton {{
    background-color: {BG};
    color: {INK};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 7px 16px;
}}
QPushButton:hover {{
    border-color: {ORANGE};
    color: {ORANGE};
}}
QPushButton:disabled {{
    color: {MUTED};
    border-color: {BORDER};
}}
QPushButton#primary {{
    background-color: {ORANGE};
    color: {CONSOLE};
    font-weight: bold;
    border: none;
    border-radius: {RADIUS_SM}px;
}}
QPushButton#primary:hover {{
    background-color: #FF9459;
}}
QPushButton#primary:disabled {{
    background-color: {BORDER};
    color: {MUTED};
}}
QPushButton#outputBar {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    color: {MUTED};
    text-align: left;
    padding: 11px 16px;
}}
QPushButton#outputBar:hover {{
    border-color: {TEAL};
    color: {INK};
}}
QPlainTextEdit {{
    background-color: {CONSOLE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    color: {GREEN};
    padding: 6px;
}}
QDoubleSpinBox {{
    background-color: {CONSOLE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    color: {INK};
    padding: 5px;
}}
QDoubleSpinBox:focus {{
    border-color: {ORANGE};
}}
"""


def extract_dropped_mp3s(event) -> list[str]:
    paths = []
    for url in event.mimeData().urls():
        path = url.toLocalFile()
        if path.lower().endswith(".mp3"):
            paths.append(path)
    return paths


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    i = 1
    while True:
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def escape_concat_path(path: str) -> str:
    return path.replace("'", "'\\''")


def ts() -> str:
    return time.strftime("%H:%M:%S")


class StatusDot(QLabel):
    def __init__(self):
        super().__init__()
        self.setFixedSize(12, 12)
        self.set_state("standby")

    def set_state(self, state: str):
        color = {"standby": MUTED, "busy": ORANGE, "done": GREEN, "error": RED}[state]
        self.setStyleSheet(f"background-color: {color}; border-radius: 6px;")


class DropListWidget(QListWidget):
    """List of files to merge — external drops append, internal drag reorders."""

    filesDropped = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._set_idle_style()

    def _base_style(self, border_color: str) -> str:
        return (
            f"QListWidget {{ border: 2px dashed {border_color}; border-radius: 12px; "
            f"background-color: {CONSOLE}; color: {INK}; padding: 4px; }}"
            f"QListWidget::item {{ padding: 4px 6px; border-radius: 4px; }}"
            f"QListWidget::item:selected {{ background-color: {TEAL}; color: {CONSOLE}; }}"
        )

    def _set_idle_style(self):
        self.setStyleSheet(self._base_style(BORDER))

    def _set_hover_style(self):
        self.setStyleSheet(self._base_style(ORANGE))

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            self._set_hover_style()
            event.acceptProposedAction()
        elif event.source() is self:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._set_idle_style()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        if event.mimeData().hasUrls():
            for path in extract_dropped_mp3s(event):
                item = QListWidgetItem(Path(path).name)
                item.setData(Qt.ItemDataRole.UserRole, path)
                self.addItem(item)
            event.acceptProposedAction()
            self._set_idle_style()
            self.filesDropped.emit()
        elif event.source() is self:
            super().dropEvent(event)

    def paths(self) -> list[str]:
        return [self.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.count())]


class SplitDropZone(QLabel):
    filesDropped = pyqtSignal(list)

    IDLE_TEXT = "DRAG MP3 FILE(S) HERE TO SPLIT"
    HOVER_TEXT = "RELEASE TO SPLIT"

    def __init__(self):
        super().__init__(self.IDLE_TEXT)
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(90)
        self.setWordWrap(True)
        self._set_idle_style()

    def _set_idle_style(self):
        self.setStyleSheet(
            f"border: 2px dashed {BORDER}; border-radius: 12px; color: {MUTED}; "
            f"background-color: {CONSOLE}; letter-spacing: 1px;"
        )

    def _set_hover_style(self):
        self.setStyleSheet(
            f"border: 2px dashed {ORANGE}; border-radius: 12px; color: {ORANGE}; "
            f"background-color: {CONSOLE}; letter-spacing: 1px;"
        )

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.setText(self.HOVER_TEXT)
            self._set_hover_style()

    def dragLeaveEvent(self, event):
        self.setText(self.IDLE_TEXT)
        self._set_idle_style()

    def dropEvent(self, event: QDropEvent):
        paths = extract_dropped_mp3s(event)
        self.setText(self.IDLE_TEXT)
        self._set_idle_style()
        if paths:
            self.filesDropped.emit(paths)


class MergeWorker(QThread):
    log = pyqtSignal(str)
    finished_ok = pyqtSignal(str)
    finished_err = pyqtSignal(str)

    def __init__(self, files: list[str], output_dir: str):
        super().__init__()
        self.files = files
        self.output_dir = output_dir

    def run(self):
        out_dir = Path(self.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = unique_path(out_dir / f"{Path(self.files[0]).stem}_merged.mp3")

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
            for path in self.files:
                f.write(f"file '{escape_concat_path(path)}'\n")
            list_path = f.name

        self.log.emit(f"[{ts()}] Merging {len(self.files)} files -> {out_path.name}")

        cmd = [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", str(out_path)]
        result = self._run(cmd)

        if result is None:
            return
        if result.returncode != 0:
            self.log.emit(f"[{ts()}]   stream copy failed, retrying with re-encode...")
            cmd = [FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
                   "-c:a", "libmp3lame", "-b:a", "192k", str(out_path)]
            result = self._run(cmd)
            if result is None:
                return
            if result.returncode != 0:
                err = result.stderr.strip().splitlines()[-1] if result.stderr else "unknown error"
                self.log.emit(f"[{ts()}]   FAILED: {err}")
                self.finished_err.emit(err)
                return

        Path(list_path).unlink(missing_ok=True)
        self.log.emit(f"[{ts()}]   Done -> {out_path}")
        self.finished_ok.emit(str(out_path))

    def _run(self, cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            self.log.emit(f"[{ts()}]   FAILED: ffmpeg not found.")
            self.finished_err.emit("ffmpeg not found")
            return None


class SplitWorker(QThread):
    log = pyqtSignal(str)
    finished_all = pyqtSignal()

    def __init__(self, files: list[str], output_dir: str, segment_seconds: int):
        super().__init__()
        self.files = files
        self.output_dir = output_dir
        self.segment_seconds = segment_seconds

    def run(self):
        for path in self.files:
            self._split_one(path)
        self.finished_all.emit()

    def _split_one(self, path: str):
        base = Path(path).stem
        outdir = Path(self.output_dir) / f"{base}_split"
        outdir.mkdir(parents=True, exist_ok=True)

        self.log.emit(f"[{ts()}] Splitting: {Path(path).name}")

        cmd = [
            FFMPEG, "-y", "-i", path,
            "-f", "segment",
            "-segment_time", str(self.segment_seconds),
            "-c", "copy",
            "-reset_timestamps", "1",
            "-map", "0:a",
            str(outdir / "%d.mp3"),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            self.log.emit(f"[{ts()}]   FAILED: ffmpeg not found.")
            return

        if result.returncode != 0:
            err = result.stderr.strip().splitlines()[-1] if result.stderr else "unknown error"
            self.log.emit(f"[{ts()}]   FAILED: {err}")
            return

        count = len(list(outdir.glob("*.mp3")))
        self.log.emit(f"[{ts()}]   Done: {count} segments -> {outdir}")


def panel(title: str) -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setObjectName("panel")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 14)
    layout.setSpacing(10)
    label = QLabel(title)
    label.setObjectName("panelTitle")
    layout.addWidget(label)
    return frame, layout


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("Thomas", "Splice")
        self.output_dir = self.settings.value("output_dir", DEFAULT_OUTPUT_DIR)
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)

        self.setWindowTitle("Splice")
        self.resize(620, 760)
        self._build_ui()
        QTimer.singleShot(200, self._check_ffmpeg)

    # ---- UI construction ----

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)

        layout.addLayout(self._build_header())
        layout.addLayout(self._build_output_bar())
        layout.addWidget(self._build_merge_panel())
        layout.addWidget(self._build_split_panel())
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
        self.output_btn.setText(f"OUTPUT ▸  {self.output_dir}   (click to change)")

    def _build_merge_panel(self):
        frame, layout = panel("MERGE MODULE — combine multi-part audiobooks")

        hint = QLabel("Drop parts in order, or drag to reorder. Output is stream-copied (re-encodes only if formats mismatch).")
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

        return frame

    def _build_split_panel(self):
        frame, layout = panel("SPLIT MODULE — chop into fixed-length segments")

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

        return frame

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

    # ---- behavior ----

    def _write_log(self, text: str):
        self.log_box.appendPlainText(text)

    def _set_status(self, state: str, text: str):
        self.status_dot.set_state(state)
        self.status_label.setText(text)

    def _choose_output_dir(self):
        chosen = QFileDialog.getExistingDirectory(self, "Choose output folder", self.output_dir)
        if chosen:
            self.output_dir = chosen
            self.settings.setValue("output_dir", chosen)
            self._refresh_output_label()

    def _open_output_dir(self):
        Path(self.output_dir).mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(self.output_dir))

    def _check_ffmpeg(self):
        if not shutil.which(FFMPEG):
            QMessageBox.critical(self, "ffmpeg not found", FFMPEG_INSTALL_HINT)

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

    def _set_busy(self, busy: bool):
        self.merge_btn.setEnabled(not busy)
        self.split_zone.setEnabled(not busy)
        if busy:
            self._set_status("busy", "PROCESSING")
        else:
            self._set_status("standby", "STANDBY")

    def _run_merge(self):
        files = self.merge_list.paths()
        if len(files) < 2:
            QMessageBox.warning(self, "Need at least 2 files", "Add two or more mp3 parts to merge.")
            return
        self._set_busy(True)
        self.merge_worker = MergeWorker(files, self.output_dir)
        self.merge_worker.log.connect(self._write_log)
        self.merge_worker.finished_ok.connect(self._on_merge_ok)
        self.merge_worker.finished_err.connect(self._on_merge_err)
        self.merge_worker.start()

    def _on_merge_ok(self, out_path: str):
        self._set_status("done", "MERGE COMPLETE")
        self._set_busy(False)
        self.merge_list.clear()

    def _on_merge_err(self, err: str):
        self._set_status("error", "MERGE FAILED")
        self._set_busy(False)

    def _run_split(self, files: list[str]):
        segment_seconds = int(self.minutes_spin.value() * 60)
        self._set_busy(True)
        self.split_worker = SplitWorker(files, self.output_dir, segment_seconds)
        self.split_worker.log.connect(self._write_log)
        self.split_worker.finished_all.connect(self._on_split_done)
        self.split_worker.start()

    def _on_split_done(self):
        self._set_status("done", "SPLIT COMPLETE")
        self._set_busy(False)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")  # ensures the stylesheet renders consistently instead of
    # fighting the native macOS/Windows widget chrome (source of stray bezel artifacts)
    app.setStyleSheet(STYLESHEET)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
