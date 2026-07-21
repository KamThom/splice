"""Reusable Qt widgets: drop zones, status indicator, file picker, cover art thumbnail, level gauge."""

import re
from pathlib import Path

from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QDragEnterEvent, QDropEvent, QMouseEvent, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QWidget,
)

from .styles import BORDER, CONSOLE, GREEN, INK, MUTED, ORANGE, RED, TEAL


def extract_dropped_mp3s(event) -> list[str]:
    paths = []
    for url in event.mimeData().urls():
        path = url.toLocalFile()
        if path.lower().endswith(".mp3"):
            paths.append(path)
    return paths


def browse_for_mp3s(parent) -> list[str]:
    paths, _ = QFileDialog.getOpenFileNames(parent, "Choose mp3 files", str(Path.home()), "MP3 files (*.mp3)")
    return paths


def natural_sort_key(path: str):
    """Sorts 'Part 2' before 'Part 10' — filename order, not lexical order."""
    name = Path(path).name.lower()
    return [int(tok) if tok.isdigit() else tok for tok in re.split(r"(\d+)", name)]


class StatusDot(QLabel):
    def __init__(self):
        super().__init__()
        self.setFixedSize(12, 12)
        self.set_state("standby")

    def set_state(self, state: str):
        color = {"standby": MUTED, "busy": ORANGE, "done": GREEN, "error": RED}[state]
        self.setStyleSheet(f"background-color: {color}; border-radius: 6px;")


class LevelGauge(QWidget):
    """Retro segmented VU-meter-style progress indicator, in place of a plain QProgressBar.

    States: idle (dim, empty) / progress (0-100, orange fill) / indeterminate
    (scanning highlight, for phases with no real percentage) / done (green) /
    error (red) / cancelled (red) — done/error/cancelled stay lit until the next
    run resets the gauge, so "finished" is visually distinct from "in progress".
    """

    SEGMENTS = 24

    def __init__(self):
        super().__init__()
        self.setMinimumHeight(28)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._state = "idle"
        self._pct = 0
        self._text = ""
        self._sweep_pos = 0
        self._timer = QTimer(self)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._advance_sweep)

    def set_idle(self):
        self._timer.stop()
        self._state = "idle"
        self._pct = 0
        self._text = ""
        self.update()

    def set_progress(self, pct: int):
        if pct < 0:
            if self._state != "indeterminate":
                self._state = "indeterminate"
                self._sweep_pos = 0
                self._timer.start()
            self._text = "WORKING"
            self.update()
            return
        self._timer.stop()
        self._state = "progress"
        self._pct = pct
        self._text = f"{pct}%"
        self.update()

    def set_done(self, text: str = "COMPLETE"):
        self._timer.stop()
        self._state = "done"
        self._pct = 100
        self._text = text
        self.update()

    def set_error(self, text: str = "FAILED"):
        self._timer.stop()
        self._state = "error"
        self._text = text
        self.update()

    def set_cancelled(self, text: str = "CANCELLED"):
        self._timer.stop()
        self._state = "cancelled"
        self._text = text
        self.update()

    def _advance_sweep(self):
        self._sweep_pos += 1
        self.update()

    def _sweep_index(self) -> int:
        period = max(1, 2 * (self.SEGMENTS - 1))
        pos = self._sweep_pos % period
        return pos if pos < self.SEGMENTS else period - pos

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(CONSOLE))
        painter.drawRoundedRect(self.rect().adjusted(0, 0, -1, -1), 7, 7)

        margin = 4
        gap = 2
        n = self.SEGMENTS
        available_w = self.width() - margin * 2
        seg_w = (available_w - gap * (n - 1)) / n
        seg_h = self.height() - margin * 2

        lit_color = {
            "progress": QColor(ORANGE),
            "indeterminate": QColor(ORANGE),
            "done": QColor(GREEN),
            "error": QColor(RED),
            "cancelled": QColor(RED),
        }.get(self._state, QColor(ORANGE))

        dim_color = QColor(BORDER)
        dim_color.setAlpha(80)

        lit_count = 0
        if self._state in ("progress", "done"):
            lit_count = round(n * self._pct / 100)
        elif self._state in ("error", "cancelled"):
            lit_count = n

        sweep_index = self._sweep_index() if self._state == "indeterminate" else -1

        for i in range(n):
            x = margin + i * (seg_w + gap)
            rect = QRectF(x, margin, seg_w, seg_h)
            if self._state == "indeterminate":
                on = abs(i - sweep_index) <= 1
            else:
                on = i < lit_count
            painter.setBrush(lit_color if on else dim_color)
            painter.drawRoundedRect(rect, 2, 2)

        if self._text:
            painter.setPen(QColor(INK))
            font = painter.font()
            font.setBold(True)
            font.setPointSize(9)
            painter.setFont(font)
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)


