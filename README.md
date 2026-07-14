# Splice

A PyQt6 desktop app for merging and splitting mp3 audiobooks — merge multi-part
files into one, then split into fixed-length segments (`0.mp3`, `1.mp3`, `2.mp3`, ...)
for devices like the Shokz OpenSwim Pro.

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

**Output location** — Splice has no default output folder. On every launch it opens
a folder picker asking where to save output (starting from your last choice, but you
confirm each time); canceling re-prompts, since the app can't run without a location.
Once chosen, it's also shown in the bar near the top of the window — click it any time
during the session to change where files get saved.

**Merge module** — drop two or more mp3 parts of the same audiobook (in order, or
drag within the list to reorder), then click MERGE. Produces a single
`<name>_merged.mp3` in the output folder. Uses a fast stream copy; if the source
files don't share the same format it automatically retries with a re-encode.

**Split module** — set the segment length in minutes (defaults to 2), then drop
one or more mp3 files onto the drop zone — splitting starts immediately. Each file
gets its own `<filename>_split/` folder in the output location, containing
`0.mp3`, `1.mp3`, `2.mp3`, etc. Splitting is a fast stream copy (no re-encoding),
so segment boundaries can be off by up to ~1 second.

To chop up a multi-part book: merge the parts first, then drag the resulting
`_merged.mp3` onto the split drop zone.
