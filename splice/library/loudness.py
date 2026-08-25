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


def measure_loudness(path: str) -> tuple[float, float] | None:
    """Single-pass ffmpeg loudnorm analysis — input_i/input_tp are
    properties of the source and get measured regardless of what
    I=/TP=/LRA= targets are set, so no two-pass re-encode is needed just to
    measure. Returns (integrated_lufs, true_peak_dbtp), or None on any
    failure (corrupt/missing/unsupported file, timeout)."""
    try:
        result = subprocess.run(
            [FFMPEG, "-i", path, "-af", "loudnorm=print_format=json", "-f", "null", "-"],
            capture_output=True, text=True, timeout=600, encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", result.stderr, re.DOTALL)
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


def apply_gain_and_copy(src: Path, dest: Path, gain_db: float):
    """Copies src to dest, applying gain_db via an ffmpeg re-encode of the
    COPY ONLY — src (the library master) is never touched. Falls back to a
    plain shutil.copy2 if the gain is negligible (<0.5dB, nothing audibly
    worth an extra re-encode) or if ffmpeg fails for any reason, so this can
    never simply drop a file from an export."""
    if abs(gain_db) < 0.5:
        shutil.copy2(src, dest)
        return
    try:
        result = subprocess.run(
            [FFMPEG, "-y", "-i", str(src), "-af", f"volume={gain_db}dB", "-q:a", "2", str(dest)],
            capture_output=True, timeout=600,
        )
        if result.returncode != 0 or not dest.exists():
            shutil.copy2(src, dest)
    except (OSError, subprocess.TimeoutExpired):
        shutil.copy2(src, dest)


class LoudnessAnalysisWorker(QThread):
    """Analyzes every track not yet measured (libdb.tracks_missing_loudness)
    — the same sweep serves both "catch up the existing library" and "just
    the handful of tracks a build/download just added", since it's simply
    "measure whatever's missing" either way. One bad file logs and moves on
    rather than aborting the whole batch, same convention every other
    library worker in this app already follows."""

    log = pyqtSignal(str)
    progress = pyqtSignal(int, int)  # (tracks processed so far, total pending)
    finished_ok = pyqtSignal(int)  # count analyzed
    cancelled = pyqtSignal()

    def __init__(self, library_root: str):
        super().__init__()
        self.library_root = library_root
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True

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
                result = measure_loudness(abs_path)
                if result is None:
                    self.log.emit(f"[{ts()}] Could not measure loudness: {row['library_path']}")
                    continue
                lufs, true_peak = result
                libdb.set_track_loudness(conn, row["id"], lufs, true_peak)
                analyzed += 1
        finally:
            conn.close()
        self.finished_ok.emit(analyzed)
