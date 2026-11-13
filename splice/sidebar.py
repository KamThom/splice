"""Left-hand sidebar: the logo, the two main library sections (Songs,
Audiobooks), small "needs attention" chips for cleanup pages (Duplicates,
Unsorted, Health), the playlists list, and a slot at the bottom for the
compact now-playing player. Tools (Merge/Split/YouTube/Tags) deliberately
don't live here — they're in MainWindow's top bar — so the sidebar stays
about browsing your music."""

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .styles import ACCENT, MUTED
from .widgets import DisplayLabel, SunsetStripe, render_rounded_pixmap

# (kind, label) for the main browse sections and the cleanup chips — the
# kind strings are what Sidebar.navigate emits and MainWindow routes on.
LIBRARY_SECTIONS = [("songs", "Songs"), ("audiobooks", "Audiobooks")]
ATTENTION_SECTIONS = [("duplicates", "Dupes"), ("unsorted", "Unsorted"), ("health", "Health")]


class NavButton(QPushButton):
    """A checkable sidebar row: label on the left, an optional muted count
    on the right. Real child QLabels inside a QPushButton (rather than one
    text string) so the count can be right-aligned and colored separately."""

    def __init__(self, text: str, object_name: str = "navItem"):
        super().__init__()
        self.setObjectName(object_name)
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 0, 12, 0)
        row.setSpacing(6)
        self.text_label = QLabel(text)
        self.text_label.setObjectName("navText")
        self.text_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.count_label = QLabel("")
        self.count_label.setObjectName("navCount")
        self.count_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.text_label)
        row.addStretch(1)
        row.addWidget(self.count_label)
        # Child labels don't get re-polished when the button's own
        # :checked state changes, so mirror it onto them as a property the
        # stylesheet can match on.
        self.toggled.connect(self._sync_label_state)

    def _sync_label_state(self, checked: bool):
        for label in (self.text_label, self.count_label):
            label.setProperty("active", checked)
            label.style().unpolish(label)
            label.style().polish(label)

    def set_count(self, text: str):
        self.count_label.setText(text)

    def set_alert(self, alert: bool):
        self.count_label.setProperty("alert", alert)
        self.count_label.style().unpolish(self.count_label)
        self.count_label.style().polish(self.count_label)

    def sizeHint(self) -> QSize:
        if self.objectName() == "attentionChip":
            return QSize(self.layout().sizeHint().width(), 26)  # just as wide as "Label N"
        return QSize(160, 34)


class PlaylistDeck(QWidget):
    """Emits navigate("playlist", (playlist_id, name)) on selection,
    createRequested when the "+" button is clicked, and renameRequested when
    a double-click-to-edit finishes. This widget never touches the database
    or filesystem itself — MainWindow owns that (including reading each
    playlist's artwork bytes, if any, before calling set_playlists), same
    separation as everywhere else."""

    navigate = pyqtSignal(str, object)
    createRequested = pyqtSignal()
    renameRequested = pyqtSignal(int, str)

    THUMB_SIZE = 26
    THUMB_RADIUS = 6

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(12, 0, 4, 0)
        header = DisplayLabel("Playlists", point_size=11, color=MUTED, letter_spacing=1)
        header_row.addWidget(header)
        header_row.addStretch(1)
        add_btn = QPushButton("+")
        add_btn.setObjectName("sidebarAdd")
        add_btn.setToolTip("New playlist")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.clicked.connect(self.createRequested.emit)
        header_row.addWidget(add_btn)
        layout.addLayout(header_row)

        self.playlist_list = QListWidget()
        self.playlist_list.setObjectName("sidebarList")
        self.playlist_list.setIconSize(QSize(self.THUMB_SIZE, self.THUMB_SIZE))
        self.playlist_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.playlist_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.playlist_list.itemClicked.connect(self._on_playlist_clicked)
        self.playlist_list.itemChanged.connect(self._on_playlist_renamed)
        # Fills whatever vertical space is left and scrolls on its own —
        # one scroll region, never nested inside another.
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

    def set_playlists(self, playlists: list[tuple[int, str, bytes | None]]):
        """playlists: list of (playlist_id, name, artwork_bytes_or_None), in
        display order."""
        selected_id = None
        current = self.playlist_list.currentItem()
        if current is not None and current.isSelected():
            selected_id = current.data(Qt.ItemDataRole.UserRole)
        self.playlist_list.blockSignals(True)  # rebuilding the list is not a user rename
        self.playlist_list.clear()
        for playlist_id, name, art_bytes in playlists:
            item = QListWidgetItem(name)
            item.setIcon(QIcon(render_rounded_pixmap(art_bytes, self.THUMB_SIZE, self.THUMB_RADIUS)))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
            item.setData(Qt.ItemDataRole.UserRole, playlist_id)
            item.setToolTip(name)
            self.playlist_list.addItem(item)
            if playlist_id == selected_id:
                item.setSelected(True)
                self.playlist_list.setCurrentItem(item)
        self.playlist_list.blockSignals(False)

    def select_playlist(self, playlist_id: int):
        for i in range(self.playlist_list.count()):
            item = self.playlist_list.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == playlist_id:
                self.playlist_list.setCurrentItem(item)
                return

    def clear_selection(self):
        self.playlist_list.clearSelection()


