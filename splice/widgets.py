"""Reusable Qt widgets: drop zones, status indicator, file picker, cover art thumbnail, level gauge."""

import re
from pathlib import Path

from PyQt6.QtCore import QPoint, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QFontMetrics,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QWidget,
)

from .styles import BORDER, CONSOLE, GREEN, INK, MUTED, ORANGE, PIXEL_FONT_FAMILY, RED, SURFACE


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
            f"QListWidget::item:selected {{ background-color: {ORANGE}; color: #FFFFFF; }}"
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


class PixelLabel(QLabel):
    """A QLabel set in the bundled Silkscreen pixel font — genuine hand-
    drawn pixel-grid glyphs, not a regular font faked into looking blocky.
    Reserved for chrome/branding; actual data (track titles, artist names,
    ...) stays in the app's normal Space Mono so it's legible. Silkscreen
    is registered with Qt via styles.load_bundled_fonts() at startup, so
    this just names it directly — no manual pixel-art rendering trick
    needed once the real font is doing the work."""

    def __init__(self, text: str = "", point_size: int = 13, color: str = INK,
                 bold: bool = True, letter_spacing: int = 1):
        super().__init__(text)
        # letter-spacing has no QSS equivalent, so it has to come from a
        # QFont via setFont() — but font-family/size/weight from setFont()
        # get silently overridden by the app-wide stylesheet's QWidget rule
        # (Qt's QSS cascade beats setFont() for any property it also sets),
        # so those three are repeated in this label's own stylesheet, which
        # (being more specific) wins over the app-wide one.
        font = QFont(PIXEL_FONT_FAMILY)
        font.setPointSize(point_size)
        font.setBold(bold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, letter_spacing)
        self.setFont(font)
        self.setStyleSheet(
            f"color: {color}; background: transparent; "
            f"font-family: '{PIXEL_FONT_FAMILY}'; font-size: {point_size}pt; "
            f"font-weight: {'bold' if bold else 'normal'};"
        )


class ChamferedFrame(QFrame):
    """A panel with one corner cut at 45°, matching Direction E's stat-card
    shape language — QSS border-radius can't do an asymmetric single-corner
    cut, so this paints its own background+border via QPainterPath, the
    same custom-paint pattern LevelGauge uses above."""

    def __init__(self, chamfer: int = 14, corner: str = "bottom-left",
                 background: str = SURFACE, border: str = BORDER):
        super().__init__()
        self._chamfer = chamfer
        self._corner = corner
        self._background = QColor(background)
        self._border = QColor(border)

    def _path(self) -> QPainterPath:
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        c = self._chamfer
        path = QPainterPath()
        if self._corner == "bottom-left":
            path.moveTo(r.left(), r.top())
            path.lineTo(r.right(), r.top())
            path.lineTo(r.right(), r.bottom())
            path.lineTo(r.left() + c, r.bottom())
            path.lineTo(r.left(), r.bottom() - c)
            path.closeSubpath()
        else:  # "top-left"
            path.moveTo(r.left(), r.top() + c)
            path.lineTo(r.left() + c, r.top())
            path.lineTo(r.right(), r.top())
            path.lineTo(r.right(), r.bottom())
            path.lineTo(r.left(), r.bottom())
            path.closeSubpath()
        return path

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(self._border, 1))
        painter.setBrush(self._background)
        painter.drawPath(self._path())


