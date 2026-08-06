"""Palette constants and the app-wide Qt stylesheet."""

from pathlib import Path

from PyQt6.QtGui import QFontDatabase

FONTS_DIR = Path(__file__).parent / "assets" / "fonts"
PIXEL_FONT_FAMILY = "Silkscreen"


def load_bundled_fonts():
    """Registers the app's bundled fonts with Qt so every machine renders
    identically regardless of what's installed system-wide — no install
    step, ever. Call once, after QApplication exists and before building
    any widgets (main_window.py's main() does this immediately after
    constructing QApplication). Silkscreen and Space Mono are both SIL Open
    Font License, free to bundle and redistribute — license text sits
    alongside the .ttf files in assets/fonts/."""
    for filename in ("Silkscreen-Regular.ttf", "Silkscreen-Bold.ttf",
                      "SpaceMono-Regular.ttf", "SpaceMono-Bold.ttf"):
        path = FONTS_DIR / filename
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))


# --- Direction E — grey/pixel HUD, built from the user's own moodboard ---
# Light grey chassis + white panels (pulled from the lightest reference on
# the board, a white technical schematic sheet) with a single vivid
# red-orange accent doing double duty as "primary action" and "active/
# selected" everywhere — deliberately one accent hue, not two.
BG = "#E7E8EA"           # light grey chassis
SURFACE = "#FFFFFF"       # panel surface — also used for the rail/deck, no separate tone needed
CONSOLE = "#E7E8EA"      # input/log "well" — same as chassis, reads as recessed against white panels
INK = "#1C1E22"          # near-black text
MUTED = "#5E636A"        # mid grey for secondary text
BORDER = "#CBCED3"       # soft grey border
ORANGE = "#FF4F30"       # primary + active/selected accent
GREEN = "#1E9E63"        # success — deepened for legibility on a light background
RED = "#D93A2B"          # true error state — distinct from the accent

RADIUS = 3
RADIUS_SM = 2

STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {BG};
    color: {INK};
    font-family: "Space Mono", "Menlo", "Consolas", "Courier New", monospace;
    font-size: 12px;
}}
QFrame#panel {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QLabel {{
    background: transparent;
}}
QLabel#panelTitle {{
    color: {INK};
    font-weight: bold;
    font-size: 13px;
    letter-spacing: 1px;
}}
QLabel#appTitle {{
    color: {ORANGE};
    font-weight: bold;
    font-size: 26px;
    letter-spacing: 2px;
}}
QLabel#appSubtitle {{
    color: {MUTED};
    font-size: 11px;
    letter-spacing: 3px;
}}
QLabel#muted {{
    color: {MUTED};
}}
QFrame#rule {{
    background-color: {BORDER};
    max-height: 1px;
    min-height: 1px;
}}
QPushButton {{
    background-color: {SURFACE};
    color: {INK};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    padding: 7px 16px;
}}
QPushButton:hover {{
    border-color: {ORANGE};
    color: {ORANGE};
}}
QPushButton:disabled {{
    color: {MUTED};
    border-color: {BORDER};
}}
QPushButton#primary {{
    background-color: {ORANGE};
    color: #FFFFFF;
    font-weight: bold;
    border: none;
    border-radius: {RADIUS_SM}px;
}}
QPushButton#primary:hover {{
    background-color: #FF7156;
}}
QPushButton#primary:disabled {{
    background-color: {BORDER};
    color: {MUTED};
}}
QPushButton#outputBar {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    color: {MUTED};
    text-align: left;
    padding: 11px 16px;
}}
QPushButton#outputBar:hover {{
    border-color: {ORANGE};
    color: {INK};
}}
QPlainTextEdit {{
    background-color: {CONSOLE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    color: {INK};
    padding: 6px;
}}
QDoubleSpinBox {{
    background-color: {CONSOLE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    color: {INK};
    padding: 5px;
}}
QDoubleSpinBox:focus {{
    border-color: {ORANGE};
}}
QLineEdit {{
    background-color: {CONSOLE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    color: {INK};
    padding: 6px 8px;
}}
QLineEdit:focus {{
    border-color: {ORANGE};
}}
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-top-left-radius: 0px;
    border-top-right-radius: 0px;
    border-bottom-left-radius: {RADIUS}px;
    border-bottom-right-radius: {RADIUS}px;
    background-color: {SURFACE};
    top: -1px;
}}
QTabBar::tab {{
    background-color: {BG};
    color: {MUTED};
    border: 1px solid {BORDER};
    border-bottom: none;
    border-top-left-radius: {RADIUS_SM}px;
    border-top-right-radius: {RADIUS_SM}px;
    padding: 7px 16px;
    margin-right: 2px;
    letter-spacing: 1px;
}}
QTabBar::tab:selected {{
    background-color: {SURFACE};
    color: {ORANGE};
    border-color: {ORANGE};
}}
QTabBar::tab:hover:!selected {{
    color: {INK};
}}
QListWidget#filePicker {{
    background-color: {CONSOLE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    color: {INK};
    padding: 4px;
}}
QListWidget#filePicker::item {{
    padding: 4px 6px;
    border-radius: 4px;
}}
QListWidget#filePicker::item:selected {{
    background-color: {ORANGE};
    color: #FFFFFF;
}}
QLabel#coverArt {{
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    background-color: {CONSOLE};
    color: {MUTED};
}}
QWidget#toolRail {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QWidget#playlistDeck {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QLabel#sidebarSection {{
    color: {MUTED};
    font-size: 10px;
    font-weight: bold;
    letter-spacing: 2px;
    padding: 8px 10px 2px 10px;
}}
QListWidget#sidebarList {{
    background-color: transparent;
    border: none;
    color: {INK};
    outline: none;
}}
QListWidget#sidebarList::item {{
    padding: 6px 10px;
    border-radius: {RADIUS_SM}px;
}}
QListWidget#sidebarList::item:hover {{
    color: {ORANGE};
}}
QListWidget#sidebarList::item:selected {{
    background-color: {BG};
    color: {ORANGE};
}}
QMenu {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    color: {INK};
    padding: 4px;
}}
QMenu::item {{
    padding: 7px 16px;
    border-radius: {RADIUS_SM}px;
}}
QMenu::item:selected {{
    background-color: {BG};
    color: {ORANGE};
}}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 6px;
}}
"""
