# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Kleine Dialoge: Passwort, Tresor, Host-Key, Editor, Transfer."""

from __future__ import annotations

import time

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QKeySequence, QShortcut, QTextDocument
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from .theme import button_style, fmt_bytes


class PasswordDialog(QDialog):
    def __init__(self, parent, title: str, prompt: str, offer_store: bool = False, store_default: bool = True):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        lbl = QLabel(prompt)
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        self.edit = QLineEdit()
        self.edit.setEchoMode(QLineEdit.EchoMode.Password)
        lay.addWidget(self.edit)
        self.store = QCheckBox("Im verschlüsselten Tresor speichern (Master-Passwort)")
        self.store.setChecked(store_default)
        self.store.setVisible(offer_store)
        lay.addWidget(self.store)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.edit.setFocus()

    @classmethod
    def ask(cls, parent, title: str, prompt: str, offer_store: bool = False) -> tuple[str, bool] | None:
        dlg = cls(parent, title, prompt, offer_store)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.edit.text():
            return None
        return dlg.edit.text(), dlg.store.isVisible() and dlg.store.isChecked()


class MasterPasswordDialog(QDialog):
    def __init__(self, parent, create: bool, error: str = "", offer_remember: bool = True):
        super().__init__(parent)
        self.create = create
        self.setWindowTitle("🔐 Tresor anlegen" if create else "🔐 Tresor entsperren")
        self.setMinimumWidth(440)
        lay = QVBoxLayout(self)
        text = (
            "Passwörter (z. B. sudo) werden verschlüsselt gespeichert. Lege dafür ein Master-Passwort fest "
            "(mindestens 8 Zeichen). Es wird nirgends gespeichert – wenn du es vergisst, sind nur die "
            "gespeicherten Passwörter weg, nicht die Profile."
            if create else
            "Master-Passwort für den Tresor eingeben. Der Tresor bleibt bis zum Programmende entsperrt."
        )
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lay.addWidget(lbl)
        form = QFormLayout()
        self.pw1 = QLineEdit()
        self.pw1.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Master-Passwort:", self.pw1)
        self.pw2 = QLineEdit()
        self.pw2.setEchoMode(QLineEdit.EchoMode.Password)
        if create:
            form.addRow("Wiederholen:", self.pw2)
        lay.addLayout(form)
        from .. import remember
        self.remember = QCheckBox(f"Auf diesem Rechner merken – {remember.method()}")
        self.remember.setToolTip("Der Tresor öffnet sich dann automatisch, solange du an diesem Rechner mit "
                                 "deinem Benutzerkonto angemeldet bist. Abschalten: Einstellungen → Tresor.")
        self.remember.setVisible(offer_remember and remember.available())
        lay.addWidget(self.remember)
        self.err = QLabel(error)
        self.err.setStyleSheet("color: #e06c75;")
        lay.addWidget(self.err)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self._check)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.pw1.setFocus()

    def _check(self):
        if self.create:
            if len(self.pw1.text()) < 8:
                self.err.setText("Mindestens 8 Zeichen.")
                return
            if self.pw1.text() != self.pw2.text():
                self.err.setText("Die beiden Eingaben stimmen nicht überein.")
                return
        if not self.pw1.text():
            return
        self.accept()

    def password(self) -> str:
        return self.pw1.text()

    def wants_remember(self) -> bool:
        return self.remember.isVisible() and self.remember.isChecked()


def confirm_host_key(parent, exc) -> bool:
    msg = (
        f"Der Server <b>{exc.host}</b> ist noch nicht bekannt.<br><br>"
        f"Schlüsseltyp: {exc.key_type}<br>"
        f"Fingerabdruck: <code>{exc.fingerprint}</code><br><br>"
        "Vergleiche den Fingerabdruck am besten direkt auf dem Server mit<br>"
        "<code>ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub</code><br><br>"
        "Server vertrauen und Schlüssel merken?"
    )
    box = QMessageBox(QMessageBox.Icon.Question, "Neuer Server", msg,
                      QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, parent)
    box.setTextFormat(Qt.TextFormat.RichText)
    return box.exec() == QMessageBox.StandardButton.Yes


