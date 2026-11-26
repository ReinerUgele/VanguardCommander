# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Hauptfenster: Profilauswahl, Verbindungsablauf, Tresor-Zugriff, Befehlsausführung."""

from __future__ import annotations

import getpass
import logging
import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QKeySequence, QShortcut
from PyQt6.QtWidgets import QComboBox, QGraphicsBlurEffect, QHBoxLayout, QLabel, QMainWindow, QMessageBox, QTabWidget, QVBoxLayout, QWidget

from .. import APP_TITLE, HOMEPAGE, LICENSE_NAME, RELEASE, VERSION, deploy, keys, paths, remember, serverinfo
from ..backends import SshBackend
from ..config import AppConfig, effective_key_path
from ..privacy import Masker
from ..ssh import AuthNeeded, HostKeyUnknown, SshConnection, SshError
from ..vault import FIELD_LABELS, Vault, VaultError
from .dialogs import MasterPasswordDialog, PasswordDialog, confirm_host_key
from .docker_tab import DockerTab
from .filemanager import FileManagerTab
from .firewall_tab import FirewallTab
from .log_tab import LogTab
from .terminal import TerminalTab
from .theme import GREEN, GREY, RED, YELLOW, button
from .workers import TaskPool, error_text


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_TITLE} {VERSION}" + (f" {RELEASE}" if RELEASE else ""))
        self.resize(1360, 860)
        self.config = AppConfig.load()
        self.vault = Vault()
        self.tasks = TaskPool(self)
        self.conn: SshConnection | None = None
        self.backend = None
        self._session_secrets: dict[tuple[str, str], str] = {}
        self._connecting = False
        self.masker = Masker()                 # Datenschutz-Modus (nur Anzeige)
        self._status_raw = ("⚪ Getrennt", GREY)
        self._mode_raw = ("", "", "")          # Text, Farbe, Tooltip
        self._build()
        self._shortcuts()
        self.alive = QTimer(self)
        self.alive.setInterval(5000)
        self.alive.timeout.connect(self._check_alive)
        self._stat_prev: dict | None = None
        self._stat_busy = False
        self.stat_timer = QTimer(self)
        self.stat_timer.setInterval(5000)
        self.stat_timer.timeout.connect(self._poll_status)
        if not self.config.profiles:
            QTimer.singleShot(300, self._first_start)

    # ================================================================== Aufbau
    def _build(self):
        root = QWidget()
        self.setCentralWidget(root)
        v = QVBoxLayout(root)
        v.setContentsMargins(6, 6, 6, 6)
        hdr = QHBoxLayout()
        hdr.addWidget(QLabel("<b>Profil:</b>"))
        self.prof_combo = QComboBox()
        self.prof_combo.setMinimumWidth(180)
        self._fill_profiles(self.config.active_profile)
        self.prof_combo.currentTextChanged.connect(self._profile_changed)
        hdr.addWidget(self.prof_combo)
        hdr.addWidget(button("⚡ Verbinden", self.connect_node, bg="#2e7d32", fg="#ffffff"))
        hdr.addWidget(button("🔌 Trennen", self.disconnect_node, bg="#8b1e1e", fg="#ffffff"))
        self.status_lbl = QLabel("⚪ Getrennt")
        self.status_lbl.setFont(QFont(self.font().family(), 10, QFont.Weight.Bold))
        hdr.addWidget(self.status_lbl)
        self.mode_lbl = QLabel("")
        hdr.addWidget(self.mode_lbl)
        self.stat_lbl = QLabel("")
        self.stat_lbl.setStyleSheet("margin-left: 10px;")
        hdr.addWidget(self.stat_lbl)
        hdr.addStretch()
        self.key_lbl = QLabel("")
        self.key_lbl.setStyleSheet(f"color: {YELLOW};")
        hdr.addWidget(self.key_lbl)
        self.privacy_btn = button("🕶️", None, bg="#2c313a",
                                  tip="Datenschutz-Modus für Screenshots (Strg+Umschalt+P): blendet Servernamen, "
                                      "IP-Adressen, Benutzer, Profilnamen und lokale Ordner aus")
        self.privacy_btn.setCheckable(True)
        self.privacy_btn.toggled.connect(self.set_privacy)
        hdr.addWidget(self.privacy_btn)
        hdr.addWidget(button("⚙️ Einstellungen", self.open_settings, bg="#2c313a", tip="Strg+,"))
        hdr.addWidget(button("❓", self.show_help, bg="#2c313a", tip="Tastenkürzel (F1)"))
        v.addLayout(hdr)

        self.tabs = QTabWidget()
        self.files = FileManagerTab(self)
        self.docker = DockerTab(self)
        self.firewall = FirewallTab(self)
        self.terminal = TerminalTab(self)
        self.log_tab = LogTab(self)
        self.tabs.addTab(self.files, "📁 Dateien")
        self.tabs.addTab(self.docker, "🐳 Docker")
        self.tabs.addTab(self.firewall, "🔥 Firewall")
        self.tabs.addTab(self.terminal, "💻 Terminal")
        self.tabs.addTab(self.log_tab, "📋 Protokoll")
        self.tabs.currentChanged.connect(self._tab_changed)
        v.addWidget(self.tabs)
        self._update_key_label()

    def _shortcuts(self):
        def sc(keys, fn):
            QShortcut(QKeySequence(keys), self).activated.connect(fn)

        sc("F5", self._refresh_current)
        sc("F1", self.show_help)
        sc("Ctrl+K", self.connect_node)
        sc("Ctrl+Shift+K", self.disconnect_node)
        sc("Ctrl+,", self.open_settings)
        sc("Ctrl+PgDown", lambda: self.tabs.setCurrentIndex((self.tabs.currentIndex() + 1) % self.tabs.count()))
        sc("Ctrl+PgUp", lambda: self.tabs.setCurrentIndex((self.tabs.currentIndex() - 1) % self.tabs.count()))
        for i in range(5):
            sc(f"Ctrl+{i + 1}", lambda i=i: self._goto_tab(i))
        sc("Ctrl+P", lambda: (self.prof_combo.setFocus(), self.prof_combo.showPopup()))
        sc("Ctrl+Shift+P", self.privacy_btn.toggle)

    def _goto_tab(self, idx: int):
        self.tabs.setCurrentIndex(idx)
        w = self.tabs.currentWidget()
        if w is self.terminal:
            if self.terminal.term is not None:
                self.terminal.term.setFocus()
            elif self.backend is not None:
                self.terminal.start()
        elif w is self.files:
            self.files.focus_table(False)

    def show_help(self):
        mod = "Cmd" if sys.platform == "darwin" else "Strg"
        rows = [
            ("Allgemein", ""),
            (f"{mod}+K / {mod}+Umschalt+K", "Verbinden / Trennen"),
            (f"{mod}+P", "Profil wählen"),
            (f"{mod}+1 … 5", "Reiter Dateien, Docker, Firewall, Terminal, Protokoll"),
            (f"{mod}+Bild↑ / Bild↓", "Vorheriger / nächster Reiter (auch aus dem Terminal)"),
            (f"{mod}+,", "Einstellungen"),
            (f"{mod}+Umschalt+P", "Datenschutz-Modus für Screenshots ein/aus"),
            ("F5", "Aktualisieren"),
            ("Dateien", ""),
            ("Tab", "Zwischen lokal und Server wechseln"),
            ("Enter / Rücktaste / Alt+↑", "Öffnen / eine Ebene höher"),
            ("F2", "Umbenennen"),
            ("F4 / Umschalt+F4", "Im Editor öffnen / neue Datei"),
            (f"{mod}+→ / {mod}+←", "Hochladen (links → Server) / Herunterladen (Server → links)"),
            ("F6", "Auswahl zur anderen Seite übertragen"),
            ("F7", "Neuer Ordner"),
            ("Entf / F8", "Löschen (lokal: Papierkorb)"),
            (f"{mod}+L / {mod}+F", "Pfadfeld / Filter (Esc zurück zur Liste)"),
            (f"{mod}+Umschalt+C", "Pfad der Auswahl kopieren"),
            ("Alt+L", "Laufwerk / Ort wählen (links)"),
            (f"{mod}+A", "Alles markieren"),
            ("Terminal", ""),
            (f"{mod}+Umschalt+T / W", "Neues Terminal / Terminal schließen"),
            ("Maus markieren, " + ("Cmd+C / Cmd+V" if mod == "Cmd" else "Strg+Umschalt+C / V"), "Kopieren / Einfügen"),
            ("Umschalt+Bild↑ / ↓, Mausrad", "Zurückblättern"),
            ("Alle anderen Tasten", "gehen an den Server (Strg+C, F-Tasten, Tab …)"),
        ]
        html_rows = "".join(
            f"<tr><td colspan=2><br><b style='color:{GREEN};'>{a}</b></td></tr>" if not b else
            f"<tr><td style='padding-right:18px;'><code>{a}</code></td><td>{b}</td></tr>" for a, b in rows)
        box = QMessageBox(self)
        box.setWindowTitle("Tastenkürzel")
        box.setTextFormat(Qt.TextFormat.RichText)
        from ..applog import log_file
        box.setText(f"<table>{html_rows}</table><p style='color:{GREY};'>Version {VERSION} {RELEASE} · Logdatei: "
                    f"{self.masker.text(str(log_file()))}</p>"
                    f"<p style='color:{GREY};'>Vanguard Commander ist freie Software ({LICENSE_NAME}) und wird "
                    f"<b>ohne jede Gewährleistung</b> bereitgestellt. Quellcode und Lizenz: "
                    f"<a href='{HOMEPAGE}' style='color:{GREEN};'>{HOMEPAGE}</a></p>")
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        box.exec()

    def _fill_profiles(self, select: str = ""):
        self.prof_combo.blockSignals(True)
        self.prof_combo.clear()
        self.prof_combo.addItems(list(self.config.profiles))
        if select in self.config.profiles:
            self.prof_combo.setCurrentText(select)
        self.prof_combo.blockSignals(False)

    def _first_start(self):
        QMessageBox.information(self, APP_TITLE, "Willkommen! Lege zuerst ein Server-Profil an "
                                "(Host, SSH-Benutzer). Danach kannst du den SSH-Schlüssel hinterlegen, "
                                "damit kein Passwort mehr nötig ist.")
        self.open_settings()

    # ================================================================== Profil
    def current_profile_name(self) -> str:
        return self.prof_combo.currentText()

    def current_profile(self) -> dict:
        return self.config.profile(self.current_profile_name())

    def _profile_changed(self, name: str):
        if self.conn is not None:
            self.disconnect_node()
        self.config.active_profile = name
        self.config.save()
        self.files.rebuild_sidebar()
        self._update_key_label()

    def _update_key_label(self):
        prof = self.current_profile()
        if not prof:
            self.key_lbl.setText("")
        elif prof.get("auth") == "password":
            self.key_lbl.setText("🔒 Anmeldung per Passwort")
        else:
            self.key_lbl.setText(f"🔑 {keys.describe_key(effective_key_path(prof))}")

    # ================================================================== Ausgabe
    def log(self, text: str, error: bool = False):
        self.log_tab.info(text, error)
        logging.getLogger("vanguard.ui").log(logging.WARNING if error else logging.INFO, "%s", text)

    def _status(self, text: str, color: str):
        self._status_raw = (text, color)
        self.status_lbl.setText(self.masker.text(text))
        self.status_lbl.setStyleSheet(f"color: {color}; font-weight: bold;")

    def _mode(self, text: str, color: str = "", tip: str = ""):
        self._mode_raw = (text, color, tip)
        m = self.masker.text
        self.mode_lbl.setText(f"<span style='color:{color};'>{m(text)}</span>" if text else "")
        self.mode_lbl.setToolTip(m(tip))

    # ================================================================== Datenschutz-Modus
    def privacy_terms(self) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        for name, prof in self.config.profiles.items():
            pairs += [(name, "Mein Server"), (prof.get("host", ""), "server.example.com"),
                      (prof.get("ssh_user", ""), "admin")]
        if self.conn is not None and self.conn.home:
            pairs.append((self.conn.home, "/home/admin"))
        info = getattr(self.backend, "info", {}) or {}
        if info.get("hostname"):
            pairs.append((str(info["hostname"]), "server"))
        try:
            pairs += [(str(Path.home()), "~"), (getpass.getuser(), "user")]
        except Exception:  # noqa: BLE001
            pass
        return pairs

    def set_privacy(self, on: bool):
        self.masker.enabled = on
        self.masker.set_terms(self.privacy_terms())
        f = self.files
        for w in (self.prof_combo, f.l_path, f.l_table, f.l_status, f.r_path, f.l_places):
            if on:
                blur = QGraphicsBlurEffect(w)
                blur.setBlurRadius(9)
                w.setGraphicsEffect(blur)
            else:
                w.setGraphicsEffect(None)
        self._status(*self._status_raw)
        self._mode(*self._mode_raw)
        f.refresh_remote()
        self.terminal.refresh_privacy()
        self.log_tab.rebuild()
        if self.privacy_btn.isChecked() != on:
            self.privacy_btn.setChecked(on)

    # ================================================================== Tresor / Passwörter
    def unlock_vault(self, create: bool = False, parent=None) -> Vault | None:
        v = self.vault
        if v.is_unlocked:
            return v
        parent = parent or self
        if v.exists():
            stored = remember.load() if remember.available() else None
            if stored:
                try:
                    v.unlock(stored)  # „Auf diesem Rechner merken“ ist eingeschaltet
                    return v
                except VaultError:
                    remember.clear()
                    self.log("Das gemerkte Master-Passwort passt nicht mehr – bitte neu eingeben.", error=True)
            err = ""
            for _ in range(3):
                dlg = MasterPasswordDialog(parent, create=False, error=err)
                if not dlg.exec():
                    return None
                try:
                    v.unlock(dlg.password())
                    self._apply_remember(dlg, parent)
                    return v
                except VaultError as exc:
                    err = str(exc)
            return None
        if not create:
            return None
        dlg = MasterPasswordDialog(parent, create=True)
        if not dlg.exec():
            return None
        try:
            v.create(dlg.password())
        except VaultError as exc:
            QMessageBox.warning(parent, "Tresor", str(exc))
            return None
        self._apply_remember(dlg, parent)
        return v

    def _apply_remember(self, dlg, parent):
        if not dlg.wants_remember():
            return
        try:
            remember.save(dlg.password())
            self.log(f"Master-Passwort gemerkt ({remember.method()}).")
        except remember.RememberError as exc:
            QMessageBox.warning(parent, "Tresor", f"Konnte das Master-Passwort nicht merken: {exc}")

    def stored_secret(self, field: str, profile: str | None = None, parent=None) -> str:
        profile = profile or self.current_profile_name()
        if (profile, field) in self._session_secrets:
            return self._session_secrets[(profile, field)]
        if self.vault.exists():
            v = self.unlock_vault(parent=parent)
            if v is not None:
                value = v.get(profile, field)
                if value:
                    self._session_secrets[(profile, field)] = value
                    return value
        return ""

    def secret(self, field: str, prompt: str, profile: str | None = None, parent=None,
               fresh: bool = False, offer_store: bool = True) -> str | None:
        """Gespeichertes Passwort holen oder einmal fragen (optional im Tresor ablegen)."""
        profile = profile or self.current_profile_name()
        if not fresh:
            value = self.stored_secret(field, profile, parent)
            if value:
                return value
        res = PasswordDialog.ask(parent or self, FIELD_LABELS.get(field, "Passwort"), prompt, offer_store)
        if res is None:
            return None
        value, store = res
        self._session_secrets[(profile, field)] = value
        if store:
            v = self.unlock_vault(create=True, parent=parent)
            if v is not None:
                v.set(profile, field, value)
        return value

    def forget_secret(self, field: str, profile: str | None = None):
        self._session_secrets.pop((profile or self.current_profile_name(), field), None)

    # ================================================================== SSH-Hilfen
    def run_ssh(self, prof: dict, creds: dict, work, on_ok, on_fail, parent=None):
        """Baut eine SSH-Verbindung im Hintergrund auf (mit Rückfrage bei unbekanntem Server)."""
        conn = SshConnection(prof, paths.known_hosts_file())

        def job():
            conn.connect(**creds)
            try:
                return work(conn)
            except BaseException:
                conn.close()
                raise

        def failed(exc):
            if isinstance(exc, HostKeyUnknown):
                if confirm_host_key(parent or self, exc):
                    self.run_ssh(prof, {**creds, "accept_fp": exc.fingerprint}, work, on_ok, on_fail, parent)
                else:
                    on_fail(SshError("Abgebrochen – der Server-Schlüssel wurde nicht bestätigt."))
                return
            on_fail(exc)

        self.tasks.run(job, on_ok=on_ok, on_fail=failed)

    # ================================================================== Verbinden
    def connect_node(self):
        if self._connecting:
            return
        name, prof = self.current_profile_name(), dict(self.current_profile())
        if not prof or not prof.get("host") or not prof.get("ssh_user"):
            QMessageBox.information(self, "Profil unvollständig", "Bitte im Profil Host und SSH-Benutzer eintragen.")
            self.open_settings()
            return
        self.disconnect_node(quiet=True)
        creds: dict = {}
        target = f"{prof['ssh_user']}@{prof['host']}"
        if prof["auth"] == "password":
            pw = self.secret("ssh_password", f"SSH-Passwort für {target}:", name)
            if pw is None:
                return
            creds["password"] = pw
        # Bei Schlüsseln wird zuerst der SSH-Agent versucht (Windows-Dienst „OpenSSH Authentication Agent“,
        # Pageant, macOS/Linux ssh-agent). Nur wenn das nicht reicht, kommt die Passphrase aus dem Tresor.
        self._connecting = True
        self._status(f"🟡 Verbinde mit {target} …", YELLOW)
        self._connect_try(name, prof, creds)

    def _connect_try(self, name: str, prof: dict, creds: dict):
        self.run_ssh(prof, creds, lambda c: c,
                     on_ok=lambda c: self._ssh_ready(name, prof, c, creds),
                     on_fail=lambda e: self._connect_failed(name, prof, creds, e))

    def _connect_failed(self, name: str, prof: dict, creds: dict, exc):
        target = f"{prof['ssh_user']}@{prof['host']}"
        if isinstance(exc, AuthNeeded):
            if exc.kind == "key_passphrase":
                kp = self.secret("key_passphrase", f"{exc}\nPassphrase eingeben:", name,
                                 fresh="key_passphrase" in creds)
                if kp:
                    return self._connect_try(name, prof, {**creds, "key_passphrase": kp})
            else:
                go = True
                if prof["auth"] == "key":
                    go = QMessageBox.question(
                        self, "Anmeldung", f"{exc}\n\nEinmalig mit Passwort anmelden? Danach kann der Schlüssel "
                        "automatisch hinterlegt werden.") == QMessageBox.StandardButton.Yes
                if go:
                    pw = self.secret("ssh_password", f"SSH-Passwort für {target}:", name,
                                     fresh=bool(creds.get("password")), offer_store=prof["auth"] == "password")
                    if pw:
                        return self._connect_try(name, prof, {**creds, "password": pw,
                                                              "force_password": prof["auth"] == "key"})
        if creds.get("password"):
            self.forget_secret("ssh_password", name)
        self._connecting = False
        msg = error_text(exc)
        self._status("🔴 Nicht verbunden", RED)
        self.log(f"Verbindung zu {target} fehlgeschlagen: {msg}", error=True)
        if not isinstance(exc, AuthNeeded):
            QMessageBox.warning(self, "Verbindung fehlgeschlagen", msg)

    def _ssh_ready(self, name: str, prof: dict, conn: SshConnection, creds: dict):
        if name != self.current_profile_name():
            conn.close()
            self._connecting = False
            return
        self.conn = conn
        self.log(f"SSH verbunden: {prof['ssh_user']}@{prof['host']} (Home {conn.home})")
        if prof.get("daemon_mode") == "auto":
            self._deploy(name, prof, self._session_secrets.get((name, "sudo_password"), ""))
        else:
            self._set_backend(SshBackend(conn, "Daemon im Profil ausgeschaltet"))
        if creds.get("force_password"):
            QTimer.singleShot(800, lambda: self._offer_key_install(name))

    def _deploy(self, name: str, prof: dict, sudo_pw: str = "", force: bool = False):
        conn = self.conn
        if conn is None:
            return
        self._status("🟡 Prüfe Daemon …", YELLOW)

        def job(task):
            return deploy.ensure_daemon(conn, prof, sudo_pw, log=lambda m: task.progress.emit(m), force=force)

        self.tasks.run(job, with_task=True, on_progress=lambda m: self.log(str(m)),
                       on_ok=lambda b: self._set_backend(b) if self.conn is conn else None,
                       on_fail=lambda e: self._deploy_failed(name, prof, conn, e, sudo_pw, force))

    def _deploy_failed(self, name, prof, conn, exc, sudo_pw, force):
        if self.conn is not conn:
            return
        target = f"{prof['ssh_user']}@{prof['host']}"
        if isinstance(exc, deploy.NeedSudo):
            if sudo_pw:
                self.forget_secret("sudo_password", name)
            pw = self.secret("sudo_password", f"{exc}\nsudo-Passwort für {target}, um den Daemon-Container "
                                              "zu starten:", name, fresh=bool(sudo_pw))
            if pw:
                return self._deploy(name, prof, pw, force)
            reason = "Docker braucht sudo – kein Passwort angegeben"
        elif isinstance(exc, deploy.DockerUnavailable):
            reason = str(exc)
            self.log(f"{exc} → weiter im Nur-SSH-Modus (Befehle als {prof['ssh_user']}, root per sudo).")
        else:
            reason = f"Daemon-Start fehlgeschlagen: {error_text(exc)}"
            self.log(reason, error=True)
            QMessageBox.warning(self, "Daemon", f"{reason}\n\nDer Commander arbeitet jetzt im Nur-SSH-Modus.")
        self._set_backend(SshBackend(conn, reason))

    def _set_backend(self, backend):
        self.backend = backend
        self._connecting = False
        prof = self.current_profile()
        self._status(f"🟢 Verbunden ({prof.get('ssh_user')}@{prof.get('host')})", GREEN)
        self.masker.set_terms(self.privacy_terms())
        if backend.kind == "daemon":
            self._mode(f"🛡️ {backend.label}", GREEN)
        else:
            self._mode(f"🔸 {backend.label}", YELLOW, backend.reason)
        self.files.on_connected()
        self.docker.on_connected()
        self.alive.start()
        self._stat_prev = None
        self._poll_status()
        self.stat_timer.start()
        current = self.tabs.currentWidget()
        if current is self.terminal and not self.terminal.is_running():
            self.terminal.start()
        elif current is self.firewall:
            self.firewall.refresh()

    def disconnect_node(self, quiet: bool = False):
        self.alive.stop()
        self.stat_timer.stop()
        self.stat_lbl.setText("")
        self.stat_lbl.setToolTip("")
        self.terminal.stop()
        conn, self.conn, self.backend = self.conn, None, None
        if conn is not None:
            conn.close()
            if not quiet:
                self.log("Verbindung getrennt.")
        self._connecting = False
        self._status("⚪ Getrennt", GREY)
        self._mode("")
        self.files.on_disconnected()
        self.docker.on_disconnected()
        self.firewall.on_disconnected()

    # ================================================================== Server-Status
    def _poll_status(self):
        b = self.backend
        if b is None or self._stat_busy:
            return
        self._stat_busy = True

        def ok(res):
            self._stat_busy = False
            if self.backend is b and res.ok:
                cur = serverinfo.parse_status(res.out)
                cpu = serverinfo.cpu_percent(self._stat_prev, cur)
                self._stat_prev = cur
                self._show_status(cur, cpu)

        def fail(_exc):
            self._stat_busy = False

        self.tasks.run(b.exec, serverinfo.STATUS_CMD, timeout=15, quiet=True, on_ok=ok, on_fail=fail)

    def _show_status(self, info: dict, cpu: float | None):
        colors = {"ok": GREEN, "warn": YELLOW, "crit": RED}
        parts, tip = [], []

        def span(text: str, pct: float) -> str:
            return f"<span style='color:{colors[serverinfo.level(pct)]};'>{text}</span>"

        gb = lambda kb: f"{kb / 1024 / 1024:.1f}".replace(".", ",")  # noqa: E731
        if cpu is not None:
            parts.append(span(f"CPU {cpu:.0f} %", cpu))
        if "mem_total_kb" in info and "mem_avail_kb" in info:
            used = info["mem_total_kb"] - info["mem_avail_kb"]
            pct = 100 * used / max(info["mem_total_kb"], 1)
            parts.append(span(f"RAM {gb(used)}/{gb(info['mem_total_kb'])} GB", pct))
        if "disk_pct" in info:
            parts.append(span(f"/ {info['disk_pct']} %", info["disk_pct"]))
            tip.append(f"Platte /: {gb(info['disk_used_kb'])} von {gb(info['disk_size_kb'])} GB belegt")
        if "load1" in info:
            ncpu = max(info.get("ncpu", 1), 1)
            parts.append(span(f"Last {info['load1']:.2f}".replace(".", ","), 100 * info["load1"] / ncpu))
            tip.append(f"Last (1 min) bei {ncpu} Kernen – ab {ncpu} ist der Server voll ausgelastet")
        self.stat_lbl.setText("🖥️ " + f" <span style='color:{GREY};'>·</span> ".join(parts))
        self.stat_lbl.setToolTip("\n".join(tip + ["Aktualisierung alle 5 s"]))

    def _check_alive(self):
        if self.conn is not None and not self.conn.is_alive():
            self.disconnect_node(quiet=True)
            self._status("🔴 Verbindung verloren", RED)
            self.log("Die SSH-Verbindung ist abgebrochen.", error=True)

    def redeploy_daemon(self):
        if self.conn is None:
            return
        if QMessageBox.question(self, "Daemon", "Daemon-Container neu hochladen und neu starten?") \
                == QMessageBox.StandardButton.Yes:
            name = self.current_profile_name()
            self._deploy(name, dict(self.current_profile()), self._session_secrets.get((name, "sudo_password"), ""),
                         force=True)

    def remove_daemon(self):
        if self.conn is None:
            return
        if QMessageBox.question(self, "Daemon entfernen", "Den Container vanguard-node stoppen und entfernen? "
                                "(Die Dateien in ~/.vanguard bleiben.)") != QMessageBox.StandardButton.Yes:
            return
        conn = self.conn
        sudo_pw = self._session_secrets.get((self.current_profile_name(), "sudo_password"), "")
        self.tasks.run(deploy.remove_daemon, conn, sudo_pw,
                       on_ok=lambda msg: (self.log(msg), self._set_backend(SshBackend(conn, "Daemon entfernt"))),
                       on_fail=lambda e: QMessageBox.warning(self, "Daemon", error_text(e)))

    # ================================================================== Schlüssel
    def _offer_key_install(self, name: str):
        if self.conn is None or name != self.current_profile_name():
            return
        if QMessageBox.question(self, "SSH-Schlüssel", "Du bist mit Passwort angemeldet. Jetzt den SSH-Schlüssel "
                                "auf dem Server hinterlegen, damit künftig kein Passwort mehr nötig ist?") \
                != QMessageBox.StandardButton.Yes:
            return
        prof = self.current_profile()
        pub = self._ensure_key(prof, name, self)
        if pub is None:
            return
        self.tasks.run(keys.install_public_key, self.conn, pub,
                       on_ok=lambda msg: QMessageBox.information(self, "SSH-Schlüssel", f"Schlüssel {msg}. "
                                                                 "Ab der nächsten Verbindung ohne Passwort."),
                       on_fail=lambda e: QMessageBox.warning(self, "SSH-Schlüssel", error_text(e)))

    def _ensure_key(self, prof: dict, name: str, parent) -> str | None:
        key_path = effective_key_path(prof)
        if not key_path.exists():
            if QMessageBox.question(parent, "Schlüssel erzeugen", f"Unter {key_path} liegt noch kein Schlüssel.\n\n"
                                    "Neuen Ed25519-Schlüssel erzeugen?") != QMessageBox.StandardButton.Yes:
                return None
            try:
                keys.generate_ed25519(key_path)
                self.log(f"Neuer SSH-Schlüssel erzeugt: {key_path}")
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(parent, "Schlüssel", error_text(exc))
                return None
        try:
            return keys.read_public_line(key_path)  # .pub-Datei reicht, keine Passphrase nötig
        except Exception:  # noqa: BLE001
            pass
        passphrase = ""
        if keys.key_needs_passphrase(key_path):
            passphrase = self.secret("key_passphrase", f"Passphrase für {key_path.name}:", name, parent) or ""
        try:
            return keys.read_public_line(key_path, passphrase)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(parent, "Schlüssel", error_text(exc))
            return None

    def install_key(self, name: str, prof: dict, parent, on_done=None):
        """Schlüssel per einmaligem Passwort-Login hinterlegen und danach die Schlüssel-Anmeldung testen."""
        pub = self._ensure_key(prof, name, parent)
        if pub is None:
            return
        target = f"{prof['ssh_user']}@{prof['host']}"
        pw = self.secret("ssh_password", f"Einmalig das SSH-Passwort für {target}.\nDamit wird der Schlüssel "
                         "eingetragen – danach ist kein Passwort mehr nötig.", name, parent, fresh=True,
                         offer_store=False)
        if pw is None:
            return
        passphrase = self._session_secrets.get((name, "key_passphrase"), "")
        key_prof = {**prof, "auth": "key"}

        def work(conn):
            msg = keys.install_public_key(conn, pub)
            conn.close()
            test = SshConnection(key_prof, paths.known_hosts_file())
            test.connect(key_passphrase=passphrase)
            test.close()
            return msg

        def ok(msg):
            QMessageBox.information(parent, "SSH-Schlüssel", f"Öffentlicher Schlüssel {msg}.\n"
                                    "Test-Anmeldung mit Schlüssel erfolgreich – ab jetzt kein Passwort mehr nötig.")
            if on_done:
                on_done(True)

        def fail(exc):
            QMessageBox.warning(parent, "SSH-Schlüssel", f"Hat nicht geklappt:\n{error_text(exc)}")
            if on_done:
                on_done(False)

        self.run_ssh(prof, {"password": pw, "force_password": True}, work, ok, fail, parent)

    # ================================================================== Befehle
    def run_cmd(self, cmd: str, on_done=None, *, as_root: bool = False, timeout: int = 600, title: str | None = None,
                show: bool = True, ask_password: bool = True, focus_log: bool = False):
        """Führt einen Befehl über die aktive Verbindung aus. on_done(ExecResult | None, Fehlertext)."""
        b = self.backend
        if b is None:
            if on_done:
                on_done(None, "Nicht verbunden")
            elif show:
                QMessageBox.information(self, "Nicht verbunden", "Bitte zuerst mit einem Server verbinden.")
            return
        pw = ""
        if as_root and not b.is_root:
            name = self.current_profile_name()
            pw = self._session_secrets.get((name, "sudo_password"), "")
            if not pw and ask_password:
                prof = self.current_profile()
                pw = self.secret("sudo_password", f"sudo-Passwort für {prof.get('ssh_user')}@{prof.get('host')}:") or ""
            if not pw:
                if on_done:
                    on_done(None, "sudo-Passwort fehlt")
                return
        if show:
            self.log_tab.command(title or cmd)
            if focus_log:
                self.tabs.setCurrentWidget(self.log_tab)

        def ok(res):
            if show:
                self.log_tab.result(res)
            if as_root and not res.ok and "sudo-Passwort fehlt oder ist falsch" in res.err:
                self.forget_secret("sudo_password")
            if on_done:
                on_done(res, "")

        def fail(exc):
            msg = error_text(exc)
            if show:
                self.log(msg, error=True)
            if on_done:
                on_done(None, msg)

        self.tasks.run(b.exec, cmd, timeout=timeout, as_root=as_root, sudo_password=pw, quiet=not show,
                       on_ok=ok, on_fail=fail)

    def open_live(self, title: str, cmd: str, as_root: bool = False, on_finished=None):
        """Öffnet ein Live-Ausgabe-Fenster (z. B. docker logs -f)."""
        from .livelog import LiveLogWindow
        b = self.backend
        if b is None:
            QMessageBox.information(self, "Nicht verbunden", "Bitte zuerst mit einem Server verbinden.")
            return
        pw = ""
        if as_root and not b.is_root:
            prof = self.current_profile()
            pw = self.secret("sudo_password", f"sudo-Passwort für {prof.get('ssh_user')}@{prof.get('host')}:") or ""
            if not pw:
                return
        LiveLogWindow(self, title, cmd, as_root, pw, on_finished).show()

    # ================================================================== Sonstiges
    def _tab_changed(self, _idx: int):
        w = self.tabs.currentWidget()
        if self.backend is None:
            return
        if w is self.terminal and not self.terminal.is_running():
            self.terminal.start()
        elif w is self.firewall:
            self.firewall.refresh()
        elif w is self.docker:
            self.docker.refresh(quiet=True)

    def _refresh_current(self):
        w = self.tabs.currentWidget()
        if w is self.files:
            self.files.refresh_local()
            self.files.refresh_remote()
        elif w is self.docker:
            self.docker.refresh()
        elif w is self.firewall:
            self.firewall.refresh()

    def open_settings(self):
        from .settings import SettingsDialog
        dlg = SettingsDialog(self)
        if dlg.exec():
            current = self.current_profile_name()
            renamed = dict(dlg.renames)
            select = renamed.get(current, current)
            if select not in self.config.profiles:
                select = self.config.active_profile
            if self.conn is not None and select != current:
                self.disconnect_node()
            self._fill_profiles(select)
            self.config.active_profile = self.current_profile_name()
            self.config.save()
            self.files.rebuild_sidebar()
            self._update_key_label()
            self.masker.set_terms(self.privacy_terms())

    def closeEvent(self, ev):
        self.terminal.stop()
        self.tasks.cancel_all()
        self.disconnect_node(quiet=True)
        self.tasks.wait_all(2000)
        self.vault.lock()
        ev.accept()