def render_chamfered_pixmap(
    image_bytes: bytes | None, size: int, chamfer: int,
    border_color: str = BORDER, background: str = CONSOLE, muted: str = MUTED,
) -> QPixmap:
    """A size x size square with one corner cut at 45° (matching
    ChamferedFrame's stat-card shape), filled with image_bytes cropped to
    cover, or a recessed "NO ARTWORK" placeholder if there's no image yet.
    Used at three scales for playlist artwork: a small Playlist Deck row
    icon, a larger PlaylistView header tile, and — sized for later, not
    built yet — a now-playing bar's mini art, so all three read as the same
    object rather than three separately-invented treatments."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    r = QRectF(0, 0, size, size).adjusted(0.5, 0.5, -0.5, -0.5)
    path = QPainterPath()
    path.moveTo(r.left(), r.top() + chamfer)
    path.lineTo(r.left() + chamfer, r.top())
    path.lineTo(r.right(), r.top())
    path.lineTo(r.right(), r.bottom())
    path.lineTo(r.left(), r.bottom())
    path.closeSubpath()

    image = None
    if image_bytes:
        candidate = QPixmap()
        if candidate.loadFromData(image_bytes):
            image = candidate

    painter.setClipPath(path)
    if image is not None:
        scaled = image.scaled(
            size, size, Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation
        )
        painter.drawPixmap((size - scaled.width()) // 2, (size - scaled.height()) // 2, scaled)
    else:
        painter.fillPath(path, QColor(background))
    painter.setClipping(False)

    painter.setPen(QPen(QColor(border_color), 1))
    painter.drawPath(path)

    if image is None and size >= 60:
        painter.setPen(QColor(muted))
        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)
        painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, "NO\nARTWORK")

    painter.end()
    return pixmap


class ChamferedArtTile(QLabel):
    """A render_chamfered_pixmap(), kept in a QLabel so it can sit in a
    layout like any other widget. clickable=True (the PlaylistView header
    tile) shows a pointing-hand cursor and an orange hover border, and emits
    clicked() instead of doing anything with the click itself — the caller
    owns what "click to change artwork" actually does (open a file dialog).
    The Playlist Deck's small row icon isn't a ChamferedArtTile — that one
    goes through QListWidgetItem.setIcon() instead, since deck rows are
    list items, not widgets."""

    clicked = pyqtSignal()

    def __init__(self, size: int = 96, chamfer: int = 16, clickable: bool = False):
        super().__init__()
        self._size = size
        self._chamfer = chamfer
        self._clickable = clickable
        self._image_bytes = None
        self._hovering = False
        self.setFixedSize(size, size)
        if clickable:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip("Click to set playlist artwork")
        self._render()

    def set_art(self, image_bytes: bytes | None):
        self._image_bytes = image_bytes
        self._render()

    def clear_art(self):
        self.set_art(None)

    def _render(self):
        border = ORANGE if (self._clickable and self._hovering) else BORDER
        self.setPixmap(render_chamfered_pixmap(self._image_bytes, self._size, self._chamfer, border_color=border))

    def enterEvent(self, event):
        if self._clickable:
            self._hovering = True
            self._render()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if self._clickable:
            self._hovering = False
            self._render()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        if self._clickable and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            return
        super().mousePressEvent(event)


def _resized_font(base_font: QFont, delta: int, minimum: int = 8) -> QFont:
    """Copy of base_font resized by delta, robust to whether its size was
    set in points or pixels. The app stylesheet sets font-size in px, which
    leaves QFont.pointSize() at -1 — calling setPointSize() on that (e.g.
    -1 + 1 = 0) trips Qt's "Point size <= 0" warning on every paint."""
    font = QFont(base_font)
    if base_font.pixelSize() > 0:
        font.setPixelSize(max(minimum, base_font.pixelSize() + delta))
    else:
        font.setPointSize(max(minimum, base_font.pointSize() + delta))
    return font


