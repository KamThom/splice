"""Duplicate review: one-after-another resolving, and auto-resolve."""

from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QPushButton

import splice.dialogs.duplicates as duplicates
import splice.views.library as library_view
from splice.library import db as libdb


def _row(i, title, artist, album, duration, rel):
    return {"id": i, "title": title, "artist": artist, "album": album,
            "duration_seconds": duration, "library_path": rel}


def _file(root: Path, rel: str, size: int) -> str:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return rel


def test_plan_auto_resolve_rules(tmp_path):
    r = tmp_path
    pairs = [
        # same song, different case/spacing: keep the bigger file
        (1, _row(1, "Song One", "Band", "LP", 200, _file(r, "a.mp3", 100)),
            _row(2, "song one ", "BAND", "LP", 201, _file(r, "b.mp3", 300))),
        # different album: only resolved when the album isn't required to match
        (2, _row(3, "Two", "Band", "LP", 180, _file(r, "c.mp3", 500)),
            _row(4, "Two", "Band", "Single", 180, _file(r, "d.mp3", 200))),
        # different titles: never auto-resolved
        (3, _row(5, "Three", "Band", "LP", 180, _file(r, "e.mp3", 100)),
            _row(6, "Three (Live)", "Band", "LP", 180, _file(r, "f.mp3", 900))),
        # same title but 30s longer: probably a different version
        (4, _row(7, "Four", "Band", "LP", 180, _file(r, "g.mp3", 100)),
            _row(8, "Four", "Band", "LP", 210, _file(r, "h.mp3", 900))),
        # missing file: left for Health / manual review
        (5, _row(9, "Five", "Band", "LP", 100, "missing.mp3"),
            _row(10, "Five", "Band", "LP", 100, _file(r, "i.mp3", 50))),
    ]
    plan, left = duplicates.plan_auto_resolve(str(r), pairs, require_album=False)
    assert [(e["keep"]["id"], e["remove"]["id"]) for e in plan] == [(2, 1), (3, 4)]
    assert left == 3
    plan, left = duplicates.plan_auto_resolve(str(r), pairs, require_album=True)
    assert [(e["keep"]["id"], e["remove"]["id"]) for e in plan] == [(2, 1)]


def test_plan_auto_resolve_chain_keeps_largest(tmp_path):
    r = tmp_path
    a = _row(1, "Five", "Band", "LP", 100, _file(r, "1.mp3", 100))
    b = _row(2, "Five", "Band", "LP", 100, _file(r, "2.mp3", 200))
    c = _row(3, "Five", "Band", "LP", 100, _file(r, "3.mp3", 300))
    plan, _ = duplicates.plan_auto_resolve(str(r), [(1, a, b), (2, b, c)], require_album=False)
    assert [e["remove"]["id"] for e in plan] == [1, 2]


def _dupe_library(library):
    conn = libdb.open_db(str(library.root))
    libdb.add_fuzzy_dupe(conn, library.ids["one"], library.ids["two"], "test")
    libdb.add_fuzzy_dupe(conn, library.ids["saga1"], library.ids["saga2"], "test")
    libdb.add_fuzzy_dupe(conn, library.ids["duo1"], library.ids["duo2"], "test")
    conn.close()


def test_resolve_steps_through_every_pair(qtbot, library, answer_dialogs, monkeypatch):
    _dupe_library(library)
    view = library_view.LibraryView(lambda: str(library.root), lambda r: None)
    view.refresh()
    assert view.duplicates_list.count() == 3
    script = iter(["Keep this one", "Skip", "Not a duplicate"])
    titles = []
    original_exec = duplicates.ResolveDuplicateDialog.exec

    def fake_exec(dialog):
        titles.append(dialog.windowTitle())
        label = next(script)
        button = next(b for b in dialog.findChildren(QPushButton) if b.text() == label)
        QTimer.singleShot(0, button.click)
        return original_exec(dialog)

    monkeypatch.setattr(duplicates.ResolveDuplicateDialog, "exec", fake_exec)
    view._resolve_selected_dupe()
    assert titles == [f"Resolve Duplicate ({i} of 3)" for i in (1, 2, 3)]
    assert view.duplicates_list.count() == 1  # only the skipped pair is left


def test_resolve_dialog_remembers_size(qtbot):
    a = {"id": 1, "title": "Song", "artist": "A", "album": "X", "duration_seconds": 200, "library_path": "a.mp3"}
    b = dict(a, id=2, library_path="b.mp3")
    first = duplicates.ResolveDuplicateDialog(None, "/tmp", a, b)
    first.resize(700, 400)
    first.done(0)
    second = duplicates.ResolveDuplicateDialog(None, "/tmp", a, b)
    assert (second.width(), second.height()) == (700, 400)
