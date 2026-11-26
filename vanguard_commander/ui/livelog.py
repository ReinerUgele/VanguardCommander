# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Eigenes Fenster für fortlaufende Ausgaben (docker logs -f, compose logs -f, journalctl -f …).

Mehrere Fenster gleichzeitig möglich. Schließen beendet den Prozess auf dem Server.
"""

from __future__ import annotations

import codecs

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QTextCursor
from PyQt6.QtWidgets import QCheckBox, QDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QVBoxLayout

from ..serverinfo import strip_ansi
from .theme import GREEN, GREY, RED, YELLOW, button
from .workers import error_text


class LiveLogWindow(QDialog):
    def __init__(self, main, title: str, cmd: str, as_root: bool, sudo_password: str, on_finished=None):
        super().__init__(main)
        self.on_finished = on_finished
        self.main, self.cmd, self.as_root, self.sudo_password = main, cmd, as_root, sudo_password
        self.backend = main.backend
        self.task = None
        self.setWindowTitle(f"📡 {title}")
        self.setWindowFlag(Qt.WindowType.WindowMinMaxButtonsHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(1000, 620)
        v = QVBoxLayout(self)
        bar = QHBoxLayout()
        self.btn_toggle = button("⏸️ Anhalten", self.toggle, bg="#3e4451")
        bar.addWidget(self.btn_toggle)
        bar.addWidget(button("🧹 Leeren", lambda: self.view.clear(), bg="#2c313a"))
        self.follow = QCheckBox("Mitlaufen")
        self.follow.setChecked(True)
        bar.addWidget(self.follow)
        self.highlight = QLineEdit()
        self.highlight.setPlaceholderText("Nur Zeilen mit … (Filter, leer = alle)")
        self.highlight.setFixedWidth(260)
        bar.addWidget(self.highlight)
        bar.addStretch()
        self.status = QLabel("")
        bar.addWidget(self.status)
        v.addLayout(bar)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(20000)
        self.view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.view.setFont(QFont("Consolas", 10))
        self.view.setStyleSheet("background-color: #14161a;")
        v.addWidget(self.view)
        hint = QLabel(f"<span style='color:{GREY};'>$ {cmd}</span>")
        hint.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(hint)
        self._pending = ""
        self.start()

    def _set_status(self, text: str, color: str):
        self.status.setText(f"<span style='color:{color};'>{text}</span>")

    def start(self):
        if self.backend is None or self.backend is not self.main.backend:
            self._set_status("Nicht verbunden", RED)
            return
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        backend, cmd, as_root, pw = self.backend, self.cmd, self.as_root, self.sudo_password

        def job(task):
            return backend.stream(cmd, lambda d: task.progress.emit(d), task.cancelled, as_root=as_root,
                                  sudo_password=pw)

        holder: dict = {}
        self.task = self.main.tasks.run(
            job, with_task=True, on_progress=lambda d: self._data(holder.get("t"), d),
            on_ok=lambda rc: self._ended(holder.get("t"), rc),
            on_fail=lambda e: self._ended(holder.get("t"), None, error_text(e)))
        holder["t"] = self.task
        self.btn_toggle.setText("⏸️ Anhalten")
        self._set_status("● läuft", GREEN)

    def stop(self):
        if self.task is not None:
            self.task.cancel()
            self.task = None

    def toggle(self):
        if self.task is not None:
            self.stop()
            self.btn_toggle.setText("▶️ Fortsetzen")
            self._set_status("angehalten", YELLOW)
        else:
            self.view.appendPlainText("──────── fortgesetzt ────────")
            self.start()

    def _data(self, task, chunk: bytes):
        if task is not self.task:
            return  # Reste einer angehaltenen Ausgabe
        text = self.main.masker.text(strip_ansi(self._decoder.decode(chunk)))
        if not text:
            return
        text = self._pending + text.replace("\r\n", "\n")
        lines = text.split("\n")
        self._pending = lines.pop()          # unvollständige letzte Zeile aufheben
        needle = self.highlight.text().strip().lower()
        if needle:
            lines = [ln for ln in lines if needle in ln.lower()]
        if not lines:
            return
        bar = self.view.verticalScrollBar()
        keep = bar.value()
        cursor = self.view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText("\n".join(lines) + "\n")
        if self.follow.isChecked():
            bar.setValue(bar.maximum())
        else:
            bar.setValue(keep)

    def _ended(self, task, rc, err: str = ""):
        if task is not self.task:
            return  # selbst angehalten oder schon neu gestartet
        self.task = None
        if self._pending:
            self.view.appendPlainText(self._pending)
            self._pending = ""
        if err:
            self._set_status(f"Fehler: {err}", RED)
        elif rc is None:
            self._set_status("beendet", GREY)
        else:
            self._set_status(f"beendet (Rückgabewert {rc})", GREY if rc == 0 else YELLOW)
        self.btn_toggle.setText("▶️ Neu starten")
        if self.on_finished is not None:
            self.on_finished(rc)

    def closeEvent(self, ev):
        self.stop()
        super().closeEvent(ev)