class Sidebar(QWidget):
    """navigate(kind, payload): kind is one of the LIBRARY_SECTIONS /
    ATTENTION_SECTIONS kinds (payload None) or "playlist" (payload
    (playlist_id, name)). createRequested/renameRequested are forwarded
    from the embedded PlaylistDeck. Highlighting is driven from outside
    via select()/clear_nav(), so programmatic navigation (Fix Tags, Back
    from a series, ...) keeps the sidebar honest too."""

    navigate = pyqtSignal(str, object)
    createRequested = pyqtSignal()
    renameRequested = pyqtSignal(int, str)

    WIDTH = 244

    def __init__(self):
        super().__init__()
        self.setObjectName("sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedWidth(self.WIDTH)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 18, 10, 12)
        layout.setSpacing(2)

        logo_col = QVBoxLayout()
        logo_col.setContentsMargins(12, 0, 0, 10)
        logo_col.setSpacing(4)
        logo_col.addWidget(DisplayLabel("splice", point_size=20, color=ACCENT, letter_spacing=1))
        logo_col.addWidget(SunsetStripe(width=64, height=4))
        layout.addLayout(logo_col)

        layout.addWidget(self._section_label("Library"))
        self._buttons: dict[str, NavButton] = {}
        for kind, label in LIBRARY_SECTIONS:
            layout.addWidget(self._make_button(kind, label, "navItem"))

        chips = QHBoxLayout()
        chips.setContentsMargins(6, 6, 0, 4)
        chips.setSpacing(4)
        for kind, label in ATTENTION_SECTIONS:
            chips.addWidget(self._make_button(kind, label, "attentionChip"))
        chips.addStretch(1)
        layout.addLayout(chips)

        layout.addSpacing(10)
        self.playlist_deck = PlaylistDeck()
        self.playlist_deck.navigate.connect(self._on_playlist_navigate)
        self.playlist_deck.createRequested.connect(self.createRequested.emit)
        self.playlist_deck.renameRequested.connect(self.renameRequested.emit)
        layout.addWidget(self.playlist_deck, stretch=1)

        self._player_slot = QVBoxLayout()
        self._player_slot.setContentsMargins(0, 8, 0, 0)
        layout.addLayout(self._player_slot)

    def _section_label(self, text: str) -> QLabel:
        label = DisplayLabel(text, point_size=11, color=MUTED, letter_spacing=1)
        label.setContentsMargins(12, 6, 0, 4)
        return label

    def _make_button(self, kind: str, label: str, object_name: str) -> NavButton:
        btn = NavButton(label, object_name)
        if object_name == "attentionChip":
            # Chips are compact: just "Label N", no stretch between them.
            btn.layout().setContentsMargins(8, 0, 8, 0)
            btn.layout().setSpacing(4)
            btn.setToolTip({"duplicates": "Tracks that look like duplicates",
                            "unsorted": "Files with missing or unusable tags",
                            "health": "Library checkup"}[kind])
            btn.layout().takeAt(1)  # drop the stretch
            btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            btn.setFixedHeight(26)
        btn.clicked.connect(lambda checked=False, k=kind: self._on_button_clicked(k))
        self._buttons[kind] = btn
        return btn

    def _on_button_clicked(self, kind: str):
        self.select(kind)
        self.navigate.emit(kind, None)

    def _on_playlist_navigate(self, kind: str, payload):
        self.clear_nav()
        self.navigate.emit(kind, payload)

    # ---- state in ----

    def set_player(self, widget: QWidget):
        self._player_slot.addWidget(widget)

    def set_playlists(self, playlists):
        self.playlist_deck.set_playlists(playlists)

    def set_counts(self, songs: int, audiobooks: int, duplicates: int, unsorted: int):
        self._buttons["songs"].set_count(f"{songs:,}")
        self._buttons["audiobooks"].set_count(f"{audiobooks:,}")
        for kind, n in (("duplicates", duplicates), ("unsorted", unsorted)):
            btn = self._buttons[kind]
            btn.set_count(f"{n:,}" if n else "")
            btn.set_alert(bool(n))

    def select(self, kind: str):
        """Highlights the given section (and un-highlights any playlist)."""
        for k, btn in self._buttons.items():
            btn.setChecked(k == kind)
        self.playlist_deck.clear_selection()

    def select_playlist(self, playlist_id: int):
        self.clear_nav()
        self.playlist_deck.select_playlist(playlist_id)

    def clear_nav(self):
        for btn in self._buttons.values():
            btn.setChecked(False)
