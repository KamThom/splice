"""Palette constants and the app-wide Qt stylesheet."""

from pathlib import Path

from PyQt6.QtGui import QColor, QFontDatabase, QPalette

FONTS_DIR = Path(__file__).parent / "assets" / "fonts"
DISPLAY_FONT_FAMILY = "Righteous"   # headings, logo, section labels — the 70s display face
BODY_FONT_FAMILY = "Barlow"         # everything else: lists, buttons, dialogs


def load_bundled_fonts():
    """Registers the app's bundled fonts with Qt so every machine renders
    identically regardless of what's installed system-wide — no install
    step, ever. Call once, after QApplication exists and before building
    any widgets (main_window.py's main() does this immediately after
    constructing QApplication). Righteous and Barlow are both SIL Open
    Font License, free to bundle and redistribute — license text sits
    alongside the .ttf files in assets/fonts/."""
    for filename in ("Righteous-Regular.ttf", "Barlow-Regular.ttf",
                     "Barlow-Medium.ttf", "Barlow-SemiBold.ttf"):
        path = FONTS_DIR / filename
        if path.exists():
            QFontDatabase.addApplicationFont(str(path))


# --- Midnight / 70s sunset ---
# Deep navy surfaces, one coral accent for anything interactive or
# selected, and amber + teal from the sunset stripe used only for status
# and decoration (never for clickable things, so coral keeps its meaning).
BG = "#0F1420"           # window background
SURFACE = "#161D2C"      # panels, cards, buttons
SIDEBAR = "#0B101A"      # sidebar / deepest chrome
CONSOLE = "#0B101A"      # input/log "well" — darker than panels so it reads as recessed
RAISED = "#1D2638"       # hover fill, header sections
INK = "#E6EAF2"          # primary text
MUTED = "#8792A8"        # secondary text
BORDER = "#232C3E"       # hairlines
ACCENT = "#FF6B5B"       # coral — primary action + active/selected
ACCENT_HOVER = "#FF8577"
ON_ACCENT = "#0F1420"    # text/icons drawn on a coral fill
AMBER = "#F2B544"        # sunset stripe / warnings / in-progress
TEAL = "#3FB6A8"         # sunset stripe / success
GREEN = TEAL             # success
RED = "#F0465A"          # true error — pinker and hotter than the coral accent
SUNSET = (ACCENT, AMBER, TEAL)

# Categorical accents for decorative numbers (stat cards, health counts) —
# informational only, never on anything clickable.
STAT_BLUE = TEAL
STAT_PURPLE = "#B48EF0"
STAT_AMBER = AMBER


def _rgba(hex_color: str, alpha: float) -> str:
    c = QColor(hex_color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


ACCENT_TINT = _rgba(ACCENT, 0.14)   # selected-but-not-filled backgrounds

RADIUS = 14       # panels and cards
RADIUS_SM = 10    # inputs
RADIUS_PILL = 15  # buttons / tabs (≈ half their height)


def build_palette() -> QPalette:
    """Dark QPalette for the Fusion style, so anything Qt draws itself
    without going through the stylesheet (built-in dialogs, item views'
    native bits, disabled text) matches the stylesheet instead of
    flashing light grey."""
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: BG,
        QPalette.ColorRole.WindowText: INK,
        QPalette.ColorRole.Base: SURFACE,
        QPalette.ColorRole.AlternateBase: RAISED,
        QPalette.ColorRole.Text: INK,
        QPalette.ColorRole.Button: SURFACE,
        QPalette.ColorRole.ButtonText: INK,
        QPalette.ColorRole.Highlight: ACCENT,
        QPalette.ColorRole.HighlightedText: ON_ACCENT,
        QPalette.ColorRole.ToolTipBase: RAISED,
        QPalette.ColorRole.ToolTipText: INK,
        QPalette.ColorRole.PlaceholderText: MUTED,
        QPalette.ColorRole.Link: ACCENT,
        QPalette.ColorRole.Mid: BORDER,
        QPalette.ColorRole.Dark: SIDEBAR,
        QPalette.ColorRole.Light: RAISED,
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(MUTED))
    return p


STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {BG};
    color: {INK};
    font-family: "{BODY_FONT_FAMILY}", "Helvetica Neue", "Segoe UI", sans-serif;
    font-size: 13px;
}}
QToolTip {{
    background-color: {RAISED};
    color: {INK};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 4px 8px;
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
    font-family: "{DISPLAY_FONT_FAMILY}";
    font-size: 16px;
    letter-spacing: 0.5px;
}}
QLabel#appTitle {{
    color: {ACCENT};
    font-family: "{DISPLAY_FONT_FAMILY}";
    font-size: 28px;
    letter-spacing: 1px;
}}
QLabel#appSubtitle {{
    color: {MUTED};
    font-size: 12px;
    letter-spacing: 0.5px;
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
    border-radius: {RADIUS_PILL}px;
    padding: 7px 18px;
    font-weight: 500;
}}
QPushButton:hover {{
    border-color: {ACCENT};
    color: {ACCENT};
}}
QPushButton:pressed {{
    background-color: {RAISED};
}}
QPushButton:disabled {{
    color: {MUTED};
    border-color: {BORDER};
    background-color: {BG};
}}
QPushButton#primary {{
    background-color: {ACCENT};
    color: {ON_ACCENT};
    font-weight: 600;
    border: none;
}}
QPushButton#primary:hover {{
    background-color: {ACCENT_HOVER};
    color: {ON_ACCENT};
}}
QPushButton#primary:disabled {{
    background-color: {BORDER};
    color: {MUTED};
}}
QPushButton#danger {{
    color: {RED};
    border-color: {_rgba(RED, 0.45)};
}}
QPushButton#danger:hover {{
    background-color: {_rgba(RED, 0.12)};
    border-color: {RED};
    color: {RED};
}}
QPushButton:checked {{
    background-color: {ACCENT};
    color: {ON_ACCENT};
    border: none;
}}
QPushButton::menu-indicator {{
    subcontrol-position: right center;
    right: 8px;
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
    border-color: {ACCENT};
    color: {INK};
}}
QPlainTextEdit, QTextEdit {{
    background-color: {CONSOLE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    color: {INK};
    padding: 6px;
    selection-background-color: {ACCENT};
    selection-color: {ON_ACCENT};
}}
QLineEdit, QSpinBox, QDoubleSpinBox {{
    background-color: {CONSOLE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    color: {INK};
    padding: 6px 10px;
    selection-background-color: {ACCENT};
    selection-color: {ON_ACCENT};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QPlainTextEdit:focus {{
    border-color: {ACCENT};
}}
QComboBox {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    color: {INK};
    padding: 5px 10px;
}}
QComboBox:hover, QComboBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox QAbstractItemView {{
    background-color: {RAISED};
    border: 1px solid {BORDER};
    color: {INK};
    selection-background-color: {ACCENT};
    selection-color: {ON_ACCENT};
    outline: none;
}}
QCheckBox {{
    spacing: 8px;
    background: transparent;
}}
QCheckBox::indicator {{
    width: 16px;
    height: 16px;
    border: 1px solid {MUTED};
    border-radius: 5px;
    background-color: {CONSOLE};
}}
QCheckBox::indicator:hover {{
    border-color: {ACCENT};
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
}}
QTabWidget::pane {{
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
    background-color: {SURFACE};
    top: 6px;
}}
QTabBar::tab {{
    background-color: transparent;
    color: {MUTED};
    border: 1px solid transparent;
    border-radius: {RADIUS_PILL}px;
    padding: 6px 16px;
    margin-right: 4px;
    font-weight: 500;
}}
QTabBar::tab:selected {{
    background-color: {ACCENT_TINT};
    color: {ACCENT};
    border-color: {ACCENT};
}}
QTabBar::tab:hover:!selected {{
    color: {INK};
    background-color: {RAISED};
}}
QAbstractItemView {{
    background-color: {SURFACE};
    alternate-background-color: {RAISED};
    border: none;
    color: {INK};
    selection-background-color: {ACCENT};
    selection-color: {ON_ACCENT};
    outline: none;
}}
QHeaderView::section {{
    background-color: {SURFACE};
    color: {MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 6px 8px;
    font-weight: 500;
}}
QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollArea > QWidget > QWidget {{
    background: transparent;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {BORDER};
    border-radius: 3px;
    min-height: 28px;
    min-width: 28px;
}}
QScrollBar::handle:vertical:hover, QScrollBar::handle:horizontal:hover {{
    background: {MUTED};
}}
QScrollBar::add-line, QScrollBar::sub-line {{
    width: 0px;
    height: 0px;
}}
QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent;
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
    border-radius: 6px;
}}
QListWidget#filePicker::item:selected {{
    background-color: {ACCENT};
    color: {ON_ACCENT};
}}
QLabel#coverArt {{
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    background-color: {CONSOLE};
    color: {MUTED};
}}
QWidget#toolRail {{
    background-color: {SIDEBAR};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QWidget#playlistDeck {{
    background-color: {SIDEBAR};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QWidget#sidebar {{
    background-color: {SIDEBAR};
    border-right: 1px solid {BORDER};
}}
QWidget#sidebar QWidget {{
    background-color: transparent;
}}
QPushButton#navItem {{
    background-color: transparent;
    border: none;
    border-radius: 10px;
    padding: 0px;
}}
QPushButton#navItem:hover {{
    background-color: {RAISED};
}}
QPushButton#navItem:checked {{
    background-color: {ACCENT_TINT};
}}
QPushButton#attentionChip {{
    background-color: transparent;
    border: 1px solid {BORDER};
    border-radius: 13px;
    padding: 0px;
}}
QPushButton#attentionChip:hover {{
    border-color: {MUTED};
}}
QPushButton#attentionChip:checked {{
    background-color: {ACCENT_TINT};
    border-color: {ACCENT};
}}
QLabel#navText {{
    color: {INK};
    font-size: 14px;
    font-weight: 500;
}}
QPushButton#attentionChip QLabel#navText {{
    color: {MUTED};
    font-size: 12px;
}}
QLabel#navText[active="true"], QPushButton#attentionChip QLabel#navText[active="true"] {{
    color: {ACCENT};
}}
QLabel#navCount {{
    color: {MUTED};
    font-size: 12px;
}}
QLabel#navCount[alert="true"] {{
    color: {AMBER};
    font-weight: 600;
}}
QLabel#navCount[active="true"] {{
    color: {ACCENT};
}}
QPushButton#sidebarAdd {{
    border-radius: 11px;
    padding: 0px;
    min-width: 22px;
    max-width: 22px;
    min-height: 22px;
    max-height: 22px;
    font-size: 15px;
}}
QWidget#topBar {{
    background-color: transparent;
}}
QLabel#pageTitle {{
    color: {INK};
    font-family: "{DISPLAY_FONT_FAMILY}";
    font-size: 22px;
}}
QLabel#pageSubtitle {{
    color: {MUTED};
    font-size: 13px;
}}
QPushButton#toolButton {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_PILL}px;
    padding: 6px 14px;
}}
QPushButton#toolButton:hover {{
    border-color: {ACCENT};
    color: {ACCENT};
}}
QPushButton#toolButton:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    color: {ON_ACCENT};
}}
QPushButton#iconButton {{
    padding: 6px 14px;
}}
QPushButton#iconButton::menu-indicator {{
    image: none;
    width: 0px;
}}
QWidget#columnHeader {{
    background-color: transparent;
    border-bottom: 1px solid {BORDER};
}}
QPushButton#columnHeaderButton {{
    background: transparent;
    border: none;
    border-radius: 6px;
    padding: 4px 0px;
    color: {MUTED};
    text-align: left;
    font-weight: 500;
}}
QPushButton#columnHeaderButton:hover {{
    color: {INK};
}}
QPushButton#columnHeaderButton[sorted="true"] {{
    color: {ACCENT};
}}
QLabel#tinyMuted {{
    color: {MUTED};
    font-size: 11px;
}}
QTabWidget#sectionStack::pane {{
    border: none;
    background: transparent;
    top: 0px;
}}
QWidget#sidebar QWidget#nowPlayingBar {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: 12px;
}}
QFrame#toolDrawer {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QFrame#toolDrawer QWidget {{
    background-color: transparent;
}}
QFrame#toolDrawer QLineEdit, QFrame#toolDrawer QPlainTextEdit, QFrame#toolDrawer QComboBox,
QFrame#toolDrawer QDoubleSpinBox, QFrame#toolDrawer QSpinBox {{
    background-color: {CONSOLE};
}}
QFrame#toolDrawer QPushButton {{
    background-color: {RAISED};
}}
QFrame#toolDrawer QPushButton#primary, QFrame#toolDrawer QPushButton:checked {{
    background-color: {ACCENT};
}}
QFrame#toolDrawer QTabBar::tab:selected {{
    background-color: {ACCENT_TINT};
}}
QLabel#drawerTitle {{
    color: {INK};
    font-family: "{DISPLAY_FONT_FAMILY}";
    font-size: 20px;
}}
QPushButton#drawerClose {{
    background-color: transparent;
    border: none;
    border-radius: 14px;
    padding: 0px;
    min-width: 28px;
    max-width: 28px;
    min-height: 28px;
    max-height: 28px;
    color: {MUTED};
    font-size: 14px;
}}
QPushButton#drawerClose:hover {{
    background-color: {RAISED};
    color: {INK};
}}
QPushButton#logButton {{
    padding: 6px 12px;
    text-align: left;
}}
QPushButton#logButton:checked {{
    background-color: {ACCENT_TINT};
    border: 1px solid {ACCENT};
    color: {ACCENT};
}}
QLabel#sidebarSection {{
    color: {MUTED};
    font-family: "{DISPLAY_FONT_FAMILY}";
    font-size: 12px;
    letter-spacing: 1px;
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
    border-radius: 8px;
}}
QListWidget#sidebarList::item:hover {{
    background-color: {RAISED};
}}
QListWidget#sidebarList::item:selected {{
    background-color: {ACCENT_TINT};
    color: {ACCENT};
}}
QMenu {{
    background-color: {RAISED};
    border: 1px solid {BORDER};
    border-radius: {RADIUS_SM}px;
    color: {INK};
    padding: 4px;
}}
QMenu::item {{
    padding: 7px 18px;
    border-radius: 6px;
}}
QMenu::item:selected {{
    background-color: {ACCENT_TINT};
    color: {ACCENT};
}}
QMenu::item:disabled {{
    color: {MUTED};
}}
QMenu::separator {{
    height: 1px;
    background: {BORDER};
    margin: 4px 6px;
}}
QWidget#nowPlayingBar {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QLabel#nowPlayingTitle {{
    font-weight: 600;
    color: {INK};
}}
QFrame#consoleStrip {{
    background-color: {SURFACE};
    border: 1px solid {BORDER};
    border-radius: {RADIUS}px;
}}
QFrame#consoleStrip:hover {{
    border-color: {ACCENT};
}}
QSlider::groove:horizontal {{
    height: 4px;
    background: {BORDER};
    border-radius: 2px;
}}
QSlider::sub-page:horizontal {{
    background: {ACCENT};
    border-radius: 2px;
}}
QSlider::add-page:horizontal {{
    background: {BORDER};
    border-radius: 2px;
}}
QSlider::handle:horizontal {{
    background: {INK};
    border: none;
    width: 12px;
    height: 12px;
    margin: -4px 0;
    border-radius: 6px;
}}
QSlider::handle:horizontal:hover {{
    background: {ACCENT};
}}
QSlider:disabled::groove:horizontal, QSlider:disabled::handle:horizontal {{
    border-color: {BORDER};
    background: {BORDER};
}}
"""
