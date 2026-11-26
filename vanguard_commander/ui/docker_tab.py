# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Docker: Container-Übersicht (JSON), Aktionen, Live-Logs und Docker Compose."""

from __future__ import annotations

import json
import shlex

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QMenu,
    QMessageBox, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..serverinfo import COMPOSE_ACTIONS, compose_command, compose_project_of, compose_scan_command, parse_compose_scan
from .theme import GREEN, RED, YELLOW, button

COLUMNS = [("ID", "ID"), ("Name", "Names"), ("Compose", "_project"), ("Image", "Image"), ("Status", "Status"),
           ("Ports", "Ports")]
COL_NAME, COL_PROJECT, COL_IMAGE = 1, 2, 3


class DockerTab(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self._loading = False
        self._root = False  # im Nur-SSH-Modus erst ohne sudo versuchen (Gruppe docker)
        self._projects: list[dict] = []
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)

        # --- Compose ---------------------------------------------------------------
        box = QGroupBox("🧩 Docker Compose")
        cv = QVBoxLayout(box)
        r1 = QHBoxLayout()
        self.compose_combo = QComboBox()
        self.compose_combo.setMinimumWidth(380)
        r1.addWidget(QLabel("Projekt:"))
        r1.addWidget(self.compose_combo, stretch=1)
        r1.addWidget(button("🔍 Suchen", self.refresh_compose, bg="#2c313a",
                            tip="Compose-Dateien suchen: laufende Projekte, Container-Merkmale und Dateisuche in "
                                "den Suchordnern des Profils (Einstellungen)"))
        r1.addWidget(button("➕ Datei …", self._add_compose_file, bg="#2c313a",
                            tip="Compose-Datei von Hand hinzufügen (wird im Profil gemerkt)"))
        cv.addLayout(r1)
        r2 = QHBoxLayout()
        r2.addWidget(button("⬇️ Pull & Up", lambda: self.compose("pull_up"), bg="#2e7d32", fg="#ffffff",
                            tip="Neue Images laden und geänderte Container neu erstellen"))
        r2.addWidget(button("▶️ Up", lambda: self.compose("up")))
        r2.addWidget(button("🔄 Restart", lambda: self.compose("restart")))
        r2.addWidget(button("⏹️ Down", lambda: self.compose("down"), bg="#5a1e1e"))
        r2.addWidget(button("📋 Status", lambda: self.compose("ps")))
        r2.addWidget(button("📜 Logs", lambda: self.compose("logs")))
        r2.addWidget(button("📡 Logs live", lambda: self.compose("logs_f"), fg=YELLOW))
        r2.addStretch()
        cv.addLayout(r2)
        v.addWidget(box)

        # --- Container -------------------------------------------------------------
        bar = QHBoxLayout()
        for label, act in (("▶️ Starten", "start"), ("⏹️ Stoppen", "stop"), ("🔄 Neustarten", "restart")):
            bar.addWidget(button(label, lambda a=act: self.action(a)))
        bar.addWidget(button("📜 Logs", self.logs))
        bar.addWidget(button("📡 Live-Logs", self.live_logs, fg=YELLOW, tip="Auch per Doppelklick"))
        bar.addWidget(button("⬇️ Image laden", self.pull_images, tip="docker pull für das Image der Auswahl"))
        bar.addWidget(button("🗑️ Entfernen", self.remove, bg="#5a1e1e"))
        bar.addWidget(button("🧹 Aufräumen", self.prune, bg="#3e4451"))
        bar.addStretch()
        self.auto = QCheckBox("Automatisch aktualisieren")
        self.auto.setChecked(True)
        bar.addWidget(self.auto)
        bar.addWidget(button("⚡ Aktualisieren", self.refresh))
        v.addLayout(bar)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels([c[0] for c in COLUMNS])
        self.table.verticalHeader().setVisible(False)
        self.table.setSortingEnabled(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(3, QHeaderView.ResizeMode.Interactive)
        h.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        h.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._follow_project)
        self.table.cellDoubleClicked.connect(lambda *_: self.live_logs())
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context)
        v.addWidget(self.table)
        self.status = QLabel("")
        self.status.setStyleSheet("color: #5c6370;")
        v.addWidget(self.status)
        self.timer = QTimer(self)
        self.timer.setInterval(8000)
        self.timer.timeout.connect(self._auto_refresh)

    # ================================================================== Verbindung
    def on_connected(self):
        self._root = False
        self.refresh(quiet=True)
        self.refresh_compose(quiet=True)
        self.timer.start()

    def on_disconnected(self):
        self.timer.stop()
        self.table.setRowCount(0)
        self.compose_combo.clear()
        self._projects = []
        self.status.setText("")

    def _auto_refresh(self):
        if self.auto.isChecked() and self.isVisible() and not self._loading:
            self.refresh(quiet=True)

    def _cmd(self, cmd: str, **kw):
        self.main.run_cmd(cmd, as_root=self._root, **kw)

    # ================================================================== Container
    def refresh(self, quiet: bool = False):
        if self.main.backend is None:
            return
        self._loading = True
        self._cmd("docker ps -a --format '{{json .}}'", show=False, ask_password=not quiet, on_done=self._fill)

    def _fill(self, res, err):
        self._loading = False
        if res is not None and not res.ok and "permission denied" in res.err.lower() and not self._root:
            self._root = True  # Benutzer ist nicht in der Gruppe docker -> künftig mit sudo
            self.status.setText("Docker braucht sudo – „Aktualisieren“ fragt nach dem sudo-Passwort "
                                "(oder Benutzer in die Gruppe docker aufnehmen).")
            return
        if res is None or not res.ok:
            msg = err or (res.text() if res else "")
            self.status.setText(f"⚠️ {msg.splitlines()[0] if msg else 'Fehler'}")
            return
        sel = set(self.selected())
        self.table.setSortingEnabled(False)
        self.table.setRowCount(0)
        count = running = 0
        for line in res.out.splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            r = self.table.rowCount()
            self.table.insertRow(r)
            up = str(row.get("Status", "")).startswith("Up")
            running += up
            row["_project"] = compose_project_of(str(row.get("Labels", "")))
            for c, (_label, key) in enumerate(COLUMNS):
                it = QTableWidgetItem(str(row.get(key, "")))
                if key == "Status":
                    it.setForeground(QColor(GREEN if up else RED))
                self.table.setItem(r, c, it)
            if row.get("Names") in sel:
                self.table.selectRow(r)
            count += 1
        self.table.setSortingEnabled(True)
        self.status.setText(f"{count} Container, {running} laufen")

    def selected(self, col: int = 1) -> list[str]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        return [self.table.item(r, col).text() for r in rows if self.table.item(r, col)]

    def _need_selection(self) -> list[str]:
        names = self.selected()
        if not names:
            QMessageBox.information(self, "Docker", "Bitte zuerst mindestens einen Container auswählen.")
        return names

    def action(self, verb: str):
        names = self._need_selection()
        if names:
            self._cmd(f"docker {verb} " + " ".join(shlex.quote(n) for n in names),
                      title=f"docker {verb} {' '.join(names)}", on_done=lambda *_: self.refresh(quiet=True))

    def logs(self):
        names = self._need_selection()
        if names:
            self._cmd(f"docker logs --tail 200 {shlex.quote(names[0])} 2>&1", title=f"Logs {names[0]}", focus_log=True)

    def live_logs(self):
        names = self._need_selection()
        for n in names[:4]:
            self.main.open_live(f"Container {n}", f"docker logs -f --tail 200 {shlex.quote(n)}", as_root=self._root)

    def pull_images(self):
        images = sorted(set(self.selected(COL_IMAGE)))
        if not images:
            self._need_selection()
            return
        self._cmd(" && ".join(f"docker pull {shlex.quote(i)}" for i in images), title="docker pull " + " ".join(images),
                  timeout=1800, focus_log=True,
                  on_done=lambda res, _e: res and res.ok and self.main.log(
                      "Images geladen. Laufende Container nutzen das neue Image erst nach Neuerstellung – "
                      "bei Compose-Projekten: „Pull & Up“."))

    def remove(self):
        names = self._need_selection()
        if not names:
            return
        if "vanguard-node" in names:
            QMessageBox.warning(self, "Docker", "vanguard-node ist der Daemon dieser Verbindung. "
                                "Entfernen bitte über die Seitenleiste → Daemon entfernen.")
            return
        if QMessageBox.warning(self, "Container entfernen", "Diese Container endgültig entfernen?\n\n" + "\n".join(names),
                               QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                               QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self._cmd("docker rm -f " + " ".join(shlex.quote(n) for n in names), title="docker rm",
                      on_done=lambda *_: self.refresh(quiet=True))

    def prune(self):
        if QMessageBox.question(self, "Aufräumen", "Gestoppte Container, unbenutzte Netzwerke und hängende Images "
                                "entfernen (docker system prune -f)?") == QMessageBox.StandardButton.Yes:
            self._cmd("docker system prune -f", title="docker system prune", on_done=lambda *_: self.refresh(quiet=True))

    def _context(self, pos):
        names = self.selected()
        if not names:
            return
        m = QMenu(self)
        for label, verb in (("▶️ Starten", "start"), ("⏹️ Stoppen", "stop"), ("🔄 Neustarten", "restart")):
            m.addAction(f"{label} ({len(names)})", lambda v=verb: self.action(v))
        m.addSeparator()
        m.addAction("📡 Live-Logs", self.live_logs)
        m.addAction("📜 Logs (letzte 200 Zeilen)", self.logs)
        m.addAction("⬇️ Image laden (pull)", self.pull_images)
        m.addSeparator()
        m.addAction("🗑️ Entfernen", self.remove)
        m.exec(self.table.viewport().mapToGlobal(pos))

    # ================================================================== Compose
    def refresh_compose(self, quiet: bool = False):
        if self.main.backend is None:
            return
        prof = self.main.current_profile()
        self.compose_combo.clear()
        self.compose_combo.addItem("🔍 suche Compose-Dateien …", None)
        self._cmd(compose_scan_command(prof.get("compose_search_paths") or ["/opt"]), show=False,
                  ask_password=not quiet, timeout=120, on_done=self._fill_compose)

    def _fill_compose(self, res, _err):
        manual = self.main.current_profile().get("compose_files", [])
        found = parse_compose_scan(res.out if res is not None else "", manual)
        current = self.compose_combo.currentData()
        self._projects = found
        self.compose_combo.clear()
        for p in found:
            self.compose_combo.addItem(f"{p['name']}  ·  {p['status']}  ·  {', '.join(p['files'])}", p["files"])
        if not found:
            self.compose_combo.addItem("(nichts gefunden – Suchordner in den Einstellungen prüfen oder „➕ Datei …“)", None)
        idx = self.compose_combo.findData(current) if current else -1
        self.compose_combo.setCurrentIndex(max(idx, 0))

    def _follow_project(self):
        """Container ausgewählt -> passendes Compose-Projekt oben vorauswählen."""
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        if len(rows) != 1 or self.table.item(rows[0], COL_PROJECT) is None:
            return
        project = self.table.item(rows[0], COL_PROJECT).text()
        for i in range(self.compose_combo.count()):
            if project and self.compose_combo.itemText(i).split("  ·  ")[0] == project:
                self.compose_combo.setCurrentIndex(i)
                return

    def _add_compose_file(self):
        name = self.main.current_profile_name()
        if not name:
            return
        path, ok = QInputDialog.getText(self, "Compose-Datei", "Pfad auf dem Server:", text="/opt/docker-compose.yml")
        path = path.strip()
        if not ok or not path.startswith("/"):
            return
        files = self.main.config.profiles[name].setdefault("compose_files", [])
        if path not in files:
            files.append(path)
            self.main.config.save()
        self.refresh_compose()

    def compose(self, action: str):
        files = self.compose_combo.currentData()
        if not files:
            QMessageBox.information(self, "Compose", "Kein Compose-Projekt ausgewählt. „🔍 Suchen“ oder „➕ Datei …“.")
            return
        label = COMPOSE_ACTIONS[action][0]
        project = self.compose_combo.currentText().split("  ·  ")[0]
        if action in ("pull_up", "restart", "down"):
            extra = {"pull_up": "Neue Images werden geladen, geänderte Container neu erstellt (kurze Unterbrechung).",
                     "restart": "Alle Container des Projekts werden neu gestartet.",
                     "down": "Alle Container des Projekts werden gestoppt und entfernt (Volumes bleiben)."}[action]
            if QMessageBox.question(self, f"Compose {label}", f"{project}: {label}?\n\n{extra}") \
                    != QMessageBox.StandardButton.Yes:
                return
        cmd = compose_command(files, action)
        if action == "logs_f":
            self.main.open_live(f"Compose {project}", cmd, as_root=self._root)
            return
        if action in ("pull_up", "up", "restart", "down"):
            # lange Aktionen mit sichtbarem Fortschritt; danach Listen auffrischen
            self.main.log(f"compose {label} · {project} (Ausgabe im Live-Fenster)")
            self.main.open_live(f"Compose {label} · {project}", cmd, as_root=self._root,
                                on_finished=lambda _rc: (self.refresh(quiet=True), self.refresh_compose(quiet=True)))
            return
        self._cmd(cmd, title=f"compose {label} · {project}", timeout=1800, focus_log=True,
                  on_done=lambda *_: (self.refresh(quiet=True), self.refresh_compose(quiet=True)))

