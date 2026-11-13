"""Shared library actions used by every view: deleting (to the system
Trash, with a one-time permanent-delete fallback), the "N selected"
action bar, and right-click menus on track lists."""

from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMenu,
    QMessageBox,
    QPushButton,
    QWidget,
)

try:
    from send2trash import send2trash
except ImportError:  # not installed: deletes fall back to asking about permanent removal
    send2trash = None
from .library import db as libdb
from .library.playlists import (
    write_m3u8,
)
from .library.series_artwork import (
    clear_series_artwork,
)

# ---- shared actions: delete, selection bar, right-click menus ----

_TOP_LEVEL_DIRS = {"Music", "Audiobooks", "Playlists"}


def _prune_empty_dirs(file_path: Path, library_root: str):
    """After deleting a file, removes the folders it leaves empty (an
    artist/album folder, a series folder), walking up but never past the
    library's own top-level Music/Audiobooks/Playlists folders."""
    root = Path(library_root).resolve()
    folder = file_path.parent
    while True:
        try:
            resolved = folder.resolve()
        except OSError:
            return
        if resolved == root or resolved.parent == resolved or folder.name in _TOP_LEVEL_DIRS:
            return
        if root not in resolved.parents:
            return
        try:
            folder.rmdir()  # only succeeds if empty
        except OSError:
            return
        folder = folder.parent


