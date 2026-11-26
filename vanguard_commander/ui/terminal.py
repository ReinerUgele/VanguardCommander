# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Echtes SSH-Terminal: interaktive Shell mit PTY über paramiko, Darstellung per pyte (VT100/xterm).

Damit funktionieren sudo (Passwortabfrage), cd, Pipes, Tab-Vervollständigung, nano, top, htop usw.
Kopieren: Text mit der Maus markieren, dann Strg+Umschalt+C (macOS: Cmd+C) oder Rechtsklick.
Einfügen: Strg+Umschalt+V (macOS: Cmd+V) oder Rechtsklick. Strg+C bricht wie gewohnt ab.
"""

from __future__ import annotations

import sys

import pyte
from PyQt6.QtCore import QEvent, QRect, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QFontDatabase, QFontMetrics, QGuiApplication, QKeySequence, QPainter, QShortcut
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QMenu, QMessageBox, QTabWidget, QVBoxLayout, QWidget

from ..termfilter import AcsTranslator
from .theme import button

PALETTE = {
    "black": "#3f4451", "red": "#e06c75", "green": "#98c379", "brown": "#e5c07b", "yellow": "#e5c07b",
    "blue": "#61afef", "magenta": "#c678dd", "cyan": "#56b6c2", "white": "#d7dae0",
    "brightblack": "#5c6370", "brightred": "#ff7a85", "brightgreen": "#b5e890", "brightbrown": "#f0d197",
    "brightyellow": "#f0d197", "brightblue": "#7cc4ff", "brightmagenta": "#de9bf0", "brightcyan": "#6fd3e0",
    "brightwhite": "#ffffff",
}
DEFAULT_FG = QColor("#d7dae0")
DEFAULT_BG = QColor("#14161a")
SELECTION_BG = QColor("#3e5a80")
PAD = 4
MODE_APP_CURSOR = 1 << 5          # DECCKM (pyte speichert private Modi um 5 Bit verschoben)
MODE_BRACKETED_PASTE = 2004 << 5

K = Qt.Key
_SEQ = {
    K.Key_Return.value: b"\r", K.Key_Enter.value: b"\r", K.Key_Backspace.value: b"\x7f",
    K.Key_Tab.value: b"\t", K.Key_Backtab.value: b"\x1b[Z", K.Key_Escape.value: b"\x1b",
    K.Key_Home.value: b"\x1b[H", K.Key_End.value: b"\x1b[F", K.Key_PageUp.value: b"\x1b[5~",
    K.Key_PageDown.value: b"\x1b[6~", K.Key_Insert.value: b"\x1b[2~", K.Key_Delete.value: b"\x1b[3~",
    K.Key_F1.value: b"\x1bOP", K.Key_F2.value: b"\x1bOQ", K.Key_F3.value: b"\x1bOR", K.Key_F4.value: b"\x1bOS",
    K.Key_F5.value: b"\x1b[15~", K.Key_F6.value: b"\x1b[17~", K.Key_F7.value: b"\x1b[18~",
    K.Key_F8.value: b"\x1b[19~", K.Key_F9.value: b"\x1b[20~", K.Key_F10.value: b"\x1b[21~",
    K.Key_F11.value: b"\x1b[23~", K.Key_F12.value: b"\x1b[24~",
}
_ARROWS = {K.Key_Up.value: b"A", K.Key_Down.value: b"B", K.Key_Right.value: b"C", K.Key_Left.value: b"D"}
_CTRL_EXTRA = {K.Key_Space.value: b"\x00", K.Key_BracketLeft.value: b"\x1b", K.Key_Backslash.value: b"\x1c",
               K.Key_BracketRight.value: b"\x1d", K.Key_AsciiCircum.value: b"\x1e", K.Key_Underscore.value: b"\x1f"}
IS_MAC = sys.platform == "darwin"


def _monospace_font(size: int) -> QFont:
    for family in ("Cascadia Mono", "Consolas", "Menlo", "SF Mono", "DejaVu Sans Mono", "Liberation Mono"):
        f = QFont(family, size)
        if f.exactMatch():
            break
    else:
        f = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        f.setPointSize(size)
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setFixedPitch(True)
    return f


class ShellReader(QThread):
    data = pyqtSignal(bytes)
    closed = pyqtSignal()

    def __init__(self, chan):
        super().__init__()
        self.chan = chan

    def run(self):
        try:
            while True:
                chunk = self.chan.recv(65536)
                if not chunk:
                    break
                self.data.emit(chunk)
        except Exception:  # noqa: BLE001 – Kanal geschlossen oder Verbindung weg
            pass
        self.closed.emit()


class TerminalWidget(QWidget):
    send_bytes = pyqtSignal(bytes)
    resized = pyqtSignal(int, int)  # Spalten, Zeilen

    def __init__(self, font_size: int = 11, parent=None, masker=None):
        super().__init__(parent)
        self.masker = masker   # Datenschutz-Modus (privacy.Masker) – nur Anzeige
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.setMinimumSize(300, 150)
        self.cols, self.rows = 100, 30
        self.screen = pyte.HistoryScreen(self.cols, self.rows, history=5000, ratio=0.5)
        self.screen.write_process_input = self._answer
        self.stream = pyte.ByteStream(self.screen)
        self._acs = AcsTranslator()  # Rahmen-/Linienzeichen (whiptail, mc, needrestart …)
        self._colors: dict[str, QColor] = {}
        self._sel_a: tuple[int, int] | None = None
        self._sel_b: tuple[int, int] | None = None
        self._repaint = QTimer(self)
        self._repaint.setSingleShot(True)
        self._repaint.setInterval(15)
        self._repaint.timeout.connect(self.update)
        self.set_font_size(font_size)

    # --- Darstellung -----------------------------------------------------------------
    def set_font_size(self, size: int) -> None:
        self.font_size = max(7, min(size, 28))
        self.term_font = _monospace_font(self.font_size)
        self.bold_font = QFont(self.term_font)
        self.bold_font.setBold(True)
        fm = QFontMetrics(self.term_font)
        self.cw = max(1, fm.horizontalAdvance("M"))
        self.ch = max(1, fm.height())
        self.ascent = fm.ascent()
        self._recalc_size()
        self.update()

    def _recalc_size(self) -> None:
        cols = max(20, (self.width() - 2 * PAD) // self.cw)
        rows = max(5, (self.height() - 2 * PAD) // self.ch)
        if (cols, rows) != (self.cols, self.rows):
            self.cols, self.rows = cols, rows
            self.screen.resize(rows, cols)
            self.resized.emit(cols, rows)

    def resizeEvent(self, ev):
        self._recalc_size()
        super().resizeEvent(ev)

    def _color(self, name: str, default: QColor) -> QColor:
        if name == "default":
            return default
        c = self._colors.get(name)
        if c is None:
            hexv = PALETTE.get(name) or (f"#{name}" if len(name) == 6 else "")
            c = QColor(hexv) if hexv else default
            if not c.isValid():
                c = default
            self._colors[name] = c
        return c

    def paintEvent(self, _ev):
        p = QPainter(self)
        p.fillRect(self.rect(), DEFAULT_BG)
        sel = self._sel_range()
        buf = self.screen.buffer
        cols = self.screen.columns
        masking = self.masker is not None and self.masker.enabled
        for y in range(self.screen.lines):
            row = buf[y]
            hidden: set[int] = set()
            if masking:
                line = "".join(row[i].data or " " for i in range(cols))
                for a, b in self.masker.spans(line):
                    hidden.update(range(a, b))
            x = 0
            while x < cols:
                c0 = row[x]
                key = (c0.fg, c0.bg, c0.bold, c0.reverse, c0.underscore, self._in_sel(sel, y, x), x in hidden)
                start = x
                chars = ["•" if x in hidden else c0.data]
                x += 1
                while x < cols:
                    c = row[x]
                    if (c.fg, c.bg, c.bold, c.reverse, c.underscore, self._in_sel(sel, y, x), x in hidden) != key:
                        break
                    chars.append("•" if x in hidden else c.data)
                    x += 1
                fg = self._color(key[0], DEFAULT_FG)
                bg = self._color(key[1], DEFAULT_BG)
                if key[3]:
                    fg, bg = bg, fg
                if key[5]:
                    bg = SELECTION_BG
                rect = QRect(PAD + start * self.cw, PAD + y * self.ch, (x - start) * self.cw, self.ch)
                if bg != DEFAULT_BG:
                    p.fillRect(rect, bg)
                text = "".join(chars)
                if text.strip():
                    p.setFont(self.bold_font if key[2] else self.term_font)
                    p.setPen(fg)
                    p.drawText(rect.x(), rect.y() + self.ascent, text)
                if key[4]:
                    p.setPen(fg)
                    p.drawLine(rect.x(), rect.bottom(), rect.right(), rect.bottom())
        cur = self.screen.cursor
        if not cur.hidden:
            r = QRect(PAD + cur.x * self.cw, PAD + cur.y * self.ch, self.cw, self.ch)
            if self.hasFocus():
                p.fillRect(r, QColor(97, 175, 239, 170))
            else:
                p.setPen(QColor("#61afef"))
                p.drawRect(r.adjusted(0, 0, -1, -1))
        p.end()

    # --- Daten -------------------------------------------------------------------------
    def feed(self, data: bytes) -> None:
        self.scroll_to_bottom()
        self.stream.feed(self._acs.feed(data))
        if not self._repaint.isActive():
            self._repaint.start()

    def _answer(self, text: str) -> None:
        self.send_bytes.emit(text.encode("utf-8"))

    def reset(self) -> None:
        self.screen.reset()
        self._acs.reset()
        self.clear_selection()
        self.update()

    def scroll_to_bottom(self) -> None:
        hist = self.screen.history
        guard = 0
        while hist.position < hist.size and guard < 10000:
            self.screen.next_page()
            guard += 1

    # --- Tastatur ----------------------------------------------------------------------
    _LEAVE_KEYS = {K.Key_PageUp.value, K.Key_PageDown.value} | {getattr(K, f"Key_{i}").value for i in range(1, 10)}

    def event(self, ev):
        """Alle Tasten gehören dem Terminal (Strg+C, F-Tasten, …) – außer Strg+1…9, Strg+Bild↑/↓
        (Reiter wechseln) und Strg+Umschalt+T/W (Terminal öffnen/schließen)."""
        if ev.type() == QEvent.Type.ShortcutOverride:
            mods = ev.modifiers()
            if mods & Qt.KeyboardModifier.ControlModifier and (
                    ev.key() in self._LEAVE_KEYS
                    or (mods & Qt.KeyboardModifier.ShiftModifier and ev.key() in (K.Key_T.value, K.Key_W.value))):
                return super().event(ev)
            ev.accept()
            return True
        return super().event(ev)

    def focusNextPrevChild(self, _next: bool) -> bool:
        return False  # Tab gehört dem Terminal (Vervollständigung)

    def keyPressEvent(self, ev):
        key = ev.key()
        mods = ev.modifiers()
        shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
        alt = bool(mods & Qt.KeyboardModifier.AltModifier)
        # Unter macOS meldet Qt die Cmd-Taste als Control und die Ctrl-Taste als Meta.
        term_ctrl = bool(mods & (Qt.KeyboardModifier.MetaModifier if IS_MAC else Qt.KeyboardModifier.ControlModifier))
        shortcut = bool(mods & Qt.KeyboardModifier.ControlModifier) if IS_MAC else (term_ctrl and shift)

        if shortcut and key == K.Key_C.value:
            self.copy_selection()
            return
        if shortcut and key == K.Key_V.value:
            self.paste()
            return
        if shift and key in (K.Key_PageUp.value, K.Key_PageDown.value):
            (self.screen.prev_page if key == K.Key_PageUp.value else self.screen.next_page)()
            self.update()
            return

        seq = None
        if key in _ARROWS:
            seq = (b"\x1bO" if MODE_APP_CURSOR in self.screen.mode else b"\x1b[") + _ARROWS[key]
        elif key in _SEQ:
            seq = _SEQ[key]
        elif term_ctrl and K.Key_A.value <= key <= K.Key_Z.value:
            seq = bytes([key - K.Key_A.value + 1])
        elif term_ctrl and key in _CTRL_EXTRA:
            seq = _CTRL_EXTRA[key]
        elif ev.text():
            seq = ev.text().encode("utf-8")
            if alt and not IS_MAC:
                seq = b"\x1b" + seq
        if seq is None:
            super().keyPressEvent(ev)
            return
        self.clear_selection()
        self.scroll_to_bottom()
        self.send_bytes.emit(seq)

    def inputMethodEvent(self, ev):
        if ev.commitString():
            self.send_bytes.emit(ev.commitString().encode("utf-8"))
        ev.accept()

    def paste(self) -> None:
        text = QGuiApplication.clipboard().text()
        if not text:
            return
        data = text.replace("\r\n", "\r").replace("\n", "\r").encode("utf-8")
        if MODE_BRACKETED_PASTE in self.screen.mode:
            data = b"\x1b[200~" + data + b"\x1b[201~"
        self.scroll_to_bottom()
        self.send_bytes.emit(data)

    # --- Maus / Markierung -------------------------------------------------------------
    def _cell(self, pos) -> tuple[int, int]:
        x = min(max(0, (pos.x() - PAD) // self.cw), self.screen.columns - 1)
        y = min(max(0, (pos.y() - PAD) // self.ch), self.screen.lines - 1)
        return int(y), int(x)

    def _sel_range(self):
        if self._sel_a is None or self._sel_b is None or self._sel_a == self._sel_b:
            return None
        return (self._sel_a, self._sel_b) if self._sel_a <= self._sel_b else (self._sel_b, self._sel_a)

    @staticmethod
    def _in_sel(sel, y: int, x: int) -> bool:
        if sel is None:
            return False
        (y0, x0), (y1, x1) = sel
        return (y0, x0) <= (y, x) < (y1, x1)

    def clear_selection(self) -> None:
        if self._sel_a is not None:
            self._sel_a = self._sel_b = None
            self.update()

    def selected_text(self) -> str:
        sel = self._sel_range()
        if sel is None:
            return ""
        (y0, x0), (y1, x1) = sel
        lines = []
        for y in range(y0, y1 + 1):
            row = self.screen.buffer[y]
            a = x0 if y == y0 else 0
            b = x1 if y == y1 else self.screen.columns
            lines.append("".join(row[x].data for x in range(a, b)).rstrip())
        return "\n".join(lines)

    def copy_selection(self) -> None:
        text = self.selected_text()
        if text:
            QGuiApplication.clipboard().setText(text)

    def mousePressEvent(self, ev):
        self.setFocus()
        if ev.button() == Qt.MouseButton.LeftButton:
            self._sel_a = self._sel_b = self._cell(ev.position().toPoint())
            self.update()
        elif ev.button() == Qt.MouseButton.MiddleButton:
            self.paste()

    def mouseMoveEvent(self, ev):
        if ev.buttons() & Qt.MouseButton.LeftButton and self._sel_a is not None:
            y, x = self._cell(ev.position().toPoint())
            self._sel_b = (y, x + 1)
            self.update()

    def mouseDoubleClickEvent(self, ev):
        y, x = self._cell(ev.position().toPoint())
        row = self.screen.buffer[y]
        is_word = lambda c: c.data.strip() and c.data not in " \t\"'()[]{}<>|;,"  # noqa: E731
        if not is_word(row[x]):
            return
        a = x
        while a > 0 and is_word(row[a - 1]):
            a -= 1
        b = x
        while b < self.screen.columns - 1 and is_word(row[b + 1]):
            b += 1
        self._sel_a, self._sel_b = (y, a), (y, b + 1)
        self.update()

    def wheelEvent(self, ev):
        steps = ev.angleDelta().y()
        if steps > 0:
            self.screen.prev_page()
        elif steps < 0:
            self.screen.next_page()
        self.update()

    def contextMenuEvent(self, ev):
        m = QMenu(self)
        a_copy = m.addAction("Kopieren")
        a_copy.setEnabled(bool(self.selected_text()))
        a_copy.triggered.connect(self.copy_selection)
        m.addAction("Einfügen").triggered.connect(self.paste)
        m.exec(ev.globalPos())


class TerminalSession(QWidget):
    """Eine Shell-Sitzung (ein Unterreiter)."""

    state_changed = pyqtSignal()

    def __init__(self, font_size: int, masker=None):
        super().__init__()
        self.chan = None
        self.reader: ShellReader | None = None
        self.label = "neu"
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        self.term = TerminalWidget(font_size, masker=masker)
        self.term.send_bytes.connect(self.send)
        self.term.resized.connect(self._resize_pty)
        v.addWidget(self.term)

    def is_running(self) -> bool:
        return self.chan is not None and not self.chan.closed

    def start(self, conn) -> str:
        self.stop()
        self.term.reset()
        self.chan = conn.open_shell(self.term.cols, self.term.rows)
        self.reader = ShellReader(self.chan)
        self.reader.data.connect(self.term.feed)
        self.reader.closed.connect(self._on_closed)
        self.reader.start()
        self.label = f"{conn.profile.get('ssh_user', '')}@{conn.profile.get('host', '')}"
        self.state_changed.emit()
        return self.label

    def stop(self) -> None:
        chan, self.chan = self.chan, None
        if chan is not None:
            try:
                chan.close()
            except Exception:  # noqa: BLE001
                pass
        if self.reader is not None:
            self.reader.wait(1500)
            self.reader = None

    def _on_closed(self) -> None:
        if self.chan is not None:
            self.chan = None
            self.term.feed(b"\r\n\x1b[33m[Sitzung beendet - \"Neue Sitzung\" startet eine neue]\x1b[0m\r\n")
        self.state_changed.emit()

    def send(self, data: bytes) -> None:
        if self.chan is not None and not self.chan.closed:
            try:
                self.chan.sendall(data)
            except Exception:  # noqa: BLE001
                self._on_closed()

    def _resize_pty(self, cols: int, rows: int) -> None:
        if self.chan is not None and not self.chan.closed:
            try:
                self.chan.resize_pty(width=cols, height=rows)
            except Exception:  # noqa: BLE001
                pass


class TerminalTab(QWidget):
    """Mehrere Terminal-Sitzungen als Unterreiter (Strg+Umschalt+T neu, Strg+Umschalt+W schließen)."""

    def __init__(self, main):
        super().__init__()
        self.main = main
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        bar = QHBoxLayout()
        bar.addWidget(button("➕ Neues Terminal", self.new_session, bg="#2e7d32", fg="#ffffff",
                             tip="Weitere Sitzung als Reiter (Strg+Umschalt+T)"))
        bar.addWidget(button("🔄 Neu verbinden", self.restart_current, bg="#3e4451",
                             tip="Aktuelle Sitzung neu starten"))
        bar.addWidget(button("🔑 sudo-Passwort senden", self.send_sudo_password, bg="#2b2d42", fg="#e5c07b",
                             tip="Schickt das gespeicherte sudo-Passwort an die laufende Abfrage (aus dem Tresor)."))
        bar.addWidget(button("📋 Kopieren", lambda: self.term and self.term.copy_selection(), bg="#2c313a"))
        bar.addWidget(button("📥 Einfügen", lambda: self.term and self.term.paste(), bg="#2c313a"))
        bar.addWidget(button("A−", lambda: self._font(-1), bg="#2c313a"))
        bar.addWidget(button("A+", lambda: self._font(+1), bg="#2c313a"))
        bar.addStretch()
        self.status = QLabel("Keine Sitzung")
        self.status.setStyleSheet("color: #5c6370;")
        bar.addWidget(self.status)
        v.addLayout(bar)
        self.sessions = QTabWidget()
        self.sessions.setTabsClosable(True)
        self.sessions.setMovable(True)
        self.sessions.setDocumentMode(True)
        self.sessions.tabCloseRequested.connect(self.close_session)
        self.sessions.currentChanged.connect(lambda _i: self._update_status())
        v.addWidget(self.sessions)
        QShortcut(QKeySequence("Ctrl+Shift+T"), self).activated.connect(self.new_session)
        QShortcut(QKeySequence("Ctrl+Shift+W"), self).activated.connect(
            lambda: self.close_session(self.sessions.currentIndex()))

    # --- Zugriff für das Hauptfenster -----------------------------------------------------
    @property
    def current(self) -> TerminalSession | None:
        w = self.sessions.currentWidget()
        return w if isinstance(w, TerminalSession) else None

    @property
    def term(self) -> TerminalWidget | None:
        return self.current.term if self.current else None

    def _all(self) -> list[TerminalSession]:
        return [self.sessions.widget(i) for i in range(self.sessions.count())]

    def is_running(self) -> bool:
        return any(s.is_running() for s in self._all())

    def start(self) -> None:
        """Beim Öffnen des Reiters: aktuelle Sitzung starten oder eine neue anlegen."""
        cur = self.current
        if cur is not None and not cur.is_running():
            self._start_session(cur)
        elif cur is None:
            self.new_session()

    def stop(self) -> None:
        for s in self._all():
            s.stop()
        self._update_status()

    # --- Sitzungen ------------------------------------------------------------------------
    def _conn(self):
        conn = self.main.conn
        if conn is None or not conn.is_alive():
            QMessageBox.information(self, "Terminal", "Bitte zuerst mit einem Server verbinden.")
            return None
        return conn

    def new_session(self) -> None:
        conn = self._conn()
        if conn is None:
            return
        sess = TerminalSession(self.main.config.data.get("terminal_font_size", 11), masker=self.main.masker)
        sess.state_changed.connect(lambda s=sess: self._session_changed(s))
        idx = self.sessions.addTab(sess, "…")
        self.sessions.setCurrentIndex(idx)
        QTimer.singleShot(0, lambda: self._start_session(sess))  # erst nach dem Layout (richtige Größe)

    def _start_session(self, sess: TerminalSession) -> None:
        conn = self._conn()
        if conn is None:
            return
        try:
            sess.start(conn)
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"Fehler: {exc}")
            return
        sess.term.setFocus()

    def restart_current(self) -> None:
        if self.current is not None:
            self._start_session(self.current)
        else:
            self.new_session()

    def close_session(self, idx: int) -> None:
        w = self.sessions.widget(idx)
        if not isinstance(w, TerminalSession):
            return
        if w.is_running() and QMessageBox.question(self, "Terminal schließen",
                                                   "Laufende Sitzung beenden?") != QMessageBox.StandardButton.Yes:
            return
        w.stop()
        self.sessions.removeTab(idx)
        w.deleteLater()
        self._update_status()
        if self.term:
            self.term.setFocus()

    def _session_changed(self, sess: TerminalSession) -> None:
        idx = self.sessions.indexOf(sess)
        if idx >= 0:
            mark = "🟢" if sess.is_running() else "⚪"
            self.sessions.setTabText(idx, f"{mark} {idx + 1}: {self.main.masker.text(sess.label)}")
        self._update_status()

    def _update_status(self) -> None:
        running = sum(s.is_running() for s in self._all())
        if not self.sessions.count():
            self.status.setText("Keine Sitzung")
            self.status.setStyleSheet("color: #5c6370;")
        else:
            self.status.setText(f"{running} von {self.sessions.count()} Sitzung(en) aktiv")
            self.status.setStyleSheet(f"color: {'#98c379' if running else '#e5c07b'};")

    def refresh_privacy(self) -> None:
        for s in self._all():
            self._session_changed(s)
            s.term.update()

    # --- Werkzeuge ------------------------------------------------------------------------
    def _font(self, delta: int) -> None:
        size = self.main.config.data.get("terminal_font_size", 11) + delta
        for s in self._all():
            s.term.set_font_size(size)
        self.main.config.data["terminal_font_size"] = max(7, min(size, 28))
        self.main.config.save()

    def send_sudo_password(self) -> None:
        cur = self.current
        if cur is None or not cur.is_running():
            return
        pw = self.main.secret("sudo_password", "sudo-Passwort (wird an die Abfrage im Terminal geschickt)")
        if pw:
            cur.send(pw.encode("utf-8") + b"\r")
        cur.term.setFocus()
