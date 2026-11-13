"""Shared fixtures. Every test runs headless (offscreen Qt) against a small
throwaway library built in a temp folder, with Splice's settings redirected
to a temp folder too — your real library and settings are never touched."""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace

# Must be set before Qt or QSettings are first used.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp(prefix="splice-test-config-")

import pytest  # noqa: E402
from mutagen.easyid3 import EasyID3  # noqa: E402
from mutagen.id3 import ID3NoHeaderError  # noqa: E402

HAVE_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture(scope="session")
def silent_mp3(tmp_path_factory) -> Path:
    """One real (silent, 1 second) mp3 that the library fixtures copy and
    tag — real files so tag reading, duration probing and playback code
    paths behave as they would on actual music."""
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg is needed to build test audio")
    out = tmp_path_factory.mktemp("audio") / "silence.mp3"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=22050:cl=mono",
         "-t", "1", "-q:a", "9", str(out)],
        check=True,
    )
    return out


def _tag(path: Path, title: str, artist: str, album: str, track: str = ""):
    try:
        tags = EasyID3(str(path))
    except ID3NoHeaderError:
        tags = EasyID3()
    tags["title"] = title
    tags["artist"] = artist
    if album:
        tags["album"] = album
    if track:
        tags["tracknumber"] = track
    tags.save(str(path))


@pytest.fixture
def library(tmp_path, silent_mp3):
    """A small library: 3 songs (one alone in its own artist folder), a
    2-book series with artwork, a second 2-book series, one standalone
    audiobook, and a playlist "Mix" holding two of the songs."""
    from splice.library import db as libdb
    from splice.library.playlists import write_m3u8
    from splice.library.series_artwork import set_series_artwork

    root = tmp_path / "Library"
    root.mkdir()
    conn = libdb.open_db(str(root))
    ids = {}

    def add(key, rel, kind, title, artist, album, track="", size_pad=0):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(silent_mp3, path)
        _tag(path, title, artist, album, track)
        if size_pad:
            with open(path, "ab") as f:
                f.write(b"\0" * size_pad)
        ids[key] = libdb.insert_track(
            conn, file_hash=f"hash-{key}", library_path=rel, kind=kind, title=title, artist=artist,
            album=album, date="", tracknumber=track, duration_seconds=1.0,
        )

    add("one", "Music/Band/LP/01 One.mp3", "music", "One", "Band", "LP")
    add("two", "Music/Band/LP/02 Two.mp3", "music", "Two", "Band", "LP")
    add("three", "Music/Solo/Single/Three.mp3", "music", "Three", "Solo", "Single")
    add("saga1", "Audiobooks/Saga/1_First.mp3", "audiobook", "First", "Author", "Saga", "1")
    add("saga2", "Audiobooks/Saga/2_Second.mp3", "audiobook", "Second", "Author", "Saga", "2")
    add("duo1", "Audiobooks/Duo/1_C.mp3", "audiobook", "C", "Author", "Duo", "1")
    add("duo2", "Audiobooks/Duo/2_D.mp3", "audiobook", "D", "Author", "Duo", "2")
    add("lonely", "Audiobooks/Lonely.mp3", "audiobook", "Lonely", "Author", "")

    playlist_id = libdb.get_or_create_playlist(conn, "Mix")
    libdb.add_playlist_track(conn, playlist_id, ids["three"], 1)
    libdb.add_playlist_track(conn, playlist_id, ids["one"], 2)
    write_m3u8(str(root / "Playlists"), "Mix",
               libdb.playlist_track_paths_in_order(conn, playlist_id), str(root))
    conn.close()

    art = tmp_path / "art.png"
    _write_png(art)
    set_series_artwork(str(root), "Saga", str(art))
    return SimpleNamespace(root=root, ids=ids, playlist_id=playlist_id)


def _write_png(path: Path):
    from PyQt6.QtGui import QColor, QImage
    img = QImage(8, 8, QImage.Format.Format_RGB32)
    img.fill(QColor("#FF6B5B"))
    img.save(str(path), "PNG")


@pytest.fixture
def answer_dialogs(monkeypatch):
    """Answers every Yes/No question with whatever answer["value"] holds
    (default Yes) and swallows information/warning popups, recording them."""
    from PyQt6.QtWidgets import QMessageBox
    state = {"value": QMessageBox.StandardButton.Yes, "messages": []}
    monkeypatch.setattr(QMessageBox, "question", staticmethod(lambda *a, **k: state["value"]))
    monkeypatch.setattr(QMessageBox, "information",
                        staticmethod(lambda *a, **k: state["messages"].append(a[1:3])))
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: state["messages"].append(a[1:3])))
    return state


@pytest.fixture
def window(qtbot, qapp, library, tmp_path, monkeypatch, answer_dialogs):
    """A full MainWindow pointed at the throwaway library, styled like the
    real app. Background loudness/art scans are switched off — they'd shell
    out to ffmpeg for every file and outlive the test."""
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QFileDialog

    from splice import main_window as mw
    from splice.styles import STYLESHEET, build_palette, load_bundled_fonts

    qapp.setStyle("Fusion")
    load_bundled_fonts()
    qapp.setPalette(build_palette())
    qapp.setStyleSheet(STYLESHEET)

    settings = QSettings("Thomas", "Splice")
    settings.clear()
    settings.setValue("library_root", str(library.root))
    settings.sync()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", staticmethod(lambda *a, **k: str(out_dir)))
    monkeypatch.setattr(mw.MainWindow, "_kick_off_loudness_analysis", lambda self: None)
    monkeypatch.setattr(mw.MainWindow, "_kick_off_health_analysis", lambda self: None)

    w = mw.MainWindow()
    qtbot.addWidget(w)
    w.resize(1400, 860)
    w.show()
    qtbot.wait(450)  # let the startup timers (output folder, queue restore) run
    return w
