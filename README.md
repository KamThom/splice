# Splice

A PyQt6 desktop app for organizing and prepping mp3 music and audiobooks. At its
core is a **Library**: a deduplicated, one-file-per-track collection (`Music/` by
Artist/Album, `Audiobooks/` preserving your existing series-folder structure)
built non-destructively from your existing folders, with hand-curated playlists
(`.m3u8`) referencing into it instead of each playlist being its own set of
duplicate copies. Alongside that, four tools handle the rest of the pipeline:
merging multi-part audiobooks, splitting into fixed-length segments (`0.mp3`,
`1.mp3`, ... — bookmarks for devices like the Shokz OpenSwim Pro that can't
seek/resume), downloading straight from YouTube, and editing ID3 tags.

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
- `splice/sidebar.py` — the left-hand navigation (Library/Search, Playlists, Tools)
- `splice/workers.py` — background `QThread` workers that shell out to ffmpeg/ffprobe/yt-dlp
- `splice/tagging.py` — `mutagen`-based ID3 read/write helpers (no Qt dependency)
- `splice/library_view.py` — the Library home view, the ADD SOURCES migration wizard, and the EXPORT dialog
- `splice/library/` — the library backend: `db.py` (SQLite index), `scanner.py` (read-only tag/duration/hash reads), `naming.py` (destination paths), `playlists.py` (`.m3u8` read/write), `migrate.py` (scan/build `QThread` workers), `export.py` (device-export `QThread` worker)
- `splice/main_window.py` — the main window: sidebar/page wiring and the Merge/Split/YouTube/Tags tool behavior

## Library

The app opens on the Library view. It starts empty — click **ADD SOURCES...** to
build it from folders you already have:

1. **Add music playlist folders** — one per existing playlist folder (monthly,
   themed, whatever). Mark exactly **one** as your master collection (e.g. a
   "Liked" folder that already contains everything) — tracks from that folder are
   filed into the library but don't get a playlist entry, since the whole library
   *is* that collection. Double-click a folder's name in the list to rename it;
   that's the playlist name it becomes.
2. Optionally **set an audiobooks folder** — its existing structure (standalone
   files, or `Series Name/1_book_one.mp3`-style folders) is preserved exactly,
   no renaming.
3. Click **SCAN** — read-only, hashes and reads tags for a preview (new tracks,
   exact duplicates, fuzzy matches, files landing in Unsorted) without writing
   anything. Safe to run as many times as you like.
4. Click **BUILD LIBRARY** — copies files into `Library/Music/<Artist>/<Album>/`
   or `Library/Audiobooks/...` and writes `.m3u8` playlists. Your original
   folders are never modified or deleted. Re-runnable later as your collection
   grows — already-migrated files (matched by content hash) are never re-copied.

Duplicate handling is conservative on purpose: exact byte-identical files are
auto-deduplicated (only one copy is ever kept), but anything less certain — same
title/artist with different underlying bytes — is left as two separate tracks and
flagged in the **Duplicates** tab for you to review; nothing is ever auto-merged.
Tracks with missing or unusable title/artist/album tags land in **Unsorted**
instead of a guessed folder — select one and click **FIX TAGS** to jump straight
to the Tags tool with it loaded.

The **Songs** and **Audiobooks** tabs are a searchable, sortable table (search
box up top filters by title/artist/album). The **In** column shows how many
playlists a track belongs to. Playlists built by a migration appear in the
sidebar — click one to see its contents (read-only for now; a full playlist
editor is planned).

## Export

Click **EXPORT...** (in the Library view, or on a playlist's page to
preselect just that one) to copy playlists and/or audiobooks out to a device —
a USB mass-storage MP3 player, an SD card, a staging folder for another
library tool to import from, whatever. Check what you want, choose a
destination folder, click **EXPORT**. Each selected item gets its own
subfolder there: playlist tracks are copied with a zero-padded position prefix
(`001 - Title.mp3`) so devices with no playlist support just play them in the
right order by folder listing; audiobook folders are copied as-is, since their
filenames already preserve series order. Re-exporting is additive by default;
check **"Replace existing contents..."** to clear each item's destination
subfolder first instead of accumulating old copies. Your library is never
modified by an export — this only ever writes into the destination you pick.

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
the merged result using the Split tool's current segment length — no manual
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
currently-downloading duplicates are skipped automatically. The YouTube tool runs
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
