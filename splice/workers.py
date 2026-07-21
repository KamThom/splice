"""Background QThread workers that shell out to ffmpeg / yt-dlp."""

import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

FFMPEG = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or shutil.which("ffprobe.exe") or "ffprobe"
FFMPEG_INSTALL_HINT = (
    "ffmpeg was not found on your PATH.\n\n"
    "macOS:   brew install ffmpeg\n"
    "Windows: winget install ffmpeg  (or choco install ffmpeg / scoop install ffmpeg)\n\n"
    "Then restart this app."
)


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    i = 1
    while True:
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def escape_concat_path(path: str) -> str:
    return path.replace("'", "'\\''")


INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|]')


def sanitize_filename(name: str) -> str:
    name = INVALID_FILENAME_CHARS.sub("", name).strip()
    return name or "merged"


def ts() -> str:
    return time.strftime("%H:%M:%S")


def probe_duration_seconds(path: str) -> float | None:
    """Returns a file's duration via ffprobe, or None if it can't be determined
    (ffprobe missing, unreadable file, etc.) — callers fall back to an
    indeterminate progress indicator in that case."""
    try:
        result = subprocess.run(
            [FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=15, encoding="utf-8", errors="replace",
        )
        return float(result.stdout.strip())
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return None


def _stream_ffmpeg_progress(process: subprocess.Popen, total_seconds: float | None, progress_signal):
    """Reads an ffmpeg `-progress pipe:1` stream and emits 0-100 on progress_signal.
    Always drains stdout (even when total_seconds is unknown) so ffmpeg never
    blocks on a full pipe buffer."""
    for line in process.stdout:
        if not total_seconds:
            continue
        line = line.strip()
        if not line.startswith("out_time="):
            continue
        try:
            h, m, s = line.split("=", 1)[1].split(":")
            elapsed = int(h) * 3600 + int(m) * 60 + float(s)
        except ValueError:
            continue
        pct = max(0, min(100, int(elapsed / total_seconds * 100)))
        progress_signal.emit(pct)


class MergeWorker(QThread):
    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished_ok = pyqtSignal(str)
    finished_err = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, files: list[str], output_dir: str, output_name: str | None = None):
        super().__init__()
        self.files = files
        self.output_dir = output_dir
        self.output_name = output_name
        self._process = None
        self._cancel_requested = False
        self._current_list_path = None

    def cancel(self):
        self._cancel_requested = True
        if self._process and self._process.poll() is None:
            self._process.terminate()

    def run(self):
        self._current_list_path = None
        try:
            self._run_impl()
        except Exception as e:
            # Anything unexpected (e.g. an unwritable/unrepresentable path) must
            # fail this job, not take the whole app down with it.
            if self._current_list_path:
                Path(self._current_list_path).unlink(missing_ok=True)
            self.log.emit(f"[{ts()}]   FAILED: {e}")
            self.finished_err.emit(str(e))

    def _run_impl(self):
        out_dir = Path(self.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        base_name = sanitize_filename(self.output_name) if self.output_name else f"{Path(self.files[0]).stem}_merged"
        out_path = unique_path(out_dir / f"{base_name}.mp3")

        total_seconds = self._probe_total_duration()
        if total_seconds is None:
            self.progress.emit(-1)

        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
            for path in self.files:
                f.write(f"file '{escape_concat_path(path)}'\n")
            list_path = f.name
        self._current_list_path = list_path

        self.log.emit(f"[{ts()}] Merging {len(self.files)} files -> {out_path.name}")

        cmd = [FFMPEG, "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
               "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", str(out_path)]
        returncode, stderr = self._run(cmd, total_seconds)
        if self._cancel_requested:
            self._cleanup_cancelled(out_path, list_path)
            return
        if returncode is None:
            Path(list_path).unlink(missing_ok=True)
            return

        if returncode != 0:
            self.log.emit(f"[{ts()}]   stream copy failed, retrying with re-encode...")
            cmd = [FFMPEG, "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
                   "-f", "concat", "-safe", "0", "-i", list_path,
                   "-c:a", "libmp3lame", "-b:a", "192k", str(out_path)]
            returncode, stderr = self._run(cmd, total_seconds)
            if self._cancel_requested:
                self._cleanup_cancelled(out_path, list_path)
                return
            if returncode is None:
                Path(list_path).unlink(missing_ok=True)
                return
            if returncode != 0:
                err = stderr.strip().splitlines()[-1] if stderr else "unknown error"
                self.log.emit(f"[{ts()}]   FAILED: {err}")
                self.finished_err.emit(err)
                Path(list_path).unlink(missing_ok=True)
                return

        Path(list_path).unlink(missing_ok=True)
        self.progress.emit(100)
        self.log.emit(f"[{ts()}]   Done -> {out_path}")
        self.finished_ok.emit(str(out_path))

    def _probe_total_duration(self) -> float | None:
        total = 0.0
        for f in self.files:
            d = probe_duration_seconds(f)
            if d is None:
                return None
            total += d
        return total

    def _run(self, cmd, total_seconds):
        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            self.log.emit(f"[{ts()}]   FAILED: ffmpeg not found.")
            self.finished_err.emit("ffmpeg not found")
            return None, ""
        _stream_ffmpeg_progress(self._process, total_seconds, self.progress)
        self._process.wait()
        stderr = self._process.stderr.read() if self._process.stderr else ""
        returncode = self._process.returncode
        self._process = None
        return returncode, stderr

    def _cleanup_cancelled(self, out_path: Path, list_path: str):
        Path(list_path).unlink(missing_ok=True)
        Path(out_path).unlink(missing_ok=True)
        self.log.emit(f"[{ts()}]   Cancelled — removed partial output.")
        self.cancelled.emit()


class SplitWorker(QThread):
    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    file_progress = pyqtSignal(int, int, str)
    finished_all = pyqtSignal(list)  # all segment mp3 paths created, in order
    cancelled = pyqtSignal()

    def __init__(self, files: list[str], output_dir: str, segment_seconds: int):
        super().__init__()
        self.files = files
        self.output_dir = output_dir
        self.segment_seconds = segment_seconds
        self._process = None
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True
        if self._process and self._process.poll() is None:
            self._process.terminate()

    def run(self):
        total = len(self.files)
        all_segments = []
        for i, path in enumerate(self.files, start=1):
            if self._cancel_requested:
                break
            self.file_progress.emit(i, total, Path(path).name)
            try:
                all_segments.extend(self._split_one(path))
            except Exception as e:
                # One bad file shouldn't take down the whole batch (or the app).
                self.log.emit(f"[{ts()}]   FAILED {Path(path).name}: {e}")
            if self._cancel_requested:
                break

        if self._cancel_requested:
            self.log.emit(f"[{ts()}]   Cancelled.")
            self.cancelled.emit()
        else:
            self.finished_all.emit(all_segments)

    def _split_one(self, path: str) -> list[str]:
        base = Path(path).stem
        outdir = Path(self.output_dir) / f"{base}_split"
        outdir.mkdir(parents=True, exist_ok=True)

        self.log.emit(f"[{ts()}] Splitting: {Path(path).name}")

        total_seconds = probe_duration_seconds(path)
        self.progress.emit(0 if total_seconds else -1)

        cmd = [
            FFMPEG, "-y", "-v", "error", "-progress", "pipe:1", "-nostats",
            "-i", path,
            "-f", "segment",
            "-segment_time", str(self.segment_seconds),
            "-c", "copy",
            "-reset_timestamps", "1",
            "-map", "0:a",
            str(outdir / "%d.mp3"),
        ]
        try:
            self._process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace",
            )
        except FileNotFoundError:
            self.log.emit(f"[{ts()}]   FAILED: ffmpeg not found.")
            return []

        _stream_ffmpeg_progress(self._process, total_seconds, self.progress)
        self._process.wait()
        stderr = self._process.stderr.read() if self._process.stderr else ""
        returncode = self._process.returncode
        self._process = None

        if self._cancel_requested:
            shutil.rmtree(outdir, ignore_errors=True)
            self.log.emit(f"[{ts()}]   Cancelled — removed partial segments for {Path(path).name}.")
            return []

        if returncode != 0:
            err = stderr.strip().splitlines()[-1] if stderr else "unknown error"
            self.log.emit(f"[{ts()}]   FAILED: {err}")
            return []

        self.progress.emit(100)
        segments = sorted(outdir.glob("*.mp3"), key=lambda p: int(p.stem))
        self.log.emit(f"[{ts()}]   Done: {len(segments)} segments -> {outdir}")
        return [str(p) for p in segments]


