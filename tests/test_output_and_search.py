"""Remembered output folder, the paused YouTube queue, and the light search path."""

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QFileDialog, QListWidgetItem

from splice import main_window as mw


def _window(qtbot, library, monkeypatch, output_dir, picker_returns=""):
    settings = QSettings("Thomas", "Splice")
    settings.clear()
    settings.setValue("library_root", str(library.root))
    if output_dir is not None:
        settings.setValue("output_dir", str(output_dir))
    settings.sync()
    asked = []
    monkeypatch.setattr(QFileDialog, "getExistingDirectory",
                        staticmethod(lambda *a, **k: (asked.append(1), picker_returns)[1]))
    monkeypatch.setattr(mw.MainWindow, "_kick_off_loudness_analysis", lambda self: None)
    monkeypatch.setattr(mw.MainWindow, "_kick_off_health_analysis", lambda self: None)
    w = mw.MainWindow()
    qtbot.addWidget(w)
    return w, asked


def test_output_folder_remembered_across_launches(qtbot, library, tmp_path, monkeypatch, answer_dialogs):
    out = tmp_path / "out"
    out.mkdir()
    w, asked = _window(qtbot, library, monkeypatch, out)
    assert w.output_dir == str(out)
    assert w._ensure_output_dir() and not asked


def test_missing_output_folder_asks_again(qtbot, library, tmp_path, monkeypatch, answer_dialogs):
    new = tmp_path / "new"
    new.mkdir()
    w, asked = _window(qtbot, library, monkeypatch, tmp_path / "unplugged-drive", picker_returns=str(new))
    assert w.output_dir is None
    assert w._ensure_output_dir() and asked == [1] and w.output_dir == str(new)
    assert QSettings("Thomas", "Splice").value("output_dir") == str(new)


def test_queue_pauses_without_a_folder(qtbot, library, monkeypatch, answer_dialogs):
    w, asked = _window(qtbot, library, monkeypatch, None, picker_returns="")
    item = QListWidgetItem("Some video")
    item.setData(256, {"url": "https://example.invalid/v", "title": "Some video", "quality": "192",
                       "playlist_id": None, "kind": "music"})
    w.youtube_queue_list.addItem(item)
    w._pump_youtube_queue()
    assert w.youtube_active_worker is None  # nothing started downloading to nowhere
    assert w.youtube_queue_list.count() == 1
    assert "paused" in w.youtube_now_label.text().lower()
    w.youtube_queue_list.clear()


def test_search_only_refilters(window):
    lib = window.page_library
    changed = []
    lib.libraryChanged.connect(lambda: changed.append(1))
    lib.search_edit.setText("solo")
    lib._apply_filter()
    assert lib.songs_list.count() == 1
    assert not changed  # sidebar/playlists aren't rebuilt while typing
    assert window.page_subtitle.text() == "1 of 3 songs match"
    lib.search_edit.setText("")
    lib._apply_filter()
    assert lib.songs_list.count() == 3 and window.page_subtitle.text().startswith("3 songs")


def test_column_sort_does_not_reload(window):
    lib = window.page_library
    changed = []
    lib.libraryChanged.connect(lambda: changed.append(1))
    lib.songs_header.sortRequested.emit("artist")
    assert not changed and lib.songs_list.count() == 3
