"""Reusable Qt widgets: drop zones, status indicator, file picker, cover art thumbnail, level gauge."""

import re
from pathlib import Path

from PyQt6.QtCore import QBuffer, QIODevice, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
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
    QAbstractButton,
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
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


def render_mosaic_bytes(cover_art_list: list[bytes]) -> bytes | None:
    """Composites up to 4 covers into one square PNG — a playlist's
    auto-fallback artwork when no explicit artwork has been set, the same
    idea Spotify/Apple Music use for auto-generated playlist covers.
    Returns raw PNG bytes so it slots into the exact same `image_bytes`
    parameter every artwork call site already passes to
    render_chamfered_pixmap — nothing about the rendering pipeline itself
    needs to know a mosaic was involved. A single distinct cover is
    returned as-is (a "mosaic" of one image is pointless); with 2 or 3,
    covers repeat to fill all four quadrants rather than leaving dead
    corners, matching how the major players handle a short playlist too."""
    if not cover_art_list:
        return None
    if len(cover_art_list) == 1:
        return cover_art_list[0]

    size = 240
    half = size // 2
    pixmap = QPixmap(size, size)
    pixmap.fill(QColor(CONSOLE))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)

    quadrants = [QRectF(0, 0, half, half), QRectF(half, 0, half, half),
                 QRectF(0, half, half, half), QRectF(half, half, half, half)]
    for i, rect in enumerate(quadrants):
        cover = QPixmap()
        if not cover.loadFromData(cover_art_list[i % len(cover_art_list)]):
            continue
        scaled = cover.scaled(
            int(rect.width()), int(rect.height()),
            Qt.AspectRatioMode.KeepAspectRatioByExpanding, Qt.TransformationMode.SmoothTransformation,
        )
        x = rect.left() + (rect.width() - scaled.width()) / 2
        y = rect.top() + (rect.height() - scaled.height()) / 2
        painter.setClipRect(rect)
        painter.drawPixmap(int(x), int(y), scaled)
        painter.setClipping(False)
    painter.end()

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    return bytes(buffer.data())


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


