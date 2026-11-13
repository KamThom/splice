"""Small pure helpers behind the tables."""

from PyQt6.QtCore import QRect

import splice.track_lists as track_lists
from splice.widgets import TrackRowDelegate


def test_format_length():
    assert track_lists._format_length(0) == ""
    assert track_lists._format_length(225) == "3:45"
    assert track_lists._format_length(66388) == "18h 26m"


def test_sort_tracks_by_time_and_membership():
    tracks = [{"id": 1, "title": "b", "duration_seconds": 90, "library_path": "b.mp3"},
              {"id": 2, "title": "a", "duration_seconds": 300, "library_path": "a.mp3"}]
    assert [t["id"] for t in track_lists.sort_tracks(tracks, {}, "Time", ascending=False)] == [2, 1]
    assert [t["id"] for t in track_lists.sort_tracks(tracks, {1: 3}, "In", ascending=False)] == [1, 2]
    assert [t["id"] for t in track_lists.sort_tracks(tracks, {}, "Title", ascending=True)] == [2, 1]


def test_column_rects_fill_row_in_order():
    row = QRect(0, 0, 1000, 34)
    rects = TrackRowDelegate.column_rects(row, track_lists.SONG_COLUMNS)
    assert len(rects) == len(track_lists.SONG_COLUMNS)
    assert all(a.right() < b.left() for a, b in zip(rects, rects[1:]))
    assert rects[-1].right() <= row.right() - TrackRowDelegate.RIGHT_ZONE
