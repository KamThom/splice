"""Left-hand navigation, split into two independent widgets: a slim icon-only
ToolRail (Library home + Merge/Split/YouTube/Tags) and a full-height
PlaylistDeck. Splitting them (rather than one long stacked sidebar) is what
lets Playlists get its own single-scrollbar column instead of a capped,
double-scrolling section fighting the tools list for space."""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget

# (kind, tooltip label, icon glyph)
TOOLS = [("merge", "MERGE", "▤"), ("split", "SPLIT", "✂"), ("youtube", "YOUTUBE", "▾"), ("tags", "TAGS", "#")]


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
        self.setFixedWidth(72)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 14, 8, 12)
        layout.setSpacing(6)

        self._nav_buttons: dict[str, QPushButton] = {}

        self._add_nav_button(layout, "library", "LIBRARY", "◆")
        layout.addSpacing(10)
        for kind, label, icon in TOOLS:
            btn = self._add_nav_button(layout, kind, label, icon)
            if kind == "tags":
                btn.clicked.connect(self.tagsRequested.emit)

        layout.addStretch(1)

    def _add_nav_button(self, layout: QVBoxLayout, kind: str, label_text: str, icon: str) -> QPushButton:
        btn = QPushButton(icon)
        btn.setObjectName("railNav")
        btn.setToolTip(label_text)
        btn.setCheckable(True)
        btn.setFixedSize(52, 48)
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
        header = QLabel("PLAYLISTS")
        header.setObjectName("sidebarSection")
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
