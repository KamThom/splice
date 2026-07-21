# Splice

A PyQt6 desktop app for merging and splitting mp3 audiobooks — merge multi-part
files into one, then split into fixed-length segments (`0.mp3`, `1.mp3`, `2.mp3`, ...)
for devices like the Shokz OpenSwim Pro. Also downloads audio straight from YouTube
and edits ID3 tags (title, artist, album, year, track number, cover art) for getting
music and audiobooks properly labeled on devices like an iPod Classic.

## Setup

Requires Python 3 and [ffmpeg](https://ffmpeg.org/).

```bash
pip install -r requirements.txt
```

**ffmpeg:**
- macOS: `brew install ffmpeg`
- Windows: `winget install ffmpeg` (or `choco install ffmpeg` / `scoop install ffmpeg`)

## Usage

```bash
python splice.py
```

## Project layout

`splice.py` is a thin entry point — the app itself lives in the `splice/` package:

- `splice/styles.py` — palette constants and the app-wide Qt stylesheet
- `splice/widgets.py` — reusable widgets (drop zones, file pickers, the level gauge, cover art thumbnail)
- `splice/workers.py` — background `QThread` workers that shell out to ffmpeg/ffprobe/yt-dlp
- `splice/tagging.py` — `mutagen`-based ID3 read/write helpers (no Qt dependency)
- `splice/main_window.py` — the main window: tab wiring and all UI behavior

**Output location** — Splice has no default output folder. On every launch it opens
a folder picker asking where to save output (starting from your last choice, but you
confirm each time); canceling re-prompts, since the app can't run without a location.
Once chosen, it's also shown in the bar near the top of the window — click it any time
during the session to change where files get saved.

Every drop target in Splice (the Merge list, the Split zone, the Tags batch list)
also opens a file picker if you click its empty space instead of dragging — use
whichever's convenient. When you add several files at once (drag or picker),
they're ordered naturally by filename (`Part 2` before `Part 10`), not
alphabetically.

Each module has its own progress gauge (a segmented meter, not a plain bar) and a
CANCEL button once a job is running. Cancelling kills the job immediately and
deletes whatever partial output it had produced — a half-merged file, in-progress
split segments, or a partially downloaded/converted YouTube mp3. The gauge turns
green with "COMPLETE" on success, red with "FAILED"/"CANCELLED" otherwise, and
sweeps back and forth while a phase (like YouTube's mp3 conversion step) has no
real percentage to report — so it's always clear whether something finished,
failed, or is still working, without needing to check the console.

**Merge module** — drop two or more mp3 parts of the same audiobook (in order, or
drag within the list to reorder), then click MERGE. A dialog asks for the output
file name (pre-filled with `<first file>_merged`, editable or replaceable
entirely); confirm to merge, or cancel to back out. Uses a fast stream copy; if
the source files don't share the same format it automatically retries with a
re-encode. Check "Then split into segments" before merging to automatically chop
the merged result using the Split tab's current segment length — no manual
hand-off needed.

**Split module** — set the segment length in minutes (defaults to 2), then drop
one or more mp3 files onto the drop zone — splitting starts immediately. Before it
starts, Splice logs an estimated segment count per file (based on each file's
probed duration); the same estimate follows along next to the current file while
it processes. Each file gets its own `<filename>_split/` folder in the output
location, containing `0.mp3`, `1.mp3`, `2.mp3`, etc. Splitting is a fast stream
copy (no re-encoding), so segment boundaries can be off by up to ~1 second.

Once a split finishes, click "SEND SEGMENTS TO TAGS" to jump straight to Tags >
Batch Apply with the new segments already loaded, ready to tag.

**YouTube module** — paste one or more URLs (one per line — single videos or
playlist links both work) and click ADD TO QUEUE. Playlist URLs are expanded into
their individual videos automatically. Everything downloads and converts to mp3
(via `yt-dlp` + ffmpeg) straight into the output folder, one at a time, from a
visible queue — you can keep pasting more (including while something's already
downloading) and it just joins the back of the line. Already-queued or
currently-downloading duplicates are skipped automatically. The YouTube tab runs
fully independently of Merge/Split/Tags — none of them block each other. This only
downloads — drag the result onto the Split zone yourself if you want it chunked.

Queue items show the video's real title (looked up in the background right after
you add it — briefly shows the raw URL until that resolves). Drag items within the
queue to reorder them, or use REMOVE SELECTED / CLEAR QUEUE to drop pending ones;
CANCEL stops only the one currently downloading (the queue then continues with the
next). The Quality dropdown next to ADD TO QUEUE controls the mp3 bitrate for
whatever you add next (from a VBR "best" option down to 128 kbps for smaller
files) and is remembered between sessions. If you close Splice with downloads
still pending, it'll offer to resume that queue next time you launch.

**Tags module** — edit ID3 metadata like iTunes/an iPod would show it:

- *Single file*: load one mp3, edit title/artist/album/year/track number, choose a
  cover image, and save. Existing tags and artwork (if any) are shown when loaded.
- *Batch apply*: add a group of files (e.g. all segments from one `_split/` folder,
  or via SEND SEGMENTS TO TAGS above), set shared artist/album/year/artwork, and
  apply to all at once — track numbers are set automatically in list order
  (1, 2, 3, ...). Title is left untouched so distinct per-segment titles aren't
  clobbered. Artist/Album/Year and the chosen artwork are remembered between runs
  (handy when tagging a whole audiobook's segments across a couple of sittings).
