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


def _drop_on_playlist(window, data):
    plist = window.sidebar.playlist_deck.playlist_list
    pos = plist.visualItemRect(plist.item(0)).center()
    plist.dragEnterEvent(QDragEnterEvent(pos, Qt.DropAction.CopyAction, data,
                                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))
    plist.dropEvent(QDropEvent(QPointF(pos), Qt.DropAction.CopyAction, data,
                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier))


def _playlist_titles(window, library):
    from splice.library import db as libdb
    conn = libdb.open_db(str(library.root))
    paths = libdb.playlist_track_paths_in_order(conn, library.playlist_id)
    conn.close()
    return [Path(p).stem for p in paths]


def test_drop_songs_onto_sidebar_playlist(window, library):
    songs = window.page_library.songs_list
    for i in range(songs.count()):  # "Two" is the only song not already on Mix
        songs.item(i).setSelected(True)
    _drop_on_playlist(window, songs.mimeData(songs.selectedItems()))
    assert _playlist_titles(window, library) == ["Three", "01 One", "02 Two"]  # appended, no duplicates
    m3u = (library.root / "Playlists/Mix.m3u8").read_text()
    assert "02 Two.mp3" in m3u
    assert "Added 1 song to Mix" in window.console_strip.text()


def test_drop_audiobooks_onto_playlist_is_refused(window, library):
    lib = window.page_library
    books = lib.audiobooks_list
    books.item(0).setSelected(True)
    _drop_on_playlist(window, books.mimeData(books.selectedItems()))
    assert _playlist_titles(window, library) == ["Three", "01 One"]
    assert "Audiobooks weren't added" in window.console_strip.text()
