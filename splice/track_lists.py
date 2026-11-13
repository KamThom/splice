"""Track tables: column layouts, sorting, filling a TrackListWidget from
index rows, and the small formatting helpers (times, sizes, dates, book
numbers) the tables and dialogs share."""

from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QListWidget,
    QListWidgetItem,
)

from . import tagging
from .widgets import (
    TrackListWidget,
    TrackRowDelegate,
    render_mosaic_bytes,
)


def _is_unsorted(library_path: str) -> bool:
    return "_Unsorted" in Path(library_path).parts


def _collect_cover_art(library_root: str, ordered_paths: list[str], limit: int = 4) -> list[bytes]:
    """Reads embedded art off the first `limit` tracks (in playlist order)
    that actually have any, for building a playlist's auto-mosaic fallback.
    Stops as soon as `limit` covers are found — a playlist can have hundreds
    of tracks and this only ever needs a handful for a 2x2 mosaic."""
    covers = []
    for rel_path in ordered_paths:
        if len(covers) >= limit:
            break
        try:
            art = tagging.read_tags(str(Path(library_root) / rel_path)).get("art")
        except Exception:
            art = None
        if art:
            covers.append(art)
    return covers


def playlist_mosaic_art(library_root: str, ordered_paths: list[str]) -> bytes | None:
    """Auto-generated fallback artwork for a playlist with no explicit
    artwork set yet — a mosaic of its first few tracks' own covers. Callers
    should cache the result keyed on (playlist_id, ordered_paths[:4]) —
    this itself does no caching, since it has no notion of "this playlist"
    across calls, only "these paths right now"."""
    return render_mosaic_bytes(_collect_cover_art(library_root, ordered_paths))


def make_track_list(columns: list | None = None) -> TrackListWidget:
    """Read-only card-style track list, shared by the Library view's Songs
    and Audiobooks tabs. Each row is painted by TrackRowDelegate (title
    bold, artist muted underneath, a playlist-count badge) rather than
    Qt's default single-line item — setMouseTracking is required for the
    delegate's hover state to update as the mouse moves, not just on click.
    TrackListWidget (rather than a plain QListWidget) is what lets clicking
    directly on the badge pill emit badgeClicked instead of just selecting
    the row."""
    list_widget = TrackListWidget(columns)
    list_widget.setItemDelegate(TrackRowDelegate(list_widget))
    list_widget.setMouseTracking(True)
    list_widget.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
    list_widget.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    return list_widget


def populate_track_list(list_widget: QListWidget, tracks, membership: dict):
    list_widget.clear()
    for t in tracks:
        item = QListWidgetItem()
        count = membership.get(t["id"], 0)
        item.setData(Qt.ItemDataRole.UserRole, {
            "id": t["id"],
            "library_path": t["library_path"],
            "title": t["title"] or Path(t["library_path"]).stem,
            "artist": t["artist"] or "",
            "album": t["album"] or "",
            "added": _format_added_date(t["added_at"]) if t["added_at"] else "",
            "time": _format_length(t["duration_seconds"]),
            "badge": str(count) if count else None,
            "loudness_lufs": t["loudness_lufs"],
            "loudness_true_peak": t["loudness_true_peak"],
        })
        list_widget.addItem(item)


def selected_library_paths(list_widget: QListWidget) -> list[str]:
    """library_path for each selected row — used by "Copy selected…" on
    the Songs/Audiobooks tabs and by PlaylistView."""
    paths = []
    for item in list_widget.selectedItems():
        data = item.data(Qt.ItemDataRole.UserRole)
        if data:
            paths.append(data["library_path"])
    return paths


SORT_FIELDS = ["Title", "Artist", "Album", "Date Added", "Time", "In"]

# Column layouts for the single-line table rows: (key, header label, width share).
SONG_COLUMNS = [("title", "Title", 34), ("artist", "Artist", 22), ("album", "Album", 22),
                ("added", "Added", 12), ("time", "Time", 8)]
PLAYLIST_COLUMNS = [("pos", "#", 4), ("title", "Title", 38), ("artist", "Artist", 26), ("album", "Album", 24),
                    ("time", "Time", 8)]
AUDIOBOOK_COLUMNS = [("title", "Title", 36), ("artist", "Author", 22), ("album", "Series", 22),
                     ("added", "Added", 12), ("time", "Length", 8)]
# Column key → sort_tracks() field name.
COLUMN_SORT_FIELDS = {"title": "Title", "artist": "Artist", "album": "Album",
                      "added": "Date Added", "time": "Time", "in": "In"}

# Per-track sort keys that don't need anything beyond the row itself. "In"
# isn't here — it needs the membership-count dict, not just the row, so
# sort_tracks() handles it as a special case instead.
_SORT_KEY_FUNCS = {
    "Title": lambda t: (t["title"] or Path(t["library_path"]).stem).lower(),
    "Artist": lambda t: (t["artist"] or "").lower(),
    "Album": lambda t: (t["album"] or "").lower(),
    "Date Added": lambda t: t["added_at"] or "",
    "Time": lambda t: t["duration_seconds"] or 0,
}


def sort_tracks(tracks, membership: dict, field: str, ascending: bool):
    if field == "In":
        key = lambda t: membership.get(t["id"], 0)
    else:
        key = _SORT_KEY_FUNCS.get(field, _SORT_KEY_FUNCS["Title"])
    return sorted(tracks, key=key, reverse=not ascending)


def _format_duration(seconds) -> str:
    if not seconds:
        return "—"
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def _format_length(seconds) -> str:
    """Table "Time"/"Length" cell: M:SS for songs, "18h 26m" once a file
    runs an hour or more (audiobooks), which M:SS makes unreadable."""
    if not seconds:
        return ""
    total = int(seconds)
    if total >= 3600:
        return f"{total // 3600}h {(total % 3600) // 60:02d}m"
    return _format_duration(total)


def _parse_book_number(tracknumber) -> int | None:
    """Parses a Track # tag value ("2", "2/6", "", None) into a book's
    position within its series, the same way naming.music_destination
    already parses tracknumber for its own filename-prefix purposes.
    Returns None when unparseable so the caller can fall back to title
    sort instead of a SQL text-sort (which would put "10" before "2")."""
    if not tracknumber:
        return None
    try:
        return int(str(tracknumber).split("/")[0].strip())
    except ValueError:
        return None


def sort_series_books(tracks: list) -> list:
    """Sorts audiobook rows within a series by parsed book number, falling
    back to title for anything unparseable (pushed to the end, in title
    order, rather than interleaved by parse-failure)."""
    def key(t):
        n = _parse_book_number(t["tracknumber"])
        return (0, n) if n is not None else (1, (t["title"] or "").lower())
    return sorted(tracks, key=key)


def _format_filesize(path: Path) -> str:
    try:
        size_mb = path.stat().st_size / (1024 * 1024)
    except OSError:
        return "—"
    return f"{size_mb:.1f} MB"


def _format_total_playtime(seconds: float) -> str:
    """Formats an aggregate duration (e.g. the whole library's playtime) as
    the coarsest two units that fit — d/h for a large library, h/m or just m
    for a small one. Distinct from _format_duration, which is M:SS for one
    single track."""
    total_minutes = int(seconds // 60)
    hours, minutes = divmod(total_minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _format_added_date(added_at: str | None) -> str:
    if not added_at:
        return "—"
    try:
        return datetime.fromisoformat(added_at).strftime("%b %d, %Y")
    except ValueError:
        return "—"
