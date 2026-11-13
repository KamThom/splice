"""Deleting from the library: songs, whole series, single books, cancel."""


from PyQt6.QtWidgets import QMessageBox

import splice.library_actions as actions
import splice.views.library as library_view
import splice.views.series as series_view
from splice.library import db as libdb
from splice.library.series_artwork import find_series_artwork


def _view(library):
    view = library_view.LibraryView(lambda: str(library.root), lambda r: None)
    view.refresh()
    return view


def _select_title(list_widget, title):
    list_widget.clearSelection()
    for i in range(list_widget.count()):
        if list_widget.item(i).data(256)["title"] == title:
            list_widget.item(i).setSelected(True)


def test_cancel_deletes_nothing(qtbot, library, answer_dialogs):
    view = _view(library)
    answer_dialogs["value"] = QMessageBox.StandardButton.No
    _select_title(view.songs_list, "One")
    view._remove_selected_tracks()
    assert view.songs_list.count() == 3
    assert (library.root / "Music/Band/LP/01 One.mp3").exists()


def test_delete_song_updates_playlist_and_prunes_folders(qtbot, library, answer_dialogs):
    view = _view(library)
    _select_title(view.songs_list, "Three")
    assert view.copy_actions.count_label.text() == "1 selected"
    view._remove_selected_tracks()
    assert not (library.root / "Music/Solo").exists()  # emptied artist/album folders removed
    assert (library.root / "Music").exists()  # top-level folder kept
    m3u = (library.root / "Playlists/Mix.m3u8").read_text()
    assert "Three" not in m3u and "One" in m3u
    assert view.songs_list.count() == 2


def test_delete_series_removes_books_and_artwork(qtbot, library, answer_dialogs):
    view = _view(library)
    view._delete_series("Saga")
    assert not (library.root / "Audiobooks/Saga").exists()
    assert (library.root / "Audiobooks").exists()
    assert find_series_artwork(str(library.root), "Saga") is None
    conn = libdb.open_db(str(library.root))
    assert not libdb.list_tracks_in_series(conn, "Saga")
    conn.close()
    assert view._counts["audiobooks"] == 3


def test_delete_standalone_with_absolute_path(qtbot, library, answer_dialogs):
    path = library.root / "Audiobooks/Lonely.mp3"
    n = actions.delete_library_tracks(None, str(library.root), [(library.ids["lonely"], str(path))], noun="audiobook")
    assert n == 1 and not path.exists()


def test_series_page_delete_last_book_goes_back(qtbot, library, answer_dialogs):
    page = series_view.AudiobookSeriesView(lambda: str(library.root))
    back, changed = [], []
    page.backRequested.connect(lambda: back.append(True))
    page.libraryChanged.connect(lambda: changed.append(True))
    page.show_series("Duo")
    page.track_list.item(0).setSelected(True)
    page._remove_selected()
    assert page.track_list.count() == 1 and not back and changed
    page.track_list.item(0).setSelected(True)
    page._remove_selected()
    assert back
    assert not (library.root / "Audiobooks/Duo").exists()


def test_delete_goes_to_trash(qtbot, library, answer_dialogs, fake_trash):
    path = library.root / "Music/Band/LP/01 One.mp3"
    n = actions.delete_library_tracks(None, str(library.root), [(library.ids["one"], "Music/Band/LP/01 One.mp3")])
    assert n == 1 and not path.exists()
    assert [p.name for p in fake_trash.iterdir()] == ["0-01 One.mp3"]


def _no_trash(monkeypatch):
    def fail(path):
        raise OSError("no trash on this drive")
    monkeypatch.setattr(actions, "send2trash", fail)


def test_no_trash_and_user_declines_keeps_file(qtbot, library, answer_dialogs, monkeypatch):
    _no_trash(monkeypatch)
    asked = []
    yes, no = QMessageBox.StandardButton.Yes, QMessageBox.StandardButton.No
    # first question: "Delete from library?" -> Yes; second: "delete permanently?" -> No
    answers = iter([yes, no])
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: (asked.append(a[1]), next(answers))[1]))
    path = library.root / "Music/Band/LP/01 One.mp3"
    n = actions.delete_library_tracks(None, str(library.root), [(library.ids["one"], "Music/Band/LP/01 One.mp3")])
    assert n == 0 and path.exists()
    assert asked == ["Delete from library", "Can't use the Trash"]
    assert answer_dialogs["messages"] and "left in your library" in answer_dialogs["messages"][0][0]
    conn = libdb.open_db(str(library.root))
    assert conn.execute("SELECT 1 FROM tracks WHERE id = ?", (library.ids["one"],)).fetchone()
    conn.close()


def test_no_trash_and_user_accepts_deletes_permanently(qtbot, library, answer_dialogs, monkeypatch):
    _no_trash(monkeypatch)
    tracks = [(library.ids["one"], "Music/Band/LP/01 One.mp3"), (library.ids["two"], "Music/Band/LP/02 Two.mp3")]
    n = actions.delete_library_tracks(None, str(library.root), tracks)
    assert n == 2 and not (library.root / "Music/Band").exists()


def test_locked_file_is_reported_not_deleted(qtbot, library, answer_dialogs, monkeypatch):
    _no_trash(monkeypatch)
    monkeypatch.setattr(actions, "_try_unlink", lambda path: "in use")
    n = actions.delete_library_tracks(None, str(library.root), [(library.ids["one"], "Music/Band/LP/01 One.mp3")])
    assert n == 0
    assert "left in your library" in answer_dialogs["messages"][0][0]
    conn = libdb.open_db(str(library.root))
    assert conn.execute("SELECT 1 FROM tracks WHERE id = ?", (library.ids["one"],)).fetchone()
    conn.close()


def test_permanent_question_asked_once_per_operation(qtbot, tmp_path, monkeypatch):
    _no_trash(monkeypatch)
    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: (asked.append(1), QMessageBox.StandardButton.Yes)[1]))
    files = []
    for i in range(3):
        f = tmp_path / f"{i}.mp3"
        f.write_bytes(b"x")
        files.append(f)
    decision = {}
    for f in files:  # one file per call, like auto-resolve does
        assert actions.remove_files(None, [f], decision)[f] is None
    assert len(asked) == 1 and not any(f.exists() for f in files)
