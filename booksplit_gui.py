#!/usr/bin/env python3
"""GUI for splitting mp3 audiobooks into fixed-length segments (0.mp3, 1.mp3, ...)."""

import os
import shutil
import subprocess
import threading

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from tkinterdnd2 import DND_FILES, TkinterDnD

FFMPEG = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe") or "ffmpeg"

FFMPEG_INSTALL_HINT = (
    "ffmpeg was not found on your PATH.\n\n"
    "macOS:   brew install ffmpeg\n"
    "Windows: winget install ffmpeg  (or choco install ffmpeg / scoop install ffmpeg)\n\n"
    "Then restart this app."
)


def parse_dropped_paths(data: str):
    # tkinterdnd2 gives paths space-separated, braces around ones with spaces
    paths = []
    token = ""
    in_brace = False
    for ch in data:
        if ch == "{":
            in_brace = True
            token = ""
        elif ch == "}":
            in_brace = False
            paths.append(token)
            token = ""
        elif ch == " " and not in_brace:
            if token:
                paths.append(token)
                token = ""
        else:
            token += ch
    if token:
        paths.append(token)
    return [p for p in paths if p.lower().endswith(".mp3")]


class App(TkinterDnD.Tk):
    def __init__(self):
        super().__init__()
        self.title("BookSplit")
        self.geometry("480x420")
        self.minsize(420, 360)
        self.output_dir = None
        self.files = []

        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 12, "pady": 8}

        settings = ttk.Frame(self)
        settings.pack(fill="x", **pad)

        ttk.Label(settings, text="Segment length (minutes):").grid(row=0, column=0, sticky="w")
        self.minutes_var = tk.StringVar(value="2")
        ttk.Entry(settings, textvariable=self.minutes_var, width=6).grid(row=0, column=1, sticky="w", padx=(6, 0))

        self.drop_zone = tk.Label(
            self,
            text="Drag & drop MP3 file(s) here",
            relief="groove",
            bd=2,
            bg="#f0f0f0",
            fg="#555",
            font=("TkDefaultFont", 14),
        )
        self.drop_zone.pack(fill="both", expand=True, **pad)
        self.drop_zone.drop_target_register(DND_FILES)
        self.drop_zone.dnd_bind("<<Drop>>", self.on_drop)

        self.log = tk.Text(self, height=8, state="disabled", bg="#111", fg="#ddd", font=("TkFixedFont", 11))
        self.log.pack(fill="both", expand=False, **pad)

        self.after(200, self._check_ffmpeg)

    def _check_ffmpeg(self):
        if not shutil.which(FFMPEG):
            messagebox.showerror("ffmpeg not found", FFMPEG_INSTALL_HINT)

    def write_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def on_drop(self, event):
        paths = parse_dropped_paths(event.data)
        if not paths:
            messagebox.showwarning("No MP3s", "Drop one or more .mp3 files.")
            return

        try:
            minutes = float(self.minutes_var.get())
            if minutes <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("Invalid segment length", "Enter a positive number of minutes.")
            return

        chosen_dir = filedialog.askdirectory(title="Choose output folder")
        if not chosen_dir:
            self.write_log("Cancelled: no output folder chosen.")
            return

        segment_seconds = int(minutes * 60)
        self.drop_zone.configure(text="Working...", bg="#ddd")
        thread = threading.Thread(target=self.process_files, args=(paths, chosen_dir, segment_seconds), daemon=True)
        thread.start()

    def process_files(self, paths, chosen_dir, segment_seconds):
        for path in paths:
            self._process_one(path, chosen_dir, segment_seconds)
        self.after(0, lambda: self.drop_zone.configure(text="Drag & drop MP3 file(s) here", bg="#f0f0f0"))

    def _process_one(self, path, chosen_dir, segment_seconds):
        base = os.path.splitext(os.path.basename(path))[0]
        outdir = os.path.join(chosen_dir, f"{base}_split")
        os.makedirs(outdir, exist_ok=True)

        self.after(0, self.write_log, f"Splitting: {os.path.basename(path)}")

        cmd = [
            FFMPEG, "-y", "-i", path,
            "-f", "segment",
            "-segment_time", str(segment_seconds),
            "-c", "copy",
            "-reset_timestamps", "1",
            "-map", "0:a",
            os.path.join(outdir, "%d.mp3"),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True)
        except FileNotFoundError:
            self.after(0, self.write_log, "  FAILED: ffmpeg not found. " + FFMPEG_INSTALL_HINT)
            return

        if result.returncode != 0:
            self.after(0, self.write_log, f"  FAILED: {result.stderr.strip().splitlines()[-1] if result.stderr else 'unknown error'}")
            return

        count = len([f for f in os.listdir(outdir) if f.endswith(".mp3")])
        self.after(0, self.write_log, f"  Done: {count} segments -> {outdir}")


if __name__ == "__main__":
    App().mainloop()