def looks_like_playlist_url(url: str) -> bool:
    return "list=" in url or "/playlist" in url


class PlaylistExpandWorker(QThread):
    """Resolves a YouTube playlist URL into its individual (video_url, title) pairs, no download."""

    expanded = pyqtSignal(list)  # list[tuple[str, str]]
    failed = pyqtSignal(str)

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            import yt_dlp
        except ImportError:
            self.failed.emit("yt-dlp not installed")
            return

        opts = {"extract_flat": "in_playlist", "quiet": True, "no_warnings": True}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=False)
        except Exception as e:
            err = str(e).strip().splitlines()[-1] if str(e).strip() else "could not read playlist"
            self.failed.emit(err)
            return

        results = []
        for entry in (info.get("entries") or [info]):
            if not entry:
                continue
            video_url = entry.get("webpage_url") or entry.get("url") or ""
            if video_url and not video_url.startswith("http"):
                video_url = f"https://www.youtube.com/watch?v={video_url}"
            if not video_url:
                continue
            title = entry.get("title") or video_url
            results.append((video_url, title))
        self.expanded.emit(results)


class TitleLookupWorker(QThread):
    """Looks up a single video's title without downloading it, for display in the queue."""

    resolved = pyqtSignal(str, str)  # url, title
    failed = pyqtSignal(str, str)  # url, error

    def __init__(self, url: str):
        super().__init__()
        self.url = url

    def run(self):
        try:
            import yt_dlp
        except ImportError:
            self.failed.emit(self.url, "yt-dlp not installed")
            return

        opts = {"quiet": True, "no_warnings": True, "skip_download": True}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=False)
            self.resolved.emit(self.url, info.get("title") or self.url)
        except Exception as e:
            err = str(e).strip().splitlines()[-1] if str(e).strip() else "could not read video info"
            self.failed.emit(self.url, err)