def delete_library_tracks(parent, library_root: str, tracks: list[tuple[int, str]],
                          noun: str = "file", question: str | None = None) -> int:
    """The one delete path every view uses. tracks: (track_id, path) pairs,
    path relative to library_root (an absolute path works too). Asks for
    confirmation, deletes each file and its index entry, rewrites any
    playlists that referenced it, tidies up emptied folders, and reports
    files another program had locked. Returns how many were deleted, or -1
    if the user cancelled."""
    if not tracks:
        return 0
    n = len(tracks)
    plural = noun if n == 1 else f"{noun}s"
    reply = QMessageBox.question(
        parent, "Delete from library",
        (question or f"Delete {n:,} {plural} from your library?")
        + "\n\nThey go to your system Trash, so you can get them back from there. This removes "
          "Splice's copy only; your original source folders aren't touched.",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if reply != QMessageBox.StandardButton.Yes:
        return -1
    paths = [Path(library_root) / rel_path for _, rel_path in tracks]
    results = remove_files(parent, paths)
    conn = libdb.open_db(library_root)
    affected_playlist_ids = set()
    failed = []
    deleted = 0
    for (track_id, _rel), path in zip(tracks, paths):
        if results.get(path):
            failed.append(path.name)
            continue  # leave its index entry in place — the file's still really there
        affected_playlist_ids.update(libdb.playlists_containing_track(conn, track_id))
        libdb.delete_track(conn, track_id)
        _prune_empty_dirs(path, library_root)
        deleted += 1
    for playlist_id in affected_playlist_ids:
        row = conn.execute("SELECT name FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
        if row:
            paths = libdb.playlist_track_paths_in_order(conn, playlist_id)
            write_m3u8(str(Path(library_root) / "Playlists"), row["name"], paths, library_root)
    conn.close()
    if failed:
        QMessageBox.warning(
            parent, "Some files were left in your library",
            "These weren't deleted. They may be open in another program, or you chose not to "
            "delete them permanently:\n\n" + "\n".join(failed),
        )
    return deleted


def delete_audiobook_series(parent, library_root: str, series_name: str) -> int:
    """Deletes every book in a series plus the series' artwork. Returns the
    number of books deleted, or -1 if cancelled."""
    conn = libdb.open_db(library_root)
    books = libdb.list_tracks_in_series(conn, series_name)
    conn.close()
    if not books:
        return 0
    count = len(books)
    deleted = delete_library_tracks(
        parent, library_root, [(b["id"], b["library_path"]) for b in books], noun="book",
        question=f"Delete the series \u201c{series_name}\u201d and all {count} of its "
                 f"{'book' if count == 1 else 'books'}?",
    )
    if deleted == count:
        clear_series_artwork(library_root, series_name)
    return deleted


class SelectionBar(QWidget):
    """The action row under every track list: "N selected" on the left,
    then the actions. Follows one or more lists' selections — get_list
    returns whichever list is currently showing (Songs vs Audiobooks share
    one bar)."""

    def __init__(self, lists: list, get_list=None, hint: str = "Select items to act on them, or right-click one"):
        super().__init__()
        self._get_list = get_list or (lambda: lists[0])
        self._hint = hint
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        self.count_label = QLabel("")
        self.count_label.setObjectName("muted")
        row.addWidget(self.count_label)
        row.addStretch(1)
        self._row = row
        for lw in lists:
            lw.itemSelectionChanged.connect(self.update_count)
        self.update_count()

    def add_button(self, text: str, slot, danger: bool = False) -> QPushButton:
        btn = QPushButton(text)
        if danger:
            btn.setObjectName("danger")
        btn.clicked.connect(lambda checked=False: slot())
        self._row.addWidget(btn)
        return btn

    def update_count(self):
        lw = self._get_list()
        n = len(lw.selectedItems()) if lw is not None else 0
        self.count_label.setText(f"{n:,} selected" if n else self._hint)


def attach_context_menu(list_widget: QListWidget, build_menu):
    """Right-click on a row: if that row isn't already part of the
    selection, select just it (so the menu always acts on what you
    clicked), then show build_menu(menu) — which fills in the actions."""
    list_widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    def show(pos):
        item = list_widget.itemAt(pos)
        if item is None:
            return
        if not item.isSelected():
            list_widget.clearSelection()
            item.setSelected(True)
            list_widget.setCurrentItem(item)
        menu = QMenu(list_widget)
        build_menu(menu, item)
        if not menu.isEmpty():
            menu.exec(list_widget.viewport().mapToGlobal(pos))

    list_widget.customContextMenuRequested.connect(show)


def add_menu_action(menu: QMenu, text: str, slot, danger: bool = False) -> QAction:
    """`danger` is documentation only for now — QMenu items can't be styled
    individually, so destructive actions are set apart with a separator."""
    action = menu.addAction(text)
    action.triggered.connect(lambda checked=False: slot())
    return action


def remove_files(parent, paths: list[Path], decision: dict | None = None) -> dict:
    """Removes library files by moving them to the system Trash, so a
    mistaken delete can be undone from the file manager. Returns
    {path: None if removed, else a short reason}.

    If some can't be trashed (a drive without a Trash folder, a filesystem
    that won't allow one, send2trash not installed) the user is asked ONCE
    whether to delete those permanently instead. Pass the same `decision`
    dict across calls (e.g. every pair in an auto-resolve) so that question
    is only ever asked once per operation."""
    decision = decision if decision is not None else {}
    results: dict = {}
    untrashable: list[Path] = []
    for path in paths:
        if not path.exists():
            results[path] = None  # already gone
            continue
        if send2trash is None:
            untrashable.append(path)
            continue
        try:
            send2trash(str(path))
            results[path] = None
        except Exception:  # send2trash raises several types depending on platform/filesystem
            untrashable.append(path)
    if untrashable:
        if "permanent" not in decision:
            n = len(untrashable)
            reply = QMessageBox.question(
                parent, "Can't use the Trash",
                f"{n:,} file{'' if n == 1 else 's'} couldn't be moved to the Trash (this drive may "
                f"not have one). Delete {'it' if n == 1 else 'them'} permanently instead?\n\n"
                f"This can't be undone.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            decision["permanent"] = reply == QMessageBox.StandardButton.Yes
        for path in untrashable:
            results[path] = _try_unlink(path) if decision["permanent"] else "not moved to the Trash"
    return results


def _try_unlink(path: Path) -> str | None:
    """Attempts to delete a library file, returning None on success
    (including "already gone" — missing_ok=True) or a short reason string
    on failure. A file can be transiently locked by another process on
    Windows (a search indexer or antivirus scan mid-sweep, a leftover
    mutagen/media handle) — that must never be allowed to escape as an
    uncaught PermissionError and crash the whole app the way it did before
    this existed; callers skip that one file and keep going instead."""
    try:
        path.unlink(missing_ok=True)
        return None
    except OSError as e:
        return str(e)