class FileEditorDialog(QDialog):
    def __init__(self, parent, filename: str, content: str, info_text: str = ""):
        super().__init__(parent)
        self.setWindowTitle(f"📝 Editor — {filename}")
        self.resize(980, 700)
        vbox = QVBoxLayout(self)
        hdr = QHBoxLayout()
        hdr.addWidget(QLabel(f"<b>Datei:</b> <span style='color:#61afef;'>{filename}</span>"))
        if info_text:
            hdr.addWidget(QLabel(f"<span style='color:#5c6370;'>({info_text})</span>"))
        hdr.addStretch()
        for text, slot in (("↩️ Rückgängig", lambda: self.editor.undo()), ("↪️ Wiederholen", lambda: self.editor.redo()),
                           ("🔍 Suchen (Strg+F)", self._toggle_search)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, s=slot: s())
            hdr.addWidget(b)
        vbox.addLayout(hdr)

        self.search_bar = QWidget()
        s = QHBoxLayout(self.search_bar)
        s.setContentsMargins(0, 0, 0, 0)
        s.addWidget(QLabel("Suchen:"))
        self.search_in = QLineEdit()
        self.search_in.returnPressed.connect(self._find_next)
        s.addWidget(self.search_in)
        for text, slot in (("Weiter ➔", self._find_next), ("⬅ Zurück", self._find_prev)):
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, f=slot: f())
            s.addWidget(b)
        self.search_stat = QLabel("")
        self.search_stat.setStyleSheet("color: #e5c07b;")
        s.addWidget(self.search_stat)
        s.addStretch()
        self.search_bar.setVisible(False)
        vbox.addWidget(self.search_bar)

        self.editor = QPlainTextEdit()
        self.editor.setFont(QFont("Consolas" if QFont("Consolas").exactMatch() else "Monospace", 10))
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setPlainText(content)
        self.editor.document().setModified(False)
        vbox.addWidget(self.editor)

        bar = QHBoxLayout()
        bar.addStretch()
        save = QPushButton("💾 Speichern (Strg+S)")
        save.setStyleSheet(button_style("#2e7d32"))
        save.clicked.connect(self.accept)
        cancel = QPushButton("Abbrechen")
        cancel.clicked.connect(self.reject)
        bar.addWidget(save)
        bar.addWidget(cancel)
        vbox.addLayout(bar)
        QShortcut(QKeySequence("Ctrl+S"), self).activated.connect(self.accept)
        QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(self._toggle_search)

    def reject(self):
        if self.editor.document().isModified():
            if QMessageBox.question(self, "Änderungen verwerfen?", "Die Änderungen wurden nicht gespeichert. Verwerfen?") \
                    != QMessageBox.StandardButton.Yes:
                return
        super().reject()

    def _toggle_search(self):
        self.search_bar.setVisible(True)
        self.search_in.setFocus()
        self.search_in.selectAll()

    def _find(self, backward: bool):
        text = self.search_in.text()
        if not text:
            return
        flags = QTextDocument.FindFlag.FindBackward if backward else QTextDocument.FindFlag(0)
        if self.editor.find(text, flags):
            self.search_stat.setText("")
            return
        cur = self.editor.textCursor()
        cur.movePosition(cur.MoveOperation.End if backward else cur.MoveOperation.Start)
        self.editor.setTextCursor(cur)
        found = self.editor.find(text, flags)
        self.search_stat.setText("Von vorne gesucht" if found else "Keine Treffer")

    def _find_next(self):
        self._find(False)

    def _find_prev(self):
        self._find(True)

    def content(self) -> str:
        return self.editor.toPlainText()


class TransferDialog(QDialog):
    def __init__(self, parent, title: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setFixedWidth(540)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)
        v = QVBoxLayout(self)
        self.lbl = QLabel("Bereite vor …")
        v.addWidget(self.lbl)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        v.addWidget(self.bar)
        self.stats = QLabel("")
        v.addWidget(self.stats)
        self.btn_cancel = QPushButton("Abbrechen")
        v.addWidget(self.btn_cancel, alignment=Qt.AlignmentFlag.AlignRight)
        self._t0 = time.monotonic()

    def on_progress(self, info):
        kind = info[0]
        if kind == "file":
            _, idx, count, name = info
            self.lbl.setText(f"<b>({idx}/{count})</b> {name}")
        elif kind == "bytes":
            _, done, total = info
            if total > 0:
                self.bar.setValue(int(done * 1000 / total))
            dt = max(time.monotonic() - self._t0, 0.001)
            speed = done / dt
            eta = f" · noch ca. {int((total - done) / speed)} s" if speed > 0 and total > done else ""
            self.stats.setText(f"{fmt_bytes(done)} / {fmt_bytes(total)} · {fmt_bytes(speed)}/s{eta}")
        elif kind == "text":
            self.lbl.setText(info[1])
