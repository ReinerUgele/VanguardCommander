# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""UFW-Firewall (läuft als root über den Daemon bzw. sudo im SSH-Modus)."""

from __future__ import annotations

import re

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QVBoxLayout, QWidget

from .theme import button

PORT_RE = re.compile(r"^\d{1,5}(:\d{1,5})?(/(tcp|udp))?$")
SERVICE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 ._-]{0,40}$")


class FirewallTab(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        bar = QHBoxLayout()
        bar.addWidget(button("🔄 Status", self.refresh))
        bar.addWidget(button("🛡️ UFW aktivieren", self.enable, bg="#2e7d32", fg="#ffffff"))
        bar.addWidget(button("⚠️ UFW deaktivieren", self.disable, bg="#8b1e1e", fg="#ffffff"))
        bar.addStretch()
        v.addLayout(bar)
        self.display = QPlainTextEdit()
        self.display.setReadOnly(True)
        self.display.setFont(QFont("Consolas", 10))
        v.addWidget(self.display)
        box = QGroupBox("Regeln")
        h = QHBoxLayout(box)
        h.addWidget(QLabel("Port/Dienst:"))
        self.port_in = QLineEdit()
        self.port_in.setPlaceholderText("z. B. 443, 8000/tcp, 6000:6010/udp oder OpenSSH")
        h.addWidget(self.port_in)
        h.addWidget(button("✅ Erlauben", lambda: self.rule("allow")))
        h.addWidget(button("⛔ Sperren", lambda: self.rule("deny")))
        h.addWidget(QLabel("  Regel Nr.:"))
        self.num_in = QLineEdit()
        self.num_in.setFixedWidth(50)
        h.addWidget(self.num_in)
        h.addWidget(button("🗑️ Regel löschen", self.delete_rule, bg="#5a1e1e"))
        v.addWidget(box)

    def on_connected(self):
        self.refresh()

    def on_disconnected(self):
        self.display.clear()

    def refresh(self, *_):
        self.main.run_cmd("ufw status numbered verbose", as_root=True, show=False,
                          on_done=lambda res, err: self.display.setPlainText(
                              self.main.masker.text(res.text() if res else f"Fehler: {err}")))

    def _run(self, cmd: str, title: str):
        self.main.run_cmd(cmd, as_root=True, title=title, on_done=lambda *_: self.refresh())

    def enable(self):
        if QMessageBox.warning(
            self, "UFW aktivieren",
            "Achtung: Wenn SSH (Port 22 bzw. dein SSH-Port) nicht erlaubt ist, sperrst du dich aus.\n\n"
            "Vor dem Aktivieren wird automatisch eine Regel für den SSH-Port dieses Profils angelegt. Fortfahren?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
            return
        port = int(self.main.current_profile().get("ssh_port", 22) or 22)
        self._run(f"ufw allow {port}/tcp && ufw --force enable", "UFW aktivieren")

    def disable(self):
        if QMessageBox.question(self, "UFW deaktivieren", "Firewall wirklich ausschalten?") == QMessageBox.StandardButton.Yes:
            self._run("ufw disable", "UFW deaktivieren")

    def rule(self, verb: str):
        p = self.port_in.text().strip()
        if not (PORT_RE.match(p) or SERVICE_RE.match(p)):
            QMessageBox.warning(self, "Firewall", "Bitte Port (z. B. 443 oder 8000/tcp) oder Dienstname eingeben.")
            return
        arg = p if PORT_RE.match(p) else f"'{p}'"
        self._run(f"ufw {verb} {arg}", f"ufw {verb} {p}")
        self.port_in.clear()

    def delete_rule(self):
        n = self.num_in.text().strip()
        if not n.isdigit():
            QMessageBox.warning(self, "Firewall", "Bitte die Nummer aus der Liste [ n ] eingeben.")
            return
        if QMessageBox.question(self, "Regel löschen", f"Regel Nr. {n} löschen?") == QMessageBox.StandardButton.Yes:
            self._run(f"ufw --force delete {int(n)}", f"ufw delete {n}")
            self.num_in.clear()
