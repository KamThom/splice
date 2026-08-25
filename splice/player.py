"""In-app music playback — a thin QObject wrapper around QMediaPlayer/
QAudioOutput that owns queue/shuffle state and never leaks either Qt
multimedia object to callers. Music only: audiobooks are consumed on the
Shokz/iPod via Export, not played in-app."""

import random

from PyQt6.QtCore import QObject, QUrl, pyqtSignal
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer

from . import tagging
from .library.loudness import compute_gain_db


class TrackPlayer(QObject):
    positionChanged = pyqtSignal(int)       # ms
    durationChanged = pyqtSignal(int)       # ms
    playbackStateChanged = pyqtSignal(str)  # "playing" | "paused" | "stopped"
    nowPlayingChanged = pyqtSignal(object)  # dict: id/title/artist/path/art
    shuffleChanged = pyqtSignal(bool)
    playbackError = pyqtSignal(str)

    _STATE_NAMES = {
        QMediaPlayer.PlaybackState.PlayingState: "playing",
        QMediaPlayer.PlaybackState.PausedState: "paused",
        QMediaPlayer.PlaybackState.StoppedState: "stopped",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._media_player = QMediaPlayer(self)
        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(1.0)
        self._media_player.setAudioOutput(self._audio_output)

        self._queue: list[dict] = []
        self._order: list[int] = []
        self._order_pos: int = -1
        self._shuffle: bool = False
        self._had_error_for_current_load = False
        self._user_volume = 1.0
        self._track_gain_db = 0.0

        self._media_player.positionChanged.connect(self.positionChanged.emit)
        self._media_player.durationChanged.connect(self.durationChanged.emit)
        self._media_player.playbackStateChanged.connect(self._on_state_changed)
        self._media_player.mediaStatusChanged.connect(self._on_media_status_changed)
        self._media_player.errorOccurred.connect(self._on_error)

    # ---- transport ----

    def play_queue(self, tracks: list[dict], start_index: int):
        """tracks: dicts with at least id/title/artist/path (absolute path).
        Builds the queue from whatever order the caller displayed it in.
        Respects shuffle if it's already on, rather than resetting it —
        shuffle is a mode, not a one-shot."""
        if not tracks or not (0 <= start_index < len(tracks)):
            return
        self._queue = list(tracks)
        self._order = self._initial_order(start_index)
        self._order_pos = self._order.index(start_index)
        self._load_and_play(self._queue[start_index])

    def next(self):
        if self._order_pos + 1 < len(self._order):
            self._order_pos += 1
            self._load_and_play(self._queue[self._order[self._order_pos]])
        # else: end of the queue — deliberately a no-op, no loop

    def prev(self):
        if self._order_pos > 0:
            self._order_pos -= 1
            self._load_and_play(self._queue[self._order[self._order_pos]])

    def toggle_play_pause(self):
        if not self._queue:
            return
        if self._media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._media_player.pause()
        else:
            self._media_player.play()

    def seek(self, position_ms: int):
        self._media_player.setPosition(position_ms)

    def set_volume(self, volume: float):
        self._user_volume = volume
        self._apply_volume()

    def set_shuffle(self, enabled: bool):
        if enabled == self._shuffle:
            return
        self._shuffle = enabled
        if self._queue:
            # Re-anchor at whatever's actually playing right now and rebuild
            # the *future* from there — deliberately not "keep the old
            # history prefix and only reshuffle the tail" (an earlier
            # version did that, and it meant turning shuffle back off could
            # resume from wherever you were *before* you ever shuffled,
            # instead of continuing from the shuffled-to track you're
            # currently on). The current track never changes here — only
            # what next()/prev() will walk through afterward.
            current_index = self._order[self._order_pos]
            if enabled:
                remaining = [i for i in range(len(self._queue)) if i != current_index]
                random.shuffle(remaining)
            else:
                remaining = [i for i in range(len(self._queue)) if i > current_index]
            self._order = [current_index] + remaining
            self._order_pos = 0
        self.shuffleChanged.emit(enabled)

    # ---- internals ----

    def _initial_order(self, start_index: int) -> list[int]:
        if not self._shuffle:
            return list(range(len(self._queue)))
        remaining = [i for i in range(len(self._queue)) if i != start_index]
        random.shuffle(remaining)
        return [start_index] + remaining

    def _load_and_play(self, track: dict):
        self._had_error_for_current_load = False
        self._media_player.setSource(QUrl.fromLocalFile(track["path"]))
        # Every track that reaches the player today is music (Songs and
        # Playlists are the only two double-click-to-play entry points, and
        # both are music-only) — hardcoded rather than plumbed through the
        # queue dict for a value that's never actually different yet.
        lufs, true_peak = track.get("loudness_lufs"), track.get("loudness_true_peak")
        self._track_gain_db = compute_gain_db(lufs, true_peak, "music") if lufs is not None and true_peak is not None else 0.0
        self._apply_volume()
        self._media_player.play()
        art = tagging.read_tags(track["path"]).get("art")
        self.nowPlayingChanged.emit({**track, "art": art})

    def _apply_volume(self):
        # QAudioOutput volume tops out at 1.0, so boosting a quiet track is
        # best-effort headroom under the user's current slider position —
        # attenuating a too-loud track always works fully; boosting a
        # too-quiet one can't exceed what's left under the slider. Inherent
        # to gain-via-volume-scaling, not a bug.
        multiplier = 10 ** (self._track_gain_db / 20.0)
        self._audio_output.setVolume(max(0.0, min(1.0, self._user_volume * multiplier)))

    def _on_state_changed(self, state):
        self.playbackStateChanged.emit(self._STATE_NAMES.get(state, "stopped"))

    def _on_media_status_changed(self, status):
        if status == QMediaPlayer.MediaStatus.EndOfMedia:
            self.next()
        elif status == QMediaPlayer.MediaStatus.InvalidMedia and not self._had_error_for_current_load:
            # Fallback for backends that report a bad file this way instead
            # of (or in addition to) errorOccurred — the flag stops a single
            # bad file from skipping two tracks if both signals fire.
            self.playbackError.emit(self._media_player.errorString() or "Invalid media")
            self.next()

    def _on_error(self, error, error_string: str):
        self._had_error_for_current_load = True
        self.playbackError.emit(error_string or "Unknown playback error")
        self.next()
