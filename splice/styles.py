"""Palette constants and the app-wide Qt stylesheet."""

# --- "Odyssey" retro-futurism palette — warm, rounded, analog-console feel ---
# (deliberately distinct from the wiki's Field Dossier brand colors)
BG = "#43403A"          # warm taupe chassis, lighter than a typical dark theme
SURFACE = "#57534B"      # panel surface, one step up from chassis
CONSOLE = "#2B2924"      # CRT-readout well — stays dark on purpose for phosphor contrast
INK = "#F6EFDD"          # warm cream text
MUTED = "#C9C0AC"        # muted cream for secondary text
BORDER = "#7A7568"       # soft warm border, no hard black edges
ORANGE = "#FF7A33"       # primary accent — hazard-tape orange, warmed up
TEAL = "#3FB8A6"         # secondary accent — retro console teal
GREEN = "#8FD19E"        # success — soft phosphor green, not neon
RED = "#E0664F"          # error — warm rust red

RADIUS = 10
RADIUS_SM = 7

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
    color: {TEAL};
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
    background-color: {TEAL};
    max-height: 3px;
    min-height: 3px;
    border-radius: 1px;
}}
QPushButton {{
    background-color: {BG};
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
    color: {CONSOLE};
    font-weight: bold;
    border: none;
    border-radius: {RADIUS_SM}px;
}}
QPushButton#primary:hover {{
    background-color: #FF9459;
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
    border-color: {TEAL};
    color: {INK};
}}
QPlainTextEdit {{
    background-color: {CONSOLE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    color: {GREEN};
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
    background-color: {TEAL};
    color: {CONSOLE};
}}
QLabel#coverArt {{
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    background-color: {CONSOLE};
    color: {MUTED};
}}
"""
