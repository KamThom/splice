"""Left-hand navigation, split into two independent widgets: a slim
text-rail ToolRail (Library home + Merge/Split/YouTube/Tags) and a
full-height PlaylistDeck. Splitting them (rather than one long stacked
sidebar) is what lets Playlists get its own single-scrollbar column instead
of a capped, double-scrolling section fighting the tools list for space."""

from PyQt6.QtCore import QRect, QSize, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (
    QAbstractButton,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .styles import BG, BORDER, MUTED, ORANGE, PIXEL_FONT_FAMILY
from .widgets import PixelLabel

TOOLS = [("merge", "MERGE"), ("split", "SPLIT"), ("youtube", "YOUTUBE"), ("tags", "TAGS")]


class RailButton(QAbstractButton):
    """One rail item: a checkable button whose label renders as vertical,
    rotated text in the bundled Silkscreen pixel font. QSS can't rotate
    text, so this paints itself directly via QPainter — same custom-paint
    idiom as LevelGauge/ChamferedFrame in widgets.py. Subclasses
    QAbstractButton rather than QWidget specifically to get checkable/
    clicked/setChecked/isChecked for free, so ToolRail's select() logic
    barely changes."""

    def __init__(self, label: str):
        super().__init__()
        self._label = label
        self._hover = False
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)

    def sizeHint(self) -> QSize:
        return QSize(38, 90)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect()

        if self.isChecked():
            tint = QColor(ORANGE)
            tint.setAlpha(28)
            painter.fillRect(rect, tint)
            painter.setPen(QPen(QColor(ORANGE), 2))
            painter.drawLine(rect.left() + 1, rect.top(), rect.left() + 1, rect.bottom())
        elif self._hover:
            painter.fillRect(rect, QColor(BG))

        painter.setPen(QPen(QColor(BORDER), 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())

        color = ORANGE if self.isChecked() else MUTED
        font = QFont(PIXEL_FONT_FAMILY)
        font.setPointSize(11)
        font.setBold(True)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 2)
        painter.setFont(font)
        painter.setPen(QColor(color))

        painter.save()
        painter.translate(rect.center())
        painter.rotate(90)
        # After rotating 90°, the painter's local x-axis runs along the
        # rail's height and local y along its (negative) width — draw into
        # a rect matching that, centered on the origin.
        text_rect = QRect(-rect.height() // 2, -rect.width() // 2, rect.height(), rect.width())
        painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, self._label)
        painter.restore()


class ToolRail(QWidget):
    """Emits navigate(kind, None) on Library/Merge/Split/YouTube. The Tags
    button additionally emits tagsRequested — MainWindow uses that to reset
    Batch Apply to an empty state before showing it, so opening Tags cold
    never shows a previous session's leftover data. (Tags is still reachable
    generically via select("tags") too, for the contextual Fix Tags / Send
    Segments to Tags entry points, which set up their own state and don't
    want the reset.)"""

    navigate = pyqtSignal(str, object)
    tagsRequested = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.setObjectName("toolRail")
        self.setFixedWidth(38)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._nav_buttons: dict[str, RailButton] = {}

        self._add_nav_button(layout, "library", "LIBRARY")
        for kind, label in TOOLS:
            btn = self._add_nav_button(layout, kind, label)
            if kind == "tags":
                btn.clicked.connect(self.tagsRequested.emit)

    def _add_nav_button(self, layout: QVBoxLayout, kind: str, label_text: str) -> RailButton:
        btn = RailButton(label_text)
        btn.clicked.connect(lambda: self.select(kind))
        layout.addWidget(btn)
        self._nav_buttons[kind] = btn
        return btn

    def select(self, kind: str):
        """Programmatic navigation (e.g. Fix Tags, Send Segments to Tags,
        Merge's "then split") — mirrors a click on the given rail button."""
        if kind not in self._nav_buttons:
            return
        for k, btn in self._nav_buttons.items():
            btn.setChecked(k == kind)
        self.navigate.emit(kind, None)

    def deselect_all(self):
        """Called when a playlist (owned by the separate PlaylistDeck) is
        selected instead, so a rail button doesn't stay lit for a page
        that's no longer showing."""
        for btn in self._nav_buttons.values():
            btn.setChecked(False)


class PlaylistDeck(QWidget):
    """Emits navigate("playlist", (playlist_id, name)) on selection,
    createRequested when the "+" button is clicked, and renameRequested when
    a double-click-to-edit finishes. This widget never touches the database
    itself — MainWindow owns that, same separation as everywhere else."""

    navigate = pyqtSignal(str, object)
    createRequested = pyqtSignal()
    renameRequested = pyqtSignal(int, str)

    def __init__(self):
        super().__init__()
        self.setObjectName("playlistDeck")
        self.setFixedWidth(210)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 10, 4, 10)
        layout.setSpacing(4)

        header_row = QHBoxLayout()
        header = PixelLabel("PLAYLISTS", point_size=12, color=MUTED, letter_spacing=2)
        header_row.addWidget(header)
        header_row.addStretch(1)
        add_btn = QPushButton("+")
        add_btn.setFixedSize(20, 20)
        add_btn.setToolTip("New playlist")
        add_btn.clicked.connect(self.createRequested.emit)
        header_row.addWidget(add_btn)
        layout.addLayout(header_row)

        self.playlist_list = QListWidget()
        self.playlist_list.setObjectName("sidebarList")
        self.playlist_list.itemClicked.connect(self._on_playlist_clicked)
        self.playlist_list.itemChanged.connect(self._on_playlist_renamed)
        # No fixed/capped height here, deliberately — the list fills whatever
        # vertical space the deck column has and scrolls on its own past
        # that. A second, outer scroll region on top of this is exactly the
        # double-scrollbar bug this widget replaces.
        layout.addWidget(self.playlist_list, stretch=1)

    def _on_playlist_clicked(self, item: QListWidgetItem):
        playlist_id = item.data(Qt.ItemDataRole.UserRole)
        self.navigate.emit("playlist", (playlist_id, item.text()))

    def _on_playlist_renamed(self, item: QListWidgetItem):
        new_name = item.text().strip()
        if not new_name:
            return
        playlist_id = item.data(Qt.ItemDataRole.UserRole)
        self.renameRequested.emit(playlist_id, new_name)

    def set_playlists(self, playlists: list[tuple[int, str]]):
        """playlists: list of (playlist_id, name), in display order."""
        self.playlist_list.blockSignals(True)  # rebuilding the list is not a user rename
        self.playlist_list.clear()
        for playlist_id, name in playlists:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            item.setData(Qt.ItemDataRole.UserRole, playlist_id)
            self.playlist_list.addItem(item)
        self.playlist_list.blockSignals(False)

    def clear_selection(self):
        """Called when a rail button (owned by the separate ToolRail) is
        selected instead, so no playlist stays highlighted for a page that's
        no longer showing."""
        self.playlist_list.clearSelection()