class TileInfoButton(QAbstractButton):
    """Small circular info affordance overlaid on a standalone-book tile's
    art (series tiles don't get one — they navigate into the series, not
    open Track Info for a single track). Draws the same vector dot+stem-free
    plus glyph TrackRowDelegate paints for its per-row info icon, so both
    routes to Track Info read as the same affordance — this one's a real
    child QWidget rather than a painted hit-zone since a shelf tile has no
    delegate to paint through."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(22, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(ORANGE) if self.underMouse() else QColor(28, 30, 34, 190))
        painter.drawEllipse(rect)
        cx, cy = rect.center().x(), rect.center().y()
        pen = QPen(QColor("white"), 1.6)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        half = 4.0
        painter.drawLine(QPointF(cx - half, cy), QPointF(cx + half, cy))
        painter.drawLine(QPointF(cx, cy - half), QPointF(cx, cy + half))


class BookShelfTile(QWidget):
    """One tile in the audiobook shelf grid — cover art + title + subtitle,
    the whole widget clickable (not just the art). Used for both series
    tiles (art = the series' own artwork, subtitle = book count) and
    standalone-book tiles (art = that book's own embedded art, subtitle =
    author). Reuses ChamferedArtTile/render_chamfered_pixmap unmodified —
    square, not the mockup's portrait 3:4 covers, a deliberate scope cut to
    avoid widening a renderer three other things already depend on."""

    clicked = pyqtSignal()
    infoClicked = pyqtSignal()

    def __init__(self, art_bytes: bytes | None, title: str, subtitle: str, size: int = 120, show_info: bool = False):
        super().__init__()
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        self.art = ChamferedArtTile(size=size, chamfer=16, clickable=False)
        self.art.set_art(art_bytes)
        layout.addWidget(self.art)

        if show_info:
            # A real child widget positioned over the art tile's corner —
            # Qt dispatches the click to this button, not up to the tile's
            # own mousePressEvent, so it can't also trigger playback.
            info_btn = TileInfoButton(self.art)
            info_btn.move(size - info_btn.width() - 6, 6)
            info_btn.raise_()
            info_btn.clicked.connect(lambda checked=False: self.infoClicked.emit())

        title_label = QLabel(title)
        title_label.setWordWrap(True)
        title_label.setStyleSheet("font-weight: bold;")
        layout.addWidget(title_label)

        subtitle_label = QLabel(subtitle)
        subtitle_label.setObjectName("muted")
        subtitle_label.setWordWrap(True)
        layout.addWidget(subtitle_label)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            return
        super().mousePressEvent(event)


class AudiobookShelfWidget(QWidget):
    """Top-level Audiobooks browse view — a scrollable grid of
    BookShelfTiles, one per series plus one per standalone (non-series)
    audiobook. Owns no DB/file access itself (same dumb-view convention as
    every other widget in this file) — populate() is handed already-fetched
    rows and already-resolved art bytes; the caller (LibraryView) does the
    actual DB queries and tagging.read_tags()/find_series_artwork() calls,
    same division of labor PlaylistView already uses for its own art tile."""

    seriesActivated = pyqtSignal(str)  # series (Album tag) name
    playRequested = pyqtSignal(list, int)  # standalone tile clicked — same contract LibraryView.playRequested uses
    infoRequested = pyqtSignal(int)  # standalone tile's info icon clicked — track_id

    COLUMNS = 4

    def __init__(self):
        super().__init__()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        outer.addWidget(self._scroll)

        self._grid_host = QWidget()
        self._grid = QGridLayout(self._grid_host)
        self._grid.setSpacing(18)
        self._scroll.setWidget(self._grid_host)

    def populate(self, series_entries: list[tuple[str, str, "bytes | None"]], standalone_entries: list[dict]):
        """series_entries: (name, subtitle, art_bytes) tuples — mirrors
        PlaylistDeck.set_playlists's (id, name, art_bytes) shape.
        standalone_entries: dicts already shaped like a play_queue() entry
        (id/title/artist/path/kind/loudness_lufs/loudness_true_peak) plus an
        extra 'art' key for the tile's own thumbnail, so a click can go
        straight to playRequested with no further lookup needed."""
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget:
                # setParent(None) detaches it from the widget tree
                # immediately (synchronously) — deleteLater() alone only
                # schedules the eventual C++-level destruction, which can
                # leave a stale tile briefly still paintable if populate()
                # runs again before the event loop gets a chance to process
                # that deferred deletion (e.g. two refreshes back to back
                # with no intervening idle tick).
                widget.setParent(None)
                widget.deleteLater()

        row = col = 0

        def place(tile: BookShelfTile):
            nonlocal row, col
            self._grid.addWidget(tile, row, col)
            col += 1
            if col >= self.COLUMNS:
                col = 0
                row += 1

        for name, subtitle, art_bytes in series_entries:
            tile = BookShelfTile(art_bytes, name, subtitle)
            tile.clicked.connect(lambda checked=False, n=name: self.seriesActivated.emit(n))
            place(tile)

        for entry in standalone_entries:
            tile = BookShelfTile(
                entry.get("art"), entry["title"], entry.get("artist") or "Standalone", show_info=True
            )
            tile.clicked.connect(lambda checked=False, e=entry: self._play_standalone(e))
            tile.infoClicked.connect(lambda tid=entry["id"]: self.infoRequested.emit(tid))
            place(tile)

        self._grid.setRowStretch(row + 1, 1)  # keep tiles pinned to the top when they don't fill the scroll area

    def _play_standalone(self, entry: dict):
        queue_dict = {k: v for k, v in entry.items() if k != "art"}
        self.playRequested.emit([queue_dict], 0)


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
    INFO_ICON_SIZE = 20
    THUMB_SIZE = 34

    @staticmethod
    def thumbnail_rect(row_rect: QRect) -> QRect:
        """Rect of the optional left-side thumbnail — only actually drawn
        when the item has a DecorationRole icon set (Songs/Playlist rows
        never set one, so their layout is unaffected; the audiobook series
        book list does). Shared by paint() and click hit-testing, same
        rationale as badge_rect/info_icon_rect."""
        size = TrackRowDelegate.THUMB_SIZE
        return QRect(
            row_rect.left() + TrackRowDelegate.PADDING_X,
            row_rect.center().y() - size // 2, size, size,
        )

    @staticmethod
    def _badge_width(base_font: QFont, badge: str | None) -> int:
        if not badge:
            return 0
        badge_font = _resized_font(base_font, -1)
        return QFontMetrics(badge_font).horizontalAdvance(badge) + 16

    @staticmethod
    def badge_rect(row_rect: QRect, base_font: QFont, badge: str) -> QRect:
        """Rect of the badge pill within a row — shared by paint() and
        TrackListWidget's click hit-testing so the two can never disagree
        about where the pill actually is."""
        badge_width = TrackRowDelegate._badge_width(base_font, badge)
        return QRect(
            row_rect.right() - TrackRowDelegate.PADDING_X - badge_width,
            row_rect.center().y() - 10, badge_width, 20,
        )

    @staticmethod
    def info_icon_rect(row_rect: QRect, base_font: QFont, badge: str | None) -> QRect:
        """Rect of the small circular INFO icon — sits just left of the
        badge pill (or where the badge would be, if there is none). Shared
        by paint() and TrackListWidget's click hit-testing, same rationale
        as badge_rect."""
        badge_width = TrackRowDelegate._badge_width(base_font, badge)
        reserved = badge_width + 8 if badge else 0  # same 8px gap paint() uses before the badge
        right_edge = row_rect.right() - TrackRowDelegate.PADDING_X - reserved
        size = TrackRowDelegate.INFO_ICON_SIZE
        return QRect(right_edge - size - 6, row_rect.center().y() - size // 2, size, size)

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
        # Read at paint time rather than baking a "now_playing" flag into
        # each item's dict at populate time — the latter would go stale the
        # instant a different track starts playing, without a full
        # repopulate. self.parent() is the owning TrackListWidget for free
        # (passed as this delegate's QObject parent at construction).
        playing = bool(data.get("id")) and data.get("id") == getattr(self.parent(), "_playing_id", None)

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

        if playing and not selected:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(ORANGE))
            painter.drawRoundedRect(QRect(rect.left() + 2, rect.top() + 6, 3, rect.height() - 12), 1, 1)

        text_color = QColor("#FFFFFF") if selected else QColor(ORANGE) if playing else QColor(INK)
        muted_color = QColor("#FFFFFF") if selected else QColor(MUTED)

        badge_width = self._badge_width(option.font, badge)
        if badge:
            badge_rect = QRect(
                rect.right() - self.PADDING_X - badge_width, rect.center().y() - 10, badge_width, 20
            )
            pen_color = QColor("#FFFFFF") if selected else QColor(ORANGE)
            badge_font = _resized_font(option.font, -1)
            painter.setFont(badge_font)
            painter.setPen(QPen(pen_color, 1))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(badge_rect, 9, 9)
            painter.setPen(pen_color)
            painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, badge)

        info_rect = self.info_icon_rect(rect, option.font, badge)
        icon_color = QColor("#FFFFFF") if selected else QColor(ORANGE)
        painter.setPen(QPen(icon_color, 1))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawEllipse(info_rect)
        # Drawn as vector lines rather than a font-rendered character — a
        # font-rendered "i" read as a "1" at this size regardless of font,
        # same "don't trust a small glyph, paint it yourself" fix
        # TransportButton's media icons used.
        cx, cy = info_rect.center().x(), info_rect.center().y()
        plus_pen = QPen(icon_color, 1.6)
        plus_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(plus_pen)
        half = 4.0
        painter.drawLine(QPointF(cx - half, cy), QPointF(cx + half, cy))
        painter.drawLine(QPointF(cx, cy - half), QPointF(cx, cy + half))

        icon = index.data(Qt.ItemDataRole.DecorationRole)
        text_left = rect.left() + self.PADDING_X
        if icon is not None and not icon.isNull():
            thumb_rect = self.thumbnail_rect(rect)
            icon.paint(painter, thumb_rect)
            text_left = thumb_rect.right() + 10
        text_right = info_rect.left() - 8
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
    infoClicked = pyqtSignal(int)
    thumbnailClicked = pyqtSignal(int)

    def __init__(self):
        super().__init__()
        self._playing_id: int | None = None

    def set_playing_track(self, track_id: int | None):
        """track_id of the row to paint with the now-playing accent, or None
        to clear it. Just a repaint — TrackRowDelegate reads this back via
        self.parent() at paint time, no per-item data to update."""
        self._playing_id = track_id
        self.viewport().update()

    def _badge_hit(self, pos) -> bool:
        item = self.itemAt(pos)
        if item is None:
            return False
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        badge = data.get("badge")
        if not badge:
            return False
        rect = TrackRowDelegate.badge_rect(self.visualItemRect(item), self.font(), badge)
        return rect.contains(pos)

    def _info_hit(self, pos) -> bool:
        item = self.itemAt(pos)
        if item is None:
            return False
        data = item.data(Qt.ItemDataRole.UserRole) or {}
        if not data.get("id"):
            return False
        rect = TrackRowDelegate.info_icon_rect(self.visualItemRect(item), self.font(), data.get("badge"))
        return rect.contains(pos)

    def _thumb_hit(self, pos) -> bool:
        item = self.itemAt(pos)
        if item is None or item.icon().isNull():
            return False
        rect = TrackRowDelegate.thumbnail_rect(self.visualItemRect(item))
        return rect.contains(pos)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            item = self.itemAt(event.pos())
            if item is not None:
                data = item.data(Qt.ItemDataRole.UserRole) or {}
                if data.get("id") is not None:
                    if not item.icon().isNull():
                        thumb_rect = TrackRowDelegate.thumbnail_rect(self.visualItemRect(item))
                        if thumb_rect.contains(event.pos()):
                            self.thumbnailClicked.emit(data["id"])
                            return
                    info_rect = TrackRowDelegate.info_icon_rect(
                        self.visualItemRect(item), self.font(), data.get("badge")
                    )
                    if info_rect.contains(event.pos()):
                        self.infoClicked.emit(data["id"])
                        return
                badge = data.get("badge")
                if badge:
                    rect = TrackRowDelegate.badge_rect(self.visualItemRect(item), self.font(), badge)
                    if rect.contains(event.pos()):
                        self.badgeClicked.emit(data["id"], event.globalPosition().toPoint())
                        return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        # Without this, double-clicking exactly on the badge pill, the info
        # icon, or the thumbnail would both trigger their single-click
        # handler above and fire itemDoubleClicked (play) at the same
        # instant, since Qt delivers both a press and a double-click event
        # for one gesture.
        if event.button() == Qt.MouseButton.LeftButton and (
            self._badge_hit(event.pos()) or self._info_hit(event.pos()) or self._thumb_hit(event.pos())
        ):
            return
        super().mouseDoubleClickEvent(event)


def _format_ms(ms: int) -> str:
    """M:SS for a player position/duration in milliseconds. Distinct from
    library_view.py's _format_duration (that one formats DB seconds, and
    library_view.py imports from this file, not the reverse)."""
    total_seconds = max(0, int(ms // 1000))
    return f"{total_seconds // 60}:{total_seconds % 60:02d}"


class ConsoleStrip(QFrame):
    """Thin clickable status strip — the console's collapsed state. QFrame
    has no built-in click signal, so mousePressEvent is overridden to emit
    one, same pattern ChamferedArtTile uses for its own click affordance."""

    clicked = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("consoleStrip")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 5, 10, 5)
        row.setSpacing(8)
        self.status_dot = StatusDot()
        self.status_label = QLabel("STANDBY")
        self.status_label.setObjectName("muted")
        # Separate from status_dot/status_label on purpose — those are the
        # single shared indicator Merge/Split/Tags treat as mutually
        # exclusive ("one busy thing at a time"). Background loudness
        # analysis can run for a long time alongside any of those, so it
        # gets its own lane here instead of fighting over the shared one —
        # same reasoning that gave YouTube downloads their own progress
        # widget rather than sharing this strip too.
        self.task_label = QLabel("")
        self.task_label.setObjectName("muted")
        self._hint = QLabel("CLICK TO EXPAND ▴")
        self._hint.setObjectName("muted")
        row.addWidget(self.status_dot)
        row.addWidget(self.status_label)
        row.addWidget(self.task_label)
        row.addStretch(1)
        row.addWidget(self._hint)

    def set_status(self, state: str, text: str):
        self.status_dot.set_state(state)
        self.status_label.setText(text)

    def set_task_text(self, text: str):
        self.task_label.setText(text)

    def set_expanded(self, expanded: bool):
        self._hint.setText("CLICK TO COLLAPSE ▾" if expanded else "CLICK TO EXPAND ▴")

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            return
        super().mousePressEvent(event)


class TransportButton(QAbstractButton):
    """A playback control (play/pause/prev/next/shuffle) drawn as a flat
    vector shape via QPainter, instead of a Unicode glyph — media symbols
    (▶⏸⏮⏭⇄) render inconsistently (mismatched weight, sometimes an ugly
    fallback glyph) depending on what fonts happen to be installed. Same
    custom-paint idiom RailButton/LevelGauge/StatusDot already use elsewhere
    in this file, so this always matches Direction E exactly regardless of
    the machine."""

    def __init__(self, icon: str, checkable: bool = False, size: tuple[int, int] = (30, 26)):
        super().__init__()
        self._icon = icon
        self._hovering = False
        self.setCheckable(checkable)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(*size)

    def set_icon(self, icon: str):
        self._icon = icon
        self.update()

    def enterEvent(self, event):
        self._hovering = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hovering = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()

        if not self.isEnabled():
            bg, border, icon_color = QColor(SURFACE), QColor(BORDER), QColor(BORDER)
        elif self.isChecked():
            bg = QColor(ORANGE)
            bg.setAlpha(28)
            border = QColor(ORANGE)
            icon_color = QColor(ORANGE)
        elif self._hovering:
            bg, border, icon_color = QColor(SURFACE), QColor(ORANGE), QColor(ORANGE)
        else:
            bg, border, icon_color = QColor(SURFACE), QColor(BORDER), QColor(INK)

        painter.setPen(QPen(border, 1))
        painter.setBrush(bg)
        painter.drawRoundedRect(rect.adjusted(0, 0, -1, -1), 2, 2)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(icon_color)
        self._draw_glyph(painter, rect, icon_color)
        painter.end()

    def _draw_glyph(self, painter: QPainter, rect: QRect, color: QColor):
        margin_x = rect.width() * 0.28
        margin_y = rect.height() * 0.2
        bx, by = rect.left() + margin_x, rect.top() + margin_y
        bw, bh = rect.width() - 2 * margin_x, rect.height() - 2 * margin_y

        if self._icon == "play":
            self._triangle(painter, bx, by, bw, bh, pointing_right=True)
        elif self._icon == "pause":
            bar_w = bw * 0.34
            gap = bw - 2 * bar_w
            painter.drawRect(QRectF(bx, by, bar_w, bh))
            painter.drawRect(QRectF(bx + bar_w + gap, by, bar_w, bh))
        elif self._icon == "next":
            tri_w = bw * 0.64
            bar_w = bw - tri_w - bw * 0.1
            self._triangle(painter, bx, by, tri_w, bh, pointing_right=True)
            painter.drawRect(QRectF(bx + bw - bar_w, by, bar_w, bh))
        elif self._icon == "prev":
            tri_w = bw * 0.64
            bar_w = bw - tri_w - bw * 0.1
            painter.drawRect(QRectF(bx, by, bar_w, bh))
            self._triangle(painter, bx + bar_w + bw * 0.1, by, tri_w, bh, pointing_right=False)
        elif self._icon == "shuffle":
            self._shuffle_glyph(painter, bx, by, bw, bh, color)

    @staticmethod
    def _triangle(painter: QPainter, x: float, y: float, w: float, h: float, pointing_right: bool):
        path = QPainterPath()
        if pointing_right:
            path.moveTo(x, y)
            path.lineTo(x, y + h)
            path.lineTo(x + w, y + h / 2)
        else:
            path.moveTo(x + w, y)
            path.lineTo(x + w, y + h)
            path.lineTo(x, y + h / 2)
        path.closeSubpath()
        painter.drawPath(path)

    @staticmethod
    def _shuffle_glyph(painter: QPainter, x: float, y: float, w: float, h: float, color: QColor):
        # Straight crossing lines read as a plain X regardless of how their
        # lengths are staggered — a curve is what actually sells "two paths
        # swapping places" instead of "multiplication sign". Two flowing
        # S-curves, each ending in a right-pointing arrowhead.
        pen = QPen(color, 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        tip_top = (x + w * 0.88, y + h * 0.14)
        tip_bottom = (x + w * 0.88, y + h * 0.86)

        upper = QPainterPath()
        upper.moveTo(x, y + h * 0.78)
        upper.cubicTo(x + w * 0.4, y + h * 0.78, x + w * 0.4, y + h * 0.22, tip_top[0], tip_top[1])
        painter.drawPath(upper)

        lower = QPainterPath()
        lower.moveTo(x, y + h * 0.22)
        lower.cubicTo(x + w * 0.4, y + h * 0.22, x + w * 0.4, y + h * 0.78, tip_bottom[0], tip_bottom[1])
        painter.drawPath(lower)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        for ex, ey in (tip_top, tip_bottom):
            arrow = QPainterPath()
            arrow.moveTo(ex - 6, ey - 4.5)
            arrow.lineTo(ex + 3, ey)
            arrow.lineTo(ex - 6, ey + 4.5)
            arrow.closeSubpath()
            painter.drawPath(arrow)


class NowPlayingBar(QWidget):
    """Persistent transport bar — a dumb view like every other custom widget
    in this file: no DB/player access of its own, signals out for user
    actions, setter methods in for state updates. Plays both music and
    audiobooks; lives outside MainWindow's page-switching content_stack so
    playback survives tab navigation."""

    playPauseClicked = pyqtSignal()
    nextClicked = pyqtSignal()
    prevClicked = pyqtSignal()
    shuffleToggled = pyqtSignal(bool)
    seekRequested = pyqtSignal(int)    # ms, emitted only on slider release
    volumeChanged = pyqtSignal(float)  # 0.0-1.0

    def __init__(self):
        super().__init__()
        self.setObjectName("nowPlayingBar")
        self._seeking = False

        row = QHBoxLayout(self)
        row.setContentsMargins(10, 6, 10, 6)
        row.setSpacing(12)

        self.art_tile = ChamferedArtTile(size=40, chamfer=8, clickable=False)
        row.addWidget(self.art_tile)

        meta_col = QVBoxLayout()
        meta_col.setSpacing(1)
        self.title_label = QLabel("NOTHING PLAYING")
        self.title_label.setObjectName("nowPlayingTitle")
        self.artist_label = QLabel("")
        self.artist_label.setObjectName("muted")
        meta_col.addWidget(self.title_label)
        meta_col.addWidget(self.artist_label)
        meta_widget = QWidget()
        meta_widget.setLayout(meta_col)
        meta_widget.setFixedWidth(150)
        row.addWidget(meta_widget)

        # Custom-painted (TransportButton), not Unicode glyphs — media
        # symbols (▶⏸⏮⏭⇄) render inconsistently across fonts, sometimes as
        # an outright ugly fallback glyph. Flat vector shapes always match
        # Direction E exactly, same reasoning as RailButton/LevelGauge.
        self.shuffle_btn = TransportButton("shuffle", checkable=True)
        self.shuffle_btn.setToolTip("Shuffle")
        self.shuffle_btn.toggled.connect(self.shuffleToggled.emit)
        row.addWidget(self.shuffle_btn)

        self.prev_btn = TransportButton("prev")
        self.prev_btn.setToolTip("Previous")
        self.prev_btn.clicked.connect(self.prevClicked.emit)
        row.addWidget(self.prev_btn)

        self.play_pause_btn = TransportButton("play", size=(36, 26))
        self.play_pause_btn.setToolTip("Play/Pause")
        self.play_pause_btn.clicked.connect(self.playPauseClicked.emit)
        row.addWidget(self.play_pause_btn)

        self.next_btn = TransportButton("next")
        self.next_btn.setToolTip("Next")
        self.next_btn.clicked.connect(self.nextClicked.emit)
        row.addWidget(self.next_btn)

        self.position_label = QLabel("0:00")
        self.position_label.setObjectName("muted")
        row.addWidget(self.position_label)

        self.scrub_slider = QSlider(Qt.Orientation.Horizontal)
        self.scrub_slider.setRange(0, 0)
        self.scrub_slider.sliderPressed.connect(self._on_scrub_pressed)
        self.scrub_slider.sliderReleased.connect(self._on_scrub_released)
        self.scrub_slider.valueChanged.connect(self._on_scrub_value_changed)
        row.addWidget(self.scrub_slider, stretch=1)

        self.duration_label = QLabel("0:00")
        self.duration_label.setObjectName("muted")
        row.addWidget(self.duration_label)

        volume_label = QLabel("♪")
        volume_label.setObjectName("muted")
        row.addWidget(volume_label)

        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(100)
        self.volume_slider.setFixedWidth(80)
        self.volume_slider.valueChanged.connect(lambda v: self.volumeChanged.emit(v / 100.0))
        row.addWidget(self.volume_slider)

        self._set_controls_enabled(False)

    # ---- setters (state in) ----

    def set_idle(self):
        self.title_label.setText("NOTHING PLAYING")
        self.artist_label.setText("")
        self.art_tile.clear_art()
        self.position_label.setText("0:00")
        self.duration_label.setText("0:00")
        self.scrub_slider.setRange(0, 0)
        self._set_controls_enabled(False)

    def set_track_info(self, title: str, artist: str, art_bytes: bytes | None):
        self._set_controls_enabled(True)
        self.title_label.setText(title or "Untitled")
        self.artist_label.setText(artist or "")
        self.art_tile.set_art(art_bytes)

    def set_position(self, ms: int):
        if self._seeking:
            return
        self.position_label.setText(_format_ms(ms))
        self.scrub_slider.blockSignals(True)
        self.scrub_slider.setValue(ms)
        self.scrub_slider.blockSignals(False)

    def set_duration(self, ms: int):
        self.scrub_slider.setRange(0, max(0, ms))
        self.duration_label.setText(_format_ms(ms))

    def set_playing(self, playing: bool):
        self.play_pause_btn.set_icon("pause" if playing else "play")

    def set_shuffle(self, enabled: bool):
        self.shuffle_btn.blockSignals(True)
        self.shuffle_btn.setChecked(enabled)
        self.shuffle_btn.blockSignals(False)

    def set_shuffle_enabled(self, enabled: bool):
        """Disables the shuffle button while an audiobook queue is playing
        — book/chapter order shouldn't get scrambled. TrackPlayer already
        guarantees an audiobook queue is never actually shuffled regardless
        of this button's state; this is the visible half of that, not the
        correctness guarantee."""
        self.shuffle_btn.setEnabled(enabled)

    # ---- internals ----

    def _set_controls_enabled(self, enabled: bool):
        # Volume is deliberately excluded — it's an output-level control, not
        # a "now playing" control, and works fine with nothing loaded yet.
        for w in (self.shuffle_btn, self.prev_btn, self.play_pause_btn, self.next_btn, self.scrub_slider):
            w.setEnabled(enabled)

    def _on_scrub_pressed(self):
        self._seeking = True

    def _on_scrub_released(self):
        self._seeking = False
        self.seekRequested.emit(self.scrub_slider.value())

    def _on_scrub_value_changed(self, value: int):
        self.position_label.setText(_format_ms(value))
