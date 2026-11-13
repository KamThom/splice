"""Duplicate review: the one-pair Resolve dialog, and Auto-resolve's
matching rules and preview dialog."""

from pathlib import Path

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPushButton,
    QVBoxLayout,
)

from ..track_lists import _format_duration, _format_filesize


class ResolveDuplicateDialog(QDialog):
    """Shows two duplicate-flagged tracks side by side — title/artist/album,
    duration, and live file size (the two cheap signals that actually help
    judge "which copy is better", e.g. a larger file usually means a higher
    bitrate rip) — and lets the user pick which one to keep. Returns the
    decision via self.action ("keep" / "skip" / "dismiss") plus
    self.keep_id/self.remove_id after exec() accepts; a reject (STOP or
    closing the window) means stop going through pairs. Does no deletion
    itself, that's the caller's job. `position` is a "(2 of 7)"-style label
    shown in the title while the caller steps through the whole list."""

    GEOMETRY_KEY = "resolve_dupe_dialog/geometry"

    def __init__(self, parent, library_root: str, track_a, track_b, position: str = ""):
        super().__init__(parent)
        self.action = None
        self.keep_id = None
        self.remove_id = None
        self._library_root = library_root
        self.setWindowTitle(f"Resolve Duplicate {position}".strip())
        # Reuse whatever size/position the last one was left at, so stepping
        # through pairs (and later sessions) doesn't snap back to the default.
        self._settings = QSettings("Thomas", "Splice")
        saved = self._settings.value(self.GEOMETRY_KEY)
        if saved is None or not self.restoreGeometry(saved):
            self.resize(560, 260)

        layout = QVBoxLayout(self)
        hint = QLabel("These look like the same track. Pick which copy to keep — the other is "
                      "moved to the Trash (originals elsewhere are never touched).")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        columns = QHBoxLayout()
        columns.addLayout(self._build_column(track_a, track_b["id"]))
        columns.addLayout(self._build_column(track_b, track_a["id"]))
        layout.addLayout(columns)

        cancel_row = QHBoxLayout()
        dismiss_btn = QPushButton("Not a duplicate")
        dismiss_btn.setToolTip("Keep both copies and stop flagging this pair")
        dismiss_btn.clicked.connect(lambda: self._finish("dismiss"))
        cancel_row.addWidget(dismiss_btn)
        skip_btn = QPushButton("Skip")
        skip_btn.setToolTip("Leave this pair for later and go to the next one")
        skip_btn.clicked.connect(lambda: self._finish("skip"))
        cancel_row.addWidget(skip_btn)
        cancel_row.addStretch(1)
        cancel_btn = QPushButton("Stop")
        cancel_btn.clicked.connect(self.reject)
        cancel_row.addWidget(cancel_btn)
        layout.addLayout(cancel_row)

    def _build_column(self, track, other_track_id: int) -> QVBoxLayout:
        col = QVBoxLayout()
        full_path = Path(self._library_root) / track["library_path"]
        for text, obj_name in [
            (track["title"] or "(no title)", "panelTitle"),
            (track["artist"] or "Unknown Artist", "muted"),
            (track["album"] or "", "muted"),
            (f"{_format_duration(track['duration_seconds'])}  •  {_format_filesize(full_path)}", "muted"),
            (track["library_path"], "muted"),
        ]:
            label = QLabel(text)
            label.setObjectName(obj_name)
            label.setWordWrap(True)
            col.addWidget(label)
        col.addStretch(1)
        keep_btn = QPushButton("Keep this one")
        keep_btn.setObjectName("primary")
        keep_btn.clicked.connect(lambda: self._choose(track["id"], other_track_id))
        col.addWidget(keep_btn)
        return col

    def _choose(self, keep_id: int, remove_id: int):
        self.keep_id = keep_id
        self.remove_id = remove_id
        self._finish("keep")

    def _finish(self, action: str):
        self.action = action
        self.accept()

    def done(self, result: int):
        # Every way out (KEEP/SKIP/NOT A DUPLICATE, STOP, Esc, the window's X) goes through done().
        self._settings.setValue(self.GEOMETRY_KEY, self.saveGeometry())
        super().done(result)


def _norm_tag(value) -> str:
    """Case/whitespace-insensitive form of a tag for "is this the same song" checks."""
    return " ".join((value or "").casefold().split())


AUTO_RESOLVE_MAX_DURATION_GAP = 5.0  # seconds; a bigger gap suggests a different version (live, edit, ...)