class DropListWidget(QListWidget):
    """List of files to merge — drag in or click empty space to browse; internal drag reorders."""

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

    def _add_paths(self, paths: list[str]):
        for path in sorted(paths, key=natural_sort_key):
            item = QListWidgetItem(Path(path).name)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.addItem(item)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton and self.itemAt(event.pos()) is None:
            paths = browse_for_mp3s(self)
            if paths:
                self._add_paths(paths)
                self.filesDropped.emit()
            return
        super().mousePressEvent(event)

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
            self._add_paths(extract_dropped_mp3s(event))
            event.acceptProposedAction()
            self._set_idle_style()
            self.filesDropped.emit()
        elif event.source() is self:
            super().dropEvent(event)

    def paths(self) -> list[str]:
        return [self.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.count())]


class SplitDropZone(QLabel):
    filesDropped = pyqtSignal(list)

    IDLE_TEXT = "DRAG MP3 FILE(S) HERE, OR CLICK TO BROWSE"
    HOVER_TEXT = "RELEASE TO SPLIT"

    def __init__(self):
        super().__init__(self.IDLE_TEXT)
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumHeight(90)
        self.setWordWrap(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
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

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            paths = browse_for_mp3s(self)
            if paths:
                self.filesDropped.emit(paths)
            return
        super().mousePressEvent(event)

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


class FilePickerList(QListWidget):
    """Multi-select list of mp3 paths for batch metadata tagging — drag in or click empty space to browse."""

    def __init__(self):
        super().__init__()
        self.setObjectName("filePicker")
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setMinimumHeight(120)
        self.setAcceptDrops(True)

    def add_paths(self, paths: list[str]):
        existing = set(self.paths())
        new_paths = [p for p in paths if p not in existing]
        for path in sorted(new_paths, key=natural_sort_key):
            item = QListWidgetItem(Path(path).name)
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.addItem(item)

    def remove_selected(self):
        for item in self.selectedItems():
            self.takeItem(self.row(item))

    def paths(self) -> list[str]:
        return [self.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.count())]

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton and self.itemAt(event.pos()) is None:
            paths = browse_for_mp3s(self)
            if paths:
                self.add_paths(paths)
            return
        super().mousePressEvent(event)

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent):
        self.add_paths(extract_dropped_mp3s(event))
        event.acceptProposedAction()


class CoverArtThumbnail(QLabel):
    """Displays embedded cover art, or a placeholder when there is none."""

    PLACEHOLDER_TEXT = "NO\nARTWORK"

    def __init__(self):
        super().__init__(self.PLACEHOLDER_TEXT)
        self.setObjectName("coverArt")
        self.setFixedSize(96, 96)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pixmap = None

    def set_art(self, data: bytes | None):
        if not data:
            self.clear_art()
            return
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            self.clear_art()
            return
        self._pixmap = pixmap
        self.setText("")
        self.setPixmap(
            pixmap.scaled(
                self.width(),
                self.height(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def clear_art(self):
        self._pixmap = None
        self.setPixmap(QPixmap())
        self.setText(self.PLACEHOLDER_TEXT)
