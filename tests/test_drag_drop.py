"""Dragging library rows out as files — into Merge and Tags — copy only."""

from pathlib import Path

from PyQt6.QtCore import QPointF, Qt
from PyQt6.QtGui import QDragEnterEvent, QDropEvent


def _drag_two_songs(window):
    songs = window.page_library.songs_list
    songs.item(0).setSelected(True)
    songs.item(1).setSelected(True)
    return songs.mimeData(songs.selectedItems())


def test_drag_carries_real_files(window):
    data = _drag_two_songs(window)
    paths = [Path(u.toLocalFile()) for u in data.urls()]
    assert len(paths) == 2 and all(p.exists() for p in paths)


def test_drag_is_copy_only(window):
    songs = window.page_library.songs_list
    assert songs.defaultDropAction() == Qt.DropAction.CopyAction


def _drop(target, data):
    center = target.rect().center()
    target.dragEnterEvent(QDragEnterEvent(center, Qt.DropAction.CopyAction, data,
                                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    target.dropEvent(QDropEvent(QPointF(center), Qt.DropAction.CopyAction, data,
                                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))


def test_drop_songs_onto_merge(window):
    _drop(window.merge_list, _drag_two_songs(window))
    assert window.merge_list.count() == 2


def test_drop_songs_onto_tags_batch(window):
    _drop(window.batch_tag_list, _drag_two_songs(window))
    assert window.batch_tag_list.count() == 2
