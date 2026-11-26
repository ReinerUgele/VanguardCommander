# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
APP_QSS = """
QWidget { background-color: #1e1e24; color: #abb2bf; font-size: 11px; }
QLineEdit, QSpinBox, QComboBox, QPlainTextEdit, QTextEdit {
    background-color: #21252b; color: #abb2bf; border: 1px solid #3e4451; padding: 3px 5px; border-radius: 3px; }
QPushButton { background-color: #2c313a; color: #abb2bf; border: 1px solid #3e4451; border-radius: 3px; padding: 4px 10px; }
QPushButton:hover { background-color: #3e4451; }
QPushButton:disabled { color: #5c6370; }
QTableWidget, QListWidget { background-color: #14161a; gridline-color: #2c313a; border: 1px solid #3e4451;
    selection-background-color: #3e4451; selection-color: #ffffff; }
QHeaderView::section { background-color: #282c34; color: #61afef; font-weight: bold; padding: 5px; border: none; }
QTabWidget::pane { border: 1px solid #3e4451; background: #1e1e24; }
QTabBar::tab { background: #282c34; color: #abb2bf; padding: 7px 16px; font-weight: bold; }
QTabBar::tab:selected { background: #3e4451; color: #61afef; }
QMenu { background-color: #21252b; color: #abb2bf; border: 1px solid #3e4451; }
QMenu::item:selected { background-color: #3e4451; }
QGroupBox { border: 1px solid #3e4451; border-radius: 4px; margin-top: 10px; padding-top: 6px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; color: #61afef; }
QProgressBar { border: 1px solid #3e4451; border-radius: 4px; text-align: center; background-color: #14161a;
    color: #ffffff; font-weight: bold; }
QProgressBar::chunk { background-color: #2e7d32; border-radius: 3px; }
QToolTip { background-color: #282c34; color: #d7dae0; border: 1px solid #3e4451; }
"""

GREEN, RED, YELLOW, BLUE, GREY = "#98c379", "#e06c75", "#e5c07b", "#61afef", "#5c6370"


# Gedämpfte Knopf-Farben: dunkler Hintergrund, farbige gut lesbare Schrift, farbiger Rand.
# Schlüssel = die bisher im Code verwendeten kräftigen Farben, damit alle Aufrufe automatisch mitziehen.
_SOFT = {
    "#2e7d32": ("#1d2a22", "#8fd18a", "#335f3b"),   # grün: Verbinden, Speichern, Start
    "#8b1e1e": ("#2c1d20", "#f0939a", "#6b2f35"),   # rot: Trennen, gefährlich
    "#5a1e1e": ("#2c1d20", "#f0939a", "#6b2f35"),
    "#1f3a58": ("#1e2530", "#a8c8ec", "#30425a"),   # blau: Standard
    "#223344": ("#1e2530", "#8ec2f5", "#30425a"),
    "#2b2d42": ("#29261f", "#e9cb8c", "#56492f"),   # gelb: eigene Befehle
    "#2c313a": ("#242830", "#c5cad3", "#3a414c"),   # neutral
    "#3e4451": ("#242830", "#c5cad3", "#3a414c"),
}
_DEFAULT_FG = ("#abb2bf", "#ffffff", "#1e2530", "")


def button_style(bg: str = "#1f3a58", fg: str = "") -> str:
    back, text, border = _SOFT.get(bg.lower(), (bg, "#c5cad3", "#3a414c"))
    if fg and fg.lower() not in _DEFAULT_FG:
        text = fg  # ausdrücklich gewünschte Schriftfarbe (z. B. gelb für Live-Funktionen) behalten
    from PyQt6.QtGui import QColor
    hover = QColor(back).lighter(135).name()
    return (f"QPushButton {{ background-color: {back}; color: {text}; border: 1px solid {border}; "
            f"border-radius: 4px; padding: 4px 10px; font-weight: 600; }}"
            f"QPushButton:hover {{ background-color: {hover}; border-color: {text}; }}"
            f"QPushButton:pressed {{ background-color: {back}; }}"
            f"QPushButton:checked {{ background-color: #3a2c47; color: #e3b5f5; border-color: #c678dd; }}"
            f"QPushButton:disabled {{ color: #5c6370; border-color: #2c313a; }}")


def button(text: str, handler=None, bg: str = "#1f3a58", fg: str = "", tip: str = ""):
    from PyQt6.QtWidgets import QPushButton
    b = QPushButton(text)
    b.setStyleSheet(button_style(bg, fg))
    if tip:
        b.setToolTip(tip)
    if handler is not None:
        b.clicked.connect(lambda _=False: handler())
    return b


def fmt_bytes(b: float) -> str:
    if b < 0:
        return "-"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024 or unit == "TB":
            return f"{int(b)} {unit}" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"
