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

Requires Python 3.10+ and [ffmpeg](https://ffmpeg.org/).

```bash
pip install -r requirements.txt
```

(`requirements-dev.txt` adds pytest for the test suite.)

**ffmpeg:**
- macOS: `brew install ffmpeg`
- Windows: `winget install ffmpeg` (or `choco install ffmpeg` / `scoop install ffmpeg`)
- Ubuntu/Debian: `sudo apt install ffmpeg` (see below for the full Linux steps)

## Usage

```bash
python splice.py
```

## Running on Ubuntu / Linux

Tested on Ubuntu; other distros work the same way with their own package
manager (`dnf`, `pacman`, ...).

**1. Install system packages** — Python with venv support, ffmpeg, and the
X11/XCB libraries Qt 6 needs to open a window (`libxcb-cursor0` in particular
is missing on a stock Ubuntu install and is the usual reason PyQt6 apps fail to
start):

```bash
sudo apt update
sudo apt install python3 python3-venv python3-pip ffmpeg libxcb-cursor0 libxkbcommon-x11-0 libegl1
```

**2. Create a virtual environment and install dependencies.** Recent Ubuntu
releases (23.04+) block `pip install` into the system Python
("externally-managed-environment"), so use a venv inside the project folder:

```bash
cd /path/to/splice
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

**3. Run it:**

```bash
python splice.py
```

Next time, just activate the venv and run — or skip activating and call the
venv's Python directly:

```bash
.venv/bin/python splice.py
```

**Running from VS Code.** The repo ships a `.vscode/` config that points VS
Code at the project's `.venv` and adds a **Run Splice** launch configuration.
After the one-time setup above (venv created, requirements installed):

1. Open the project folder in VS Code (`code /path/to/splice`). Install the
   Microsoft **Python** extension if prompted.
2. Check the interpreter in the bottom-right status bar says `.venv`. If it
   doesn't, press `Ctrl+Shift+P` → **Python: Select Interpreter** → pick
   `./.venv/bin/python`. VS Code remembers this choice for the folder.
3. Press **F5** (or Run and Debug → **Run Splice**). New terminals opened
   inside VS Code also activate the venv automatically, so `python splice.py`
   works there too.

If you created the venv while VS Code was already open, run **Developer:
Reload Window** so it picks it up.

**Optional: launcher icon.** To start Splice from your app menu, create
`~/.local/share/applications/splice.desktop` (replace `/path/to/splice` with
the real path):

```ini
[Desktop Entry]
Type=Application
Name=Splice
Comment=Organize, merge, split, download and tag mp3s
Exec=/path/to/splice/.venv/bin/python /path/to/splice/splice.py
Path=/path/to/splice
Terminal=false
Categories=AudioVideo;Audio;
```

**Troubleshooting**

- *`qt.qpa.plugin: Could not load the Qt platform plugin "xcb"`* — a Qt system
  library is missing. Install the packages from step 1; to see exactly which
  library is missing, run `QT_DEBUG_PLUGINS=1 python splice.py`.
- *Wayland sessions* — Qt falls back to XWayland automatically. To force native
  Wayland, run `QT_QPA_PLATFORM=wayland python splice.py` (needs
  `sudo apt install qt6-wayland` on some setups).
- *`GLib-GIO-ERROR: Settings schema 'org.gnome.settings-daemon.plugins.xsettings'
  does not contain a key named 'antialiasing'` / "Aborted (core dumped)"* — you
  launched from the **snap** build of VS Code, whose terminal points GTK at the
  snap's old bundled GNOME schemas. `splice.py` now undoes this automatically on
  startup; if you still see it (e.g. running another Qt script), launch from a
  regular terminal, or install VS Code from Microsoft's `.deb` instead of the snap.
- *"ffmpeg was not found on your PATH"* — `sudo apt install ffmpeg`, then
  confirm with `ffmpeg -version`. Avoid the snap version of ffmpeg if you hit
  permission errors reading files outside your home folder.
- *YouTube downloads failing* — YouTube changes often; update yt-dlp inside the
  venv with `pip install -U "yt-dlp[default,deno]"`.

## Running the tests

The tests run headless (no window opens) against a small throwaway library
they build in a temp folder; your real library and settings are never touched.
They need ffmpeg on your PATH to make test audio.

```bash
pip install -r requirements-dev.txt
python -m pytest
```

They cover navigation (sidebar, top bar, tool drawer, log), deleting
(songs, whole series, single books, cancel, locked files), duplicate review and
auto-resolve, drag-and-drop into Merge/Tags, and the table helpers.

## Finding your way around

- **Sidebar (left)** — your music: **Songs** and **Audiobooks** with their
  counts, small **Dupes / Unsorted / Health** chips for cleanup (a count turns
  amber when something needs a look), your **Playlists** (**+** makes a new
  one; double-click a name to rename it), and a compact player at the bottom.
- **Top bar** — the page title and a one-line summary on the left. On the
  right: **Merge**, **Split**, **YouTube** and **Tags**; **Log** (its dot shows
  whether something's running); **⋯** for library-wide actions (Add sources,
  Add files, Export, change the library folder); and **Output** for the
  folder Merge/Split/YouTube save into.
- **Tools open in a drawer** that slides over the right side of the window, so
  the page you were on stays put. Click the tool's button again, the ✕, or
  press **Esc** to close it — jobs keep running while it's closed, and the
  YouTube button shows how many downloads are left (e.g. **YouTube 2**).
- **Lists** are tables: click a column name to sort by it, click again to
  reverse. Double-click a song to play it. The bar under each list shows how
  many items are selected and what you can do with them; right-click any row
  for the same actions.
- **Drag songs or audiobooks** from a list straight onto the Merge list, the
  Split drop area or the Tags batch list. These drags only ever copy — a file
  can't be moved out of your library by dragging it.

## Library

The app opens on **Songs**. A new library starts empty — open **⋯ → Add
sources…** to build it from folders you already have:

1. **Add music playlist folders** — one per existing playlist folder (monthly,
   themed, whatever). Mark exactly **one** as your master collection (e.g. a
   "Liked" folder that already contains everything) — tracks from that folder are
   filed into the library but don't get a playlist entry, since the whole library
   *is* that collection. Double-click a folder's name in the list to rename it;
   that's the playlist name it becomes.
2. Optionally **set an audiobooks folder** — its existing structure (standalone
   files, or `Series Name/1_book_one.mp3`-style folders) is preserved exactly,
   no renaming.
3. Click **Scan** — read-only, hashes and reads tags for a preview (new tracks,
   exact duplicates, fuzzy matches, files landing in Unsorted) without writing
   anything. Safe to run as many times as you like.
4. Click **Build library** — copies files into `Library/Music/<Artist>/<Album>/`
   or `Library/Audiobooks/...` and writes `.m3u8` playlists. Your original
   folders are never modified or deleted. Re-runnable later as your collection
   grows — already-migrated files (matched by content hash) are never re-copied.

**Songs** is a table of Title / Artist / Album / Added / Time, plus **In** — how
many playlists a track is in (click that number to jump to one of them). The
search box filters by title, artist or album. Select songs to **Add to
playlist…**, **Edit tags**, **Copy…** them somewhere, or **Delete…** them.

**Audiobooks** opens on a shelf of covers, one per series plus one per book
that isn't in a series; switch **View** to **All** for a table of every file.
Click a series to open it — books are listed in series order, and clicking a
book's cover plays it with the rest of the series queued after it. Hover over
a cover for its **⋯** menu (or right-click it): a series has **Open** and
**Delete series…**; a standalone book has **Play**, **Track info**, **Edit
tags**, **Copy…** and **Delete…**. The series page's ⚙ menu also has **Delete
series…**, and you can select books on it to delete just those.

**Playlists** are fully editable: **Add tracks…** from the library, **Add
files…** from disk, drag rows to reorder, and select tracks to **Edit tags**,
**Copy…** or **Remove from playlist** (the song stays in your library). The ⚙
menu has Export and Delete playlist. Click the cover to set the playlist's
artwork; without one it shows a mosaic of its first few covers. Every change
rewrites the playlist's `.m3u8` immediately.

### Deleting

Every delete asks first and says what's about to go. It removes Splice's copy
only — your original source folders are never touched — but it can't be undone.
Playlists that held a deleted song are updated, and folders the delete leaves
empty (an artist/album or series folder) are cleaned up. A file that's open in
another program is left alone and listed afterwards.

### Cleanup: Duplicates, Unsorted, Health

Duplicate handling is conservative on purpose: exact byte-identical files are
auto-deduplicated (only one copy is ever kept), but anything less certain — same
title/artist with different underlying bytes — is left as two separate tracks and
flagged under **Dupes** for you to review; nothing is ever auto-merged.

- **Resolve…** goes through the pairs one after another, starting at the
  selected pair (or the top). Each pick — **Keep this one**, **Skip**, or **Not
  a duplicate** — opens the next pair right away, until the list ends or you
  click **Stop**. Resize the window once and it stays that size.
- **Auto-resolve…** handles the common case, the same song ripped twice. It
  picks every pair with the same title and artist (ignoring case and spacing)
  whose lengths are within 5 seconds of each other, and keeps the larger file of
  each, usually the higher-bitrate copy. Tick **Only when the album matches
  too** to be stricter. You see exactly what will be kept and deleted before
  anything happens; playlist entries for a deleted copy move to the one kept.
  Anything that doesn't match (different titles, a live version with a
  different length, a missing file) stays in the list for **Resolve…**.

Tracks with missing or unusable title/artist/album tags land in **Unsorted**
instead of a guessed folder — select one and click **Fix tags** to open it in
the Tags tool; saving re-files it into the library. **Health** counts missing
tags, missing artwork, index entries whose file has gone missing, and files in
the library folder Splice doesn't know about.

## Export

Open **⋯ → Export…** (or **Export…** from a playlist's ⚙ menu to preselect just
that one) to copy playlists and/or audiobooks out to a device — a USB
mass-storage MP3 player, an SD card, a staging folder for another library tool
to import from, whatever. Check what you want, choose a destination folder,
click **Export**. Each selected item gets its own subfolder there: playlist
tracks are copied with a zero-padded position prefix (`001 - Title.mp3`) so
devices with no playlist support just play them in the right order by folder
listing; audiobook folders are copied as-is, since their filenames already
preserve series order. Re-exporting is additive by default; check **"Replace
existing contents..."** to clear each item's destination subfolder first instead
of accumulating old copies. Your library is never modified by an export — this
only ever writes into the destination you pick.

## Tools

**Output folder** — Merge, Split and YouTube save into an output folder. Splice
asks for it when it starts (starting from your last choice); change it any time
from **Output → Choose folder…**, or open it with **Output → Open folder**.

Every drop target in the tools (the Merge list, the Split zone, the Tags batch
list) also opens a file picker if you click its empty space instead of dragging.
When you add several files at once they're ordered naturally by filename
(`Part 2` before `Part 10`), not alphabetically.

Each tool has its own progress meter. It fills teal → amber → coral as a job
runs, turns solid teal with "Complete" on success and red with "Failed" or
"Cancelled" otherwise, and sweeps back and forth while a step (like YouTube's
mp3 conversion) has no real percentage to report. **Cancel** stops the job
immediately and deletes whatever partial output it had produced — a half-merged
file, in-progress split segments, or a partially downloaded YouTube mp3.

**Merge** — drop two or more mp3 parts of the same audiobook (in order, or drag
within the list to reorder), then click **Merge**. A dialog asks for the output
file name (pre-filled with `<first file>_merged`). Uses a fast stream copy; if
the source files don't share the same format it automatically retries with a
re-encode. **File as** files the result into the library (as an audiobook or
music) or leaves it in the output folder; **Add to** optionally puts it on a
playlist. Check "Then split into segments" to chop the merged result with the
Split tool's current segment length — no manual hand-off needed.

**Split** — set the segment length in minutes (defaults to 2), then drop one or
more mp3 files onto the drop zone — splitting starts immediately. Splice logs an
estimated segment count per file first. Each file gets its own
`<filename>_split/` folder in the output folder, containing `0.mp3`, `1.mp3`,
`2.mp3`, etc. Splitting is a fast stream copy (no re-encoding), so segment
boundaries can be off by up to ~1 second. When it finishes, **Send segments to
Tags** opens Tags › Batch apply with the new segments already loaded.

**YouTube** — paste one or more URLs (one per line — single videos or playlist
links both work) and click **Add to queue**. Playlist URLs are expanded into
their individual videos automatically. Everything downloads and converts to mp3
(via `yt-dlp` + ffmpeg) one at a time from a visible queue — keep pasting more
while something's downloading and it joins the back of the line; duplicates
are skipped. **Quality** sets the mp3 bitrate for what you add next (remembered
between sessions), **File as** files downloads into the library as music or an
audiobook (or leaves them in the output folder), and **Add to playlist** puts
music straight onto a playlist. Queue items show the video's real title once
it's looked up. Drag items to reorder, or use **Remove selected** / **Clear
queue**; **Cancel** stops only the current download and the queue carries on.
If you close Splice with downloads pending, it offers to resume them next time.

**Tags** — edit ID3 metadata like iTunes/an iPod would show it:

- *Single file*: what **Fix tags** / **Edit tags** open — edit
  title/artist/album/year/track number, choose a cover image, and save. Saving a
  library track re-files it if the new tags change where it belongs.
- *Batch apply*: what the Tags button opens (always empty, never leftovers from
  last time). Add a group of files (e.g. all segments from one `_split/`
  folder, or dragged in from the library), set shared artist/album/year/artwork,
  and apply to all at once — track numbers are set automatically in list order
  (1, 2, 3, ...). Title is left untouched so per-segment titles aren't
  clobbered. Artist/Album/Year and the artwork are remembered between runs.

**Log** — the **Log** button in the top bar opens the log of everything Splice
has done this session. Its dot is grey when idle, amber while working, teal when
something finished and red on an error; background jobs (measuring loudness,
checking artwork) show their progress on the button itself.

## Project layout

`splice.py` is a thin entry point — the app itself lives in the `splice/` package:

- `splice/styles.py` — the Midnight palette, fonts (Righteous + Barlow, bundled
  in `splice/assets/fonts/`, SIL Open Font License) and the app-wide Qt stylesheet
- `splice/sidebar.py` — the sidebar: library sections, cleanup chips, playlists,
  and the slot for the mini player
- `splice/main_window.py` — the window shell (sidebar, top bar, tool drawer, log)
  and the Merge/Split/YouTube/Tags tool behavior
- `splice/library_view.py` — the Songs/Audiobooks/cleanup pages, the playlist and
  audiobook series pages, the shared delete/selection/right-click helpers, and the
  dialogs (Add sources, Add files, Export, duplicate review, track info)
- `splice/widgets.py` — reusable widgets: the track table rows and column header,
  progress meter, drop zones, artwork tiles, audiobook shelf, mini player, log button
- `splice/workers.py` — background `QThread` workers that shell out to ffmpeg/ffprobe/yt-dlp
- `splice/player.py` — in-app playback
- `splice/tagging.py` — `mutagen`-based ID3 read/write helpers (no Qt dependency)
- `splice/library/` — the library backend: `db.py` (SQLite index), `scanner.py`
  (read-only tag/duration/hash reads), `naming.py` (destination paths),
  `playlists.py` (`.m3u8` read/write), `migrate.py` (scan/build workers),
  `export.py` (device-export worker), `loudness.py` and `health.py` (background
  library checks), `series_artwork.py`
- `tests/` — the pytest suite (see "Running the tests")
