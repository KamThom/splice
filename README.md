# BookSplit

Splits an mp3 audiobook into fixed-length segments (`0.mp3`, `1.mp3`, `2.mp3`, ...) for devices like the Shokz OpenSwim Pro.

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
python booksplit_gui.py
```

1. Enter segment length in minutes (defaults to 2)
2. Drag one or more mp3 files onto the drop zone
3. Choose an output folder when prompted

Each file gets its own `<filename>_split/` folder inside the chosen output folder, containing `0.mp3`, `1.mp3`, `2.mp3`, etc. Splitting is a fast stream copy (no re-encoding), so segment boundaries can be off by up to ~1 second.