class TrackRowDelegate(QStyledItemDelegate):
    """Paints each row as a card — title bold, artist muted underneath, an
    optional badge pill on the right — instead of Qt's default single-line
    list item. A delegate, not setItemWidget(): with a library of thousands
    of tracks, setItemWidget() would materialize that many real child
    widgets at once, where a delegate only ever paints the rows actually
    visible in the viewport, same as the QTableWidget this replaced.

    Expects Qt.ItemDataRole.UserRole on each item to hold a dict with
    "title", "artist", and an optional "badge" (a short string, or None to
    omit the pill entirely — e.g. a track in no playlist shows no badge
    rather than a "0")."""

    ROW_HEIGHT = 46
    PADDING_X = 14

    @staticmethod
    def badge_rect(row_rect: QRect, base_font: QFont, badge: str) -> QRect:
        """Rect of the badge pill within a row — shared by paint() and
        TrackListWidget's click hit-testing so the two can never disagree
        about where the pill actually is."""
        badge_font = _resized_font(base_font, -1)
        badge_width = QFontMetrics(badge_font).horizontalAdvance(badge) + 16
        return QRect(
            row_rect.right() - TrackRowDelegate.PADDING_X - badge_width,
            row_rect.center().y() - 10, badge_width, 20,
        )

    def paint(self, painter: QPainter, option, index):
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        data = index.data(Qt.ItemDataRole.UserRole) or {}
        title = data.get("title") or ""
        artist = data.get("artist") or ""
        badge = data.get("badge")

        rect = option.rect
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)

        if selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(ORANGE))
            painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 3, 3)
        elif hovered:
            tint = QColor(ORANGE)
            tint.setAlpha(24)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(tint)
            painter.drawRoundedRect(rect.adjusted(2, 2, -2, -2), 3, 3)

        text_color = QColor("#FFFFFF") if selected else QColor(INK)
        muted_color = QColor("#FFFFFF") if selected else QColor(MUTED)

        badge_width = 0
        if badge:
            badge_font = _resized_font(option.font, -1)
            painter.setFont(badge_font)
            text_w = painter.fontMetrics().horizontalAdvance(badge)
            badge_width = text_w + 16
            badge_rect = QRect(
                rect.right() - self.PADDING_X - badge_width, rect.center().y() - 10, badge_width, 20
            )
            pen_color = QColor("#FFFFFF") if selected else QColor(ORANGE)
            painter.setPen(QPen(pen_color, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(badge_rect, 9, 9)
            painter.setPen(pen_color)
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge)

        text_left = rect.left() + self.PADDING_X
        text_right = rect.right() - self.PADDING_X - (badge_width + 8 if badge else 0)
        title_rect = QRect(text_left, rect.top() + 6, max(0, text_right - text_left), 20)
        artist_rect = QRect(text_left, rect.top() + 24, max(0, text_right - text_left), 18)

        title_font = _resized_font(option.font, 1)
        title_font.setBold(True)
        painter.setFont(title_font)
        painter.setPen(text_color)
        elided_title = painter.fontMetrics().elidedText(title, Qt.TextElideMode.ElideRight, title_rect.width())
        painter.drawText(title_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_title)

        artist_font = QFont(option.font)
        painter.setFont(artist_font)
        painter.setPen(muted_color)
        elided_artist = painter.fontMetrics().elidedText(artist, Qt.TextElideMode.ElideRight, artist_rect.width())
        painter.drawText(artist_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, elided_artist)

        painter.restore()

    def sizeHint(self, option, index) -> QSize:
        size = super().sizeHint(option, index)
        return QSize(size.width(), self.ROW_HEIGHT)


class TrackListWidget(QListWidget):
    """QListWidget paired with TrackRowDelegate for Songs/Audiobooks. A plain
    QListWidget can't distinguish "clicked the row" from "clicked the badge
    pill on the row" — this override checks whether a left-click landed
    inside the pill's rect (via TrackRowDelegate.badge_rect, so it's always
    exactly the area actually painted) and emits badgeClicked instead of
    letting the click fall through to normal row selection."""

    badgeClicked = pyqtSignal(int, QPoint)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.pos())
            if item is not None:
                data = item.data(Qt.ItemDataRole.UserRole) or {}
                badge = data.get("badge")
                if badge:
                    rect = TrackRowDelegate.badge_rect(self.visualItemRect(item), self.font(), badge)
                    if rect.contains(event.pos()):
                        self.badgeClicked.emit(data["id"], event.globalPosition().toPoint())
                        return
        super().mousePressEvent(event)