class YouTubeWorker(QThread):
    """Downloads a YouTube URL and extracts it to mp3 via yt-dlp + ffmpeg."""

    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished_ok = pyqtSignal(str)
    finished_err = pyqtSignal(str)
    cancelled = pyqtSignal()

    def __init__(self, url: str, output_dir: str, quality: str = "192"):
        super().__init__()
        self.url = url
        self.output_dir = output_dir
        self.quality = quality
        self._cancel_requested = False

    def cancel(self):
        self._cancel_requested = True

    def _hook(self, d):
        # Called from yt-dlp's internal thread. emit() is safe here — Qt detects
        # the emitting thread differs from the GUI thread and queues delivery.
        # Raising here is yt-dlp's documented way for a hook to abort a download
        # (the same mechanism it uses internally for --max-downloads).
        if self._cancel_requested:
            raise RuntimeError("Splice: cancelled by user")

        status = d.get("status")
        if status == "downloading":
            downloaded = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            if total:
                pct = max(0, min(100, int(downloaded / total * 100)))
                self.progress.emit(pct)
                self.log.emit(f"[{ts()}]   downloading {pct}%")
            else:
                self.progress.emit(-1)
        elif status == "finished":
            self.progress.emit(-1)  # indeterminate: ffmpeg is now converting to mp3
            self.log.emit(f"[{ts()}]   download complete, converting to mp3...")

    def _cleanup_partial(self, out_dir: Path):
        for pattern in ("*.part", "*.part-Frag*", "*.ytdl"):
            for f in out_dir.glob(pattern):
                f.unlink(missing_ok=True)

    def run(self):
        try:
            import yt_dlp
        except ImportError:
            self.log.emit(f"[{ts()}]   FAILED: yt-dlp not installed (pip install -r requirements.txt)")
            self.finished_err.emit("yt-dlp not installed")
            return

        out_dir = Path(self.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        self.log.emit(f"[{ts()}] Fetching: {self.url}")

        opts = {
            "format": "bestaudio/best",
            "outtmpl": str(out_dir / "%(title)s.%(ext)s"),
            "ffmpeg_location": FFMPEG,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": self.quality,
            }],
            "progress_hooks": [self._hook],
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(self.url, download=True)
            out_path = str(Path(ydl.prepare_filename(info)).with_suffix(".mp3"))
        except Exception as e:
            if self._cancel_requested:
                self._cleanup_partial(out_dir)
                self.log.emit(f"[{ts()}]   Cancelled by user.")
                self.cancelled.emit()
                return
            err = str(e).strip().splitlines()[-1] if str(e).strip() else "download failed"
            self.log.emit(f"[{ts()}]   FAILED: {err}")
            self.finished_err.emit(err)
            return

        if self._cancel_requested:
            Path(out_path).unlink(missing_ok=True)
            self._cleanup_partial(out_dir)
            self.log.emit(f"[{ts()}]   Cancelled by user.")
            self.cancelled.emit()
            return

        self.progress.emit(100)
        self.log.emit(f"[{ts()}]   Done -> {out_path}")
        self.finished_ok.emit(out_path)
