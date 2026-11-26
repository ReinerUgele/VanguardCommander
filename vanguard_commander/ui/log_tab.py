# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Protokoll und Befehlsausgaben. Ausgaben werden als reiner Text angezeigt (kein HTML wird ausgeführt)."""

from __future__ import annotations

import html
from datetime import datetime

from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtWidgets import QHBoxLayout, QLineEdit, QTextEdit, QVBoxLayout, QWidget

from .theme import GREEN, GREY, RED, YELLOW, button


class LogTab(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        self.view = QTextEdit()
        self.view.setReadOnly(True)
        self.view.setFont(QFont("Consolas", 10))
        self.view.setStyleSheet("background-color: #14161a;")
        v.addWidget(self.view)
        bar = QHBoxLayout()
        self.cmd = QLineEdit()
        self.cmd.setPlaceholderText("Einzelbefehl über die Verbindung ausführen (für Interaktives: Tab „Terminal“) …")
        self.cmd.returnPressed.connect(self._run)
        bar.addWidget(self.cmd)
        bar.addWidget(button("▶ Ausführen", self._run))
        bar.addWidget(button("🧹 Leeren", self.clear, bg="#2c313a"))
        v.addLayout(bar)
        self._entries: list[tuple[str, str, str, bool]] = []   # (Art, Zeit, Text, Fehler)

    MAX_ENTRIES = 3000

    def clear(self):
        self._entries.clear()
        self.view.clear()

    def _add(self, kind: str, text: str, error: bool = False):
        entry = (kind, datetime.now().strftime("%H:%M:%S"), text, error)
        self._entries.append(entry)
        if len(self._entries) > self.MAX_ENTRIES:
            del self._entries[: len(self._entries) - self.MAX_ENTRIES]
        self._append(self._render(entry))

    def _render(self, entry) -> str:
        kind, ts, text, error = entry
        text = html.escape(self.main.masker.text(text))
        stamp = f"<span style='color:{GREY};'>[{ts}]</span> "
        if kind == "info":
            return f"{stamp}<span style='color:{RED if error else GREEN};'>{text.replace(chr(10), '<br>')}</span>"
        if kind == "cmd":
            return f"{stamp}<span style='color:{YELLOW};'>$ {text}</span>"
        return f"<pre style='color:{RED if error else '#abb2bf'}; margin:0;'>{text}</pre>"

    def rebuild(self):
        """Alles neu darstellen (z. B. nach Umschalten des Datenschutz-Modus)."""
        self.view.clear()
        for e in self._entries:
            self._append(self._render(e))

    def _append(self, html_text: str):
        self.view.moveCursor(QTextCursor.MoveOperation.End)
        self.view.append(html_text)
        self.view.moveCursor(QTextCursor.MoveOperation.End)

    def info(self, text: str, error: bool = False):
        self._add("info", text, error)

    def command(self, title: str):
        self._add("cmd", title)

    def result(self, res):
        self._add("result", res.text(), not res.ok)

    def _run(self):
        cmd = self.cmd.text().strip()
        if cmd:
            self.cmd.clear()
            self.main.run_cmd(cmd)  # über den Daemon ohnehin als root, im SSH-Modus als SSH-Benutzer
