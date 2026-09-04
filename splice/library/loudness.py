"""Loudness measurement and gain computation. Hybrid, non-destructive design:
the library's own copies (Library/Music, Library/Audiobooks) are never
touched — each track is measured once and the raw measurement (LUFS, true
peak) is stored, never a baked-in gain. The actual gain is computed on
demand from the current target constants and applied at the two places
audio leaves the library: in-app playback (TrackPlayer, volume scaling) and
Export (an ffmpeg re-encode of the exported copy only)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from ..workers import FFMPEG, ts
from . import db as libdb

TARGET_LUFS = {"music": -14.0, "audiobook": -18.0}
TRUE_PEAK_CEILING_DBTP = -1.0  # headroom so a boosted track never clips


def measure_loudness(path: str, register_process=None) -> tuple[float, float] | None:
    """Single-pass ffmpeg loudnorm analysis — input_i/input_tp are
    properties of the source and get measured regardless of what
    I=/TP=/LRA= targets are set, so no two-pass re-encode is needed just to
    measure. Returns (integrated_lufs, true_peak_dbtp), or None on any
    failure (corrupt/missing/unsupported file, timeout).

    Uses Popen instead of subprocess.run so the caller can get a handle to
    the live ffmpeg process via register_process(process) and kill it on
    demand (e.g. LoudnessAnalysisWorker.cancel(), or the app closing mid-
    scan) — a large file can take minutes to measure, and without a way to
    actually terminate it, cancelling only ever stopped the QThread loop
    between files, never the ffmpeg subprocess already in flight. That's
    exactly how an ffmpeg.exe can outlive the app entirely: Windows doesn't
    kill child processes when their parent exits, so an unmanaged one just
    keeps running (and keeps the file it's reading locked) indefinitely."""
    process = None
    try:
        process = subprocess.Popen(
            [FFMPEG, "-i", path, "-af", "loudnorm=print_format=json", "-f", "null", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        )
        if register_process:
            register_process(process)
        _, stderr = process.communicate(timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        if process is not None:
            process.kill()
            process.communicate()
        return None
    finally:
        if register_process:
            register_process(None)
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.DOTALL)
    if not match:
        return None
    try:
        stats = json.loads(match.group(0))
        return float(stats["input_i"]), float(stats["input_tp"])
    except (json.JSONDecodeError, KeyError, ValueError):
        return None


def compute_gain_db(loudness_lufs: float, true_peak_dbtp: float, kind: str) -> float:
    """dB adjustment to bring a track to its kind's target loudness, capped
    so a boosted track's peak never crosses TRUE_PEAK_CEILING_DBTP — cutting
    a too-loud track never risks clipping, so only positive gain needs the
    cap."""
    target = TARGET_LUFS.get(kind, TARGET_LUFS["music"])
    gain_db = target - loudness_lufs
    if gain_db > 0:
        gain_db = min(gain_db, TRUE_PEAK_CEILING_DBTP - true_peak_dbtp)
    return gain_db


def gain_for_track(conn, library_path: str) -> float:
    """0.0 (no adjustment) if the track was never measured or its row is
    gone — export/copy always falls back to an unmodified file in that
    case, never blocks on missing data."""
    row = conn.execute(
        "SELECT loudness_lufs, loudness_true_peak, kind FROM tracks WHERE library_path = ?",
        (library_path,),
    ).fetchone()
    if not row or row["loudness_lufs"] is None or row["loudness_true_peak"] is None:
        return 0.0
    return compute_gain_db(row["loudness_lufs"], row["loudness_true_peak"], row["kind"])


def apply_gain_and_copy(src: Path, dest: Path, gain_db: float, register_process=None):
    """Copies src to dest, applying gain_db via an ffmpeg re-encode of the
    COPY ONLY — src (the library master) is never touched. Falls back to a
    plain shutil.copy2 if the gain is negligible (<0.5dB, nothing audibly
    worth an extra re-encode) or if ffmpeg fails for any reason, so this can
    never simply drop a file from an export. Popen + register_process for
    the same reason as measure_loudness — lets a cancel (or the app
    closing) actually kill the ffmpeg subprocess instead of leaving it to
    finish (or hang) unsupervised."""
    if abs(gain_db) < 0.5:
        shutil.copy2(src, dest)
        return
    process = None
    try:
        process = subprocess.Popen(
            [FFMPEG, "-y", "-i", str(src), "-af", f"volume={gain_db}dB", "-q:a", "2", str(dest)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if register_process:
            register_process(process)
        process.communicate(timeout=600)
        if process.returncode != 0 or not dest.exists():
            shutil.copy2(src, dest)
    except (OSError, subprocess.TimeoutExpired):
        if process is not None:
            process.kill()
            process.communicate()
        shutil.copy2(src, dest)
    finally:
        if register_process:
            register_process(None)


class LoudnessAnalysisWorker(QThread):
    """Analyzes every music track not yet measured (libdb.tracks_missing_loudness
    — scoped to kind='music' only, audiobooks are excluded from this sweep
    entirely) — the same sweep serves both "catch up the existing library"
    and "just the handful of tracks a build/download just added", since
    it's simply "measure whatever's missing" either way. One bad file logs
    and moves on rather than aborting the whole batch, same convention
    every other library worker in this app already follows."""

    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)  # (tracks processed so far, total pending)
    finished_ok = pyqtSignal(int)  # count analyzed
    cancelled = pyqtSignal()

    def __init__(self, library_root: str):
        super().__init__()
        self.library_root = library_root
        self._cancel_requested = False
        self._process = None

    def cancel(self):
        """Stops the loop between files AND kills whatever ffmpeg process is
        currently in flight — without the latter, a large file mid-measurement
        (this can take minutes) would just keep running as an orphan after
        the worker "cancels", including when the whole app is closing."""
        self._cancel_requested = True
        if self._process is not None and self._process.poll() is None:
            self._process.kill()

    def _set_process(self, process):
        self._process = process

    def run(self):
        conn = libdb.open_db(self.library_root)
        try:
            rows = libdb.tracks_missing_loudness(conn)
            total = len(rows)
            analyzed = 0
            for i, row in enumerate(rows, start=1):
                if self._cancel_requested:
                    self.cancelled.emit()
                    return
                self.progress.emit(i, total)
                abs_path = str(Path(self.library_root) / row["library_path"])
                result = measure_loudness(abs_path, register_process=self._set_process)
                if result is None:
                    self.log.emit(f"[{ts()}] Could not measure loudness: {row['library_path']}")
                    continue
                lufs, true_peak = result
                libdb.set_track_loudness(conn, row["id"], lufs, true_peak)
                analyzed += 1
        finally:
            conn.close()
        self.finished_ok.emit(analyzed)
