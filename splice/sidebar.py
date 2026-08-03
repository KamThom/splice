"""Left sidebar navigation: Library/Search at top, a hand-curated Playlists list,
and a Tools section (Merge/Split/YouTube/Tags) — replaces the old top tab bar."""

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QLabel, QListWidget, QListWidgetItem, QPushButton, QVBoxLayout, QWidget

TOOLS = [("merge", "MERGE"), ("split", "SPLIT"), ("youtube", "YOUTUBE"), ("tags", "TAGS")]

PLAYLIST_MAX_VISIBLE_ROWS = 6


class Sidebar(QWidget):
    """Emits navigate(kind, payload) on any selection:
    kind is "library" | "search" | one of the TOOLS keys | "playlist", payload
    is None except for "playlist" (a (playlist_id, name) tuple)."""

    navigate = pyqtSignal(str, object)

    def __init__(self):
        super().__init__()
        self.setObjectName("sidebar")
        self.setFixedWidth(190)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 10, 6, 10)
        layout.setSpacing(2)

        self._nav_buttons: dict[str, QPushButton] = {}

        self._add_nav_button(layout, "library", "LIBRARY")
        self._add_nav_button(layout, "search", "SEARCH")

        layout.addSpacing(10)
        self._playlists_label = self._section_label("PLAYLISTS")
        layout.addWidget(self._playlists_label)
        self.playlist_list = QListWidget()
        self.playlist_list.setObjectName("sidebarList")
        self.playlist_list.itemClicked.connect(self._on_playlist_clicked)
        layout.addWidget(self.playlist_list)
        self._resize_playlist_list()  # starts empty -> collapsed, no gap

        layout.addSpacing(10)
        layout.addWidget(self._section_label("TOOLS"))
        for kind, label in TOOLS:
            self._add_nav_button(layout, kind, label)

        layout.addStretch(1)

    def _section_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("sidebarSection")
        return label

    def _add_nav_button(self, layout: QVBoxLayout, kind: str, label_text: str):
        btn = QPushButton(label_text)
        btn.setObjectName("sidebarNav")
        btn.setCheckable(True)
        btn.clicked.connect(lambda: self.select(kind))
        layout.addWidget(btn)
        self._nav_buttons[kind] = btn

    def _on_playlist_clicked(self, item: QListWidgetItem):
        for btn in self._nav_buttons.values():
            btn.setChecked(False)
        playlist_id = item.data(Qt.ItemDataRole.UserRole)
        self.navigate.emit("playlist", (playlist_id, item.text()))

    def select(self, kind: str):
        """Programmatic navigation (e.g. from the Split module's "send to Tags"
        shortcut) — mirrors a click on the given nav button, kept in sync
        visually with whatever's actually showing."""
        if kind not in self._nav_buttons:
            return
        self.playlist_list.clearSelection()
        for k, btn in self._nav_buttons.items():
            btn.setChecked(k == kind)
        self.navigate.emit(kind, None)

    def set_playlists(self, playlists: list[tuple[int, str]]):
        """playlists: list of (playlist_id, name), in display order."""
        self.playlist_list.clear()
        for playlist_id, name in playlists:
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, playlist_id)
            self.playlist_list.addItem(item)
        self._resize_playlist_list()

    def _resize_playlist_list(self):
        """No static height — collapses to nothing when there are no playlists
        (instead of leaving a dead gap), then grows to fit as playlists are
        added, up to PLAYLIST_MAX_VISIBLE_ROWS, beyond which it scrolls
        normally rather than growing further."""
        count = self.playlist_list.count()
        has_playlists = count > 0
        self._playlists_label.setVisible(has_playlists)
        self.playlist_list.setVisible(has_playlists)
        if not has_playlists:
            return
        row_height = self.playlist_list.sizeHintForRow(0)
        visible_rows = min(count, PLAYLIST_MAX_VISIBLE_ROWS)
        frame = 2 * self.playlist_list.frameWidth()
        self.playlist_list.setFixedHeight(visible_rows * row_height + frame + 4)