def plan_auto_resolve(library_root: str, pairs, require_album: bool) -> tuple[list[dict], int]:
    """Decides which duplicate pairs are safe to resolve without asking: same
    title and artist (and album, if require_album), durations within a few
    seconds of each other, both files present on disk. For each one, keeps
    the larger file — usually the higher-bitrate rip — and on a tie the
    older track. `pairs` is a list of (dupe_id, track_a, track_b). Returns
    (plan, left_for_review), each plan entry a dict with dupe_id, keep,
    remove, keep_size, remove_size. A pair whose track is already slated
    for removal is skipped (its row disappears with that track anyway). A
    kept track can still lose to an even larger copy in a later pair
    (A<B, B<C ends with only C), which is fine since each step keeps the
    bigger file, and execution runs in this order so playlist entries
    follow along A→B→C."""
    plan = []
    removing = set()
    for dupe_id, a, b in pairs:
        if a["id"] in removing or b["id"] in removing:
            continue
        if not _norm_tag(a["title"]) or _norm_tag(a["title"]) != _norm_tag(b["title"]):
            continue
        if _norm_tag(a["artist"]) != _norm_tag(b["artist"]):
            continue
        if require_album and _norm_tag(a["album"]) != _norm_tag(b["album"]):
            continue
        da, db_ = a["duration_seconds"], b["duration_seconds"]
        if da and db_ and abs(da - db_) > AUTO_RESOLVE_MAX_DURATION_GAP:
            continue
        try:
            size_a = (Path(library_root) / a["library_path"]).stat().st_size
            size_b = (Path(library_root) / b["library_path"]).stat().st_size
        except OSError:
            continue  # a missing file is a Health problem, not a judgement call
        if size_a > size_b or (size_a == size_b and a["id"] < b["id"]):
            keep, remove, keep_size, remove_size = a, b, size_a, size_b
        else:
            keep, remove, keep_size, remove_size = b, a, size_b, size_a
        removing.add(remove["id"])
        plan.append({"dupe_id": dupe_id, "keep": keep, "remove": remove,
                     "keep_size": keep_size, "remove_size": remove_size})
    return plan, len(pairs) - len(plan)


class AutoResolveDialog(QDialog):
    """Preview for AUTO-RESOLVE: lists every pair plan_auto_resolve would
    handle (which copy is kept, which is deleted, and their sizes) so the
    user can sanity-check before anything is removed. Nothing is deleted
    here — after exec() accepts, the caller acts on self.plan."""

    GEOMETRY_KEY = "auto_resolve_dialog/geometry"

    def __init__(self, parent, library_root: str, pairs):
        super().__init__(parent)
        self._library_root = library_root
        self._pairs = pairs
        self.plan = []
        self.setWindowTitle("Auto-Resolve Duplicates")
        self._settings = QSettings("Thomas", "Splice")
        saved = self._settings.value(self.GEOMETRY_KEY)
        if saved is None or not self.restoreGeometry(saved):
            self.resize(720, 460)

        layout = QVBoxLayout(self)
        hint = QLabel(
            "Pairs with the same title and artist, and lengths within "
            f"{AUTO_RESOLVE_MAX_DURATION_GAP:.0f} seconds of each other, keep the larger file "
            "(usually the higher-quality copy). The smaller one goes to the Trash and is removed from your "
            "library and its playlist entries point at the kept copy. Everything else stays in "
            "the list for you to review."
        )
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.album_check = QCheckBox("Only when the album matches too")
        self.album_check.setChecked(self._settings.value("auto_resolve_dialog/require_album", False, type=bool))
        self.album_check.toggled.connect(self._replan)
        layout.addWidget(self.album_check)

        self.preview_list = QListWidget()
        layout.addWidget(self.preview_list, 1)

        self.summary = QLabel()
        self.summary.setObjectName("muted")
        layout.addWidget(self.summary)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        buttons.addWidget(cancel_btn)
        self.go_btn = QPushButton()
        self.go_btn.setObjectName("primary")
        self.go_btn.clicked.connect(self.accept)
        buttons.addWidget(self.go_btn)
        layout.addLayout(buttons)

        self._replan()

    def _replan(self):
        self.plan, left = plan_auto_resolve(self._library_root, self._pairs, self.album_check.isChecked())
        self.preview_list.clear()
        for entry in self.plan:
            keep, remove = entry["keep"], entry["remove"]
            self.preview_list.addItem(
                f"{keep['title']} — {keep['artist']}\n"
                f"   Keep     {keep['library_path']}  ({entry['keep_size'] / (1024 * 1024):.1f} MB)\n"
                f"   Trash    {remove['library_path']}  ({entry['remove_size'] / (1024 * 1024):.1f} MB)"
            )
        self.summary.setText(f"{len(self.plan)} pair(s) will be resolved · {left} left for manual review")
        self.go_btn.setText(f"Resolve {len(self.plan)} pair(s)")
        self.go_btn.setEnabled(bool(self.plan))

    def done(self, result: int):
        self._settings.setValue(self.GEOMETRY_KEY, self.saveGeometry())
        self._settings.setValue("auto_resolve_dialog/require_album", self.album_check.isChecked())
        super().done(result)
