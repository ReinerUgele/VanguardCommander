# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Einstellungen: Profile, SSH-Schlüssel, Passwort-Tresor, eigene Befehle."""

from __future__ import annotations

import copy
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .. import keys, paths, remember
from ..config import effective_key_path, new_profile, normalize_profile
from ..vault import FIELD_LABELS, VaultError
from .dialogs import MasterPasswordDialog, PasswordDialog
from .theme import button_style, GREEN, GREY, YELLOW, button


class SettingsDialog(QDialog):
    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("⚙️ Einstellungen")
        self.resize(960, 760)
        self.data = copy.deepcopy(main.config.data)
        self.renames: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self.current: str | None = None
        lay = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._profiles_tab(), "🌐 Profile")
        self.tabs.addTab(self._commands_tab(), "⚡ Eigene Befehle")
        self.tabs.addTab(self._vault_tab(), "🔐 Tresor")
        lay.addWidget(self.tabs)
        bar = QHBoxLayout()
        bar.addWidget(QLabel(f"<span style='color:{GREY};'>Ablage: {paths.config_dir()}</span>"))
        bar.addStretch()
        save = QPushButton("💾 Speichern")
        save.setStyleSheet(button_style("#2e7d32"))
        save.clicked.connect(self._save)
        cancel = QPushButton("Abbrechen")
        cancel.clicked.connect(self.reject)
        bar.addWidget(save)
        bar.addWidget(cancel)
        lay.addLayout(bar)
        if main.masker.enabled:
            from PyQt6.QtWidgets import QGraphicsBlurEffect
            for w in (self.plist, self.f_name, self.f_host, self.f_user, self.f_key, self.key_info):
                blur = QGraphicsBlurEffect(w)
                blur.setBlurRadius(9)
                w.setGraphicsEffect(blur)
        names = list(self.data["profiles"])
        self.plist.addItems(names)
        start = main.current_profile_name()
        if start in names:
            self.plist.setCurrentRow(names.index(start))
        elif names:
            self.plist.setCurrentRow(0)
        else:
            self._set_form_enabled(False)

    # ================================================================== Profile
    def _profiles_tab(self) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        left = QVBoxLayout()
        self.plist = QListWidget()
        self.plist.setFixedWidth(210)
        self.plist.currentTextChanged.connect(self._select)
        left.addWidget(self.plist)
        row = QHBoxLayout()
        row.addWidget(button("➕ Neu", self._add))
        row.addWidget(button("⧉ Kopie", self._duplicate))
        row.addWidget(button("🗑️", self._delete, bg="#5a1e1e"))
        left.addLayout(row)
        h.addLayout(left)

        self.form_box = QWidget()
        right = QVBoxLayout(self.form_box)
        form = QFormLayout()
        self.f_name = QLineEdit()
        self.f_host = QLineEdit()
        self.f_host.setPlaceholderText("z. B. 192.168.1.50 oder server.example.org")
        self.f_port = QSpinBox()
        self.f_port.setRange(1, 65535)
        self.f_user = QLineEdit()
        form.addRow("Profilname:", self.f_name)
        form.addRow("Host / IP:", self.f_host)
        form.addRow("SSH-Port:", self.f_port)
        form.addRow("SSH-Benutzer:", self.f_user)
        right.addLayout(form)

        auth = QGroupBox("Anmeldung")
        af = QFormLayout(auth)
        self.f_auth = QComboBox()
        self.f_auth.addItems(["SSH-Schlüssel (empfohlen, kein Passwort nötig)", "Passwort"])
        af.addRow("Verfahren:", self.f_auth)
        krow = QHBoxLayout()
        self.f_key = QLineEdit()
        self.f_key.setPlaceholderText(str(paths.default_key_path()))
        self.f_key.textChanged.connect(self._update_key_info)
        krow.addWidget(self.f_key)
        browse = QPushButton("…")
        browse.setFixedWidth(30)
        browse.clicked.connect(self._browse_key)
        krow.addWidget(browse)
        af.addRow("Schlüssel-Datei:", krow)
        self.key_info = QLabel("")
        af.addRow("", self.key_info)
        kbtn = QHBoxLayout()
        kbtn.addWidget(button("🔑 Schlüssel erzeugen", self._generate_key))
        kbtn.addWidget(button("📤 Schlüssel auf Server hinterlegen", self._install_key, bg="#2e7d32", fg="#ffffff",
                              tip="Fragt einmal nach dem SSH-Passwort und trägt den Schlüssel in "
                                  "~/.ssh/authorized_keys ein. Danach Anmeldung ohne Passwort."))
        kbtn.addStretch()
        af.addRow("", kbtn)
        right.addWidget(auth)

        dmn = QGroupBox("Vanguard-Daemon (Server-Skript)")
        df = QFormLayout(dmn)
        self.f_daemon = QComboBox()
        self.f_daemon.addItems(["Automatisch bereitstellen, wenn Docker läuft", "Aus – nur SSH"])
        df.addRow("Modus:", self.f_daemon)
        self.f_dport = QSpinBox()
        self.f_dport.setRange(1024, 65535)
        df.addRow("Daemon-Port (nur 127.0.0.1):", self.f_dport)
        right.addWidget(dmn)

        misc = QGroupBox("Dateien")
        mf = QFormLayout(misc)
        self.f_workdir = QLineEdit()
        self.f_workdir.setPlaceholderText("leer = Home-Verzeichnis")
        mf.addRow("Startordner:", self.f_workdir)
        self.f_bm = QTableWidget(0, 2)
        self.f_bm.setHorizontalHeaderLabels(["Name", "Pfad"])
        self.f_bm.verticalHeader().setVisible(False)
        self.f_bm.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.f_bm.setFixedHeight(110)
        mf.addRow("Lesezeichen:", self.f_bm)
        brow = QHBoxLayout()
        brow.addWidget(button("➕ Lesezeichen", self._bm_add, bg="#2c313a"))
        brow.addWidget(button("➖ Entfernen", self._bm_del, bg="#2c313a"))
        brow.addStretch()
        mf.addRow("", brow)
        right.addWidget(misc)

        dc = QGroupBox("🐳 Docker Compose")
        dcf = QFormLayout(dc)
        self.f_csearch = QLineEdit()
        self.f_csearch.setPlaceholderText("/opt, /srv, /home, /root")
        self.f_csearch.setToolTip("Ordner auf dem Server, in denen „🔍 Suchen“ im Docker-Reiter nach "
                                  "docker-compose.yml / compose.yaml sucht (bis 5 Ebenen tief), durch Komma getrennt.")
        dcf.addRow("Suchordner:", self.f_csearch)
        self.f_cfiles = QPlainTextEdit()
        self.f_cfiles.setPlaceholderText("/opt/docker-compose.yml\n(eine Datei pro Zeile – zusätzlich zur Suche)")
        self.f_cfiles.setFixedHeight(64)
        dcf.addRow("Feste Dateien:", self.f_cfiles)
        right.addWidget(dc)

        pw = QGroupBox("Gespeicherte Passwörter (verschlüsselter Tresor)")
        pl = QHBoxLayout(pw)
        for field in ("sudo_password", "ssh_password", "key_passphrase"):
            pl.addWidget(button(f"{FIELD_LABELS[field]} …", lambda f=field: self._edit_secret(f), bg="#2b2d42", fg=YELLOW))
        pl.addStretch()
        right.addWidget(pw)
        right.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.form_box)
        h.addWidget(scroll, stretch=1)
        return w

    def _set_form_enabled(self, on: bool):
        self.form_box.setEnabled(on)

    def _select(self, name: str):
        self._store_form()
        self.current = name if name in self.data["profiles"] else None
        self._set_form_enabled(self.current is not None)
        if self.current is None:
            return
        p = self.data["profiles"][self.current]
        self.f_name.setText(self.current)
        self.f_host.setText(p["host"])
        self.f_port.setValue(int(p["ssh_port"]))
        self.f_user.setText(p["ssh_user"])
        self.f_auth.setCurrentIndex(0 if p["auth"] == "key" else 1)
        self.f_key.setText(p["ssh_key"])
        self.f_daemon.setCurrentIndex(0 if p["daemon_mode"] == "auto" else 1)
        self.f_dport.setValue(int(p["daemon_port"]))
        self.f_workdir.setText(p["remote_workdir"])
        self.f_csearch.setText(", ".join(p.get("compose_search_paths", [])))
        self.f_cfiles.setPlainText("\n".join(p.get("compose_files", [])))
        self.f_bm.setRowCount(0)
        for bm in p["bookmarks"]:
            self._bm_add(bm["name"], bm["path"])
        self._update_key_info()

    def _store_form(self) -> bool:
        """Übernimmt das Formular in self.data. Gibt False zurück, wenn der Name ungültig ist."""
        if self.current is None or self.current not in self.data["profiles"]:
            return True
        bms = []
        for r in range(self.f_bm.rowCount()):
            n, p = self.f_bm.item(r, 0), self.f_bm.item(r, 1)
            path = p.text().strip() if p else ""
            if path:
                bms.append({"name": (n.text().strip() if n else "") or path, "path": path})
        prof = normalize_profile({
            "host": self.f_host.text(), "ssh_port": self.f_port.value(), "ssh_user": self.f_user.text(),
            "auth": "key" if self.f_auth.currentIndex() == 0 else "password", "ssh_key": self.f_key.text(),
            "daemon_mode": "auto" if self.f_daemon.currentIndex() == 0 else "off",
            "daemon_port": self.f_dport.value(), "remote_workdir": self.f_workdir.text(), "bookmarks": bms,
            "compose_search_paths": [x.strip() for x in self.f_csearch.text().replace(";", ",").split(",") if x.strip()],
            "compose_files": [x.strip() for x in self.f_cfiles.toPlainText().splitlines() if x.strip()],
        })
        new_name = self.f_name.text().strip()
        if new_name and new_name != self.current:
            if new_name in self.data["profiles"]:
                QMessageBox.warning(self, "Profil", f"Ein Profil „{new_name}“ gibt es schon.")
                self.f_name.setText(self.current)
                new_name = self.current
            else:
                profiles = self.data["profiles"]
                self.data["profiles"] = {(new_name if k == self.current else k): v for k, v in profiles.items()}
                self.renames.append((self.current, new_name))
                self._sync_profile_combos((self.current, new_name))
                if self.data.get("active_profile") == self.current:
                    self.data["active_profile"] = new_name
                for i in range(self.plist.count()):
                    if self.plist.item(i).text() == self.current:
                        self.plist.blockSignals(True)
                        self.plist.item(i).setText(new_name)
                        self.plist.blockSignals(False)
                self.current = new_name
        self.data["profiles"][self.current] = prof
        return True

    def _unique(self, base: str) -> str:
        name, i = base, 2
        while name in self.data["profiles"]:
            name, i = f"{base} {i}", i + 1
        return name

    def _add(self):
        self._store_form()
        name = self._unique("Neuer Server")
        self.data["profiles"][name] = new_profile()
        self._sync_profile_combos()
        self.plist.addItem(name)
        self.plist.setCurrentRow(self.plist.count() - 1)
        self.f_name.setFocus()
        self.f_name.selectAll()

    def _duplicate(self):
        if self.current is None:
            return
        self._store_form()
        name = self._unique(f"{self.current} (Kopie)")
        self.data["profiles"][name] = copy.deepcopy(self.data["profiles"][self.current])
        self._sync_profile_combos()
        self.plist.addItem(name)
        self.plist.setCurrentRow(self.plist.count() - 1)

    def _delete(self):
        if self.current is None:
            return
        if QMessageBox.question(self, "Profil löschen", f"Profil „{self.current}“ löschen? (Gespeicherte "
                                "Passwörter dieses Profils werden aus dem Tresor entfernt.)") != QMessageBox.StandardButton.Yes:
            return
        name = self.current
        self.current = None
        self.data["profiles"].pop(name, None)
        self.deleted.append(name)
        self._sync_profile_combos()  # Buttons nur für dieses Profil gelten danach für alle
        row = self.plist.currentRow()
        self.plist.blockSignals(True)
        self.plist.takeItem(row)
        self.plist.blockSignals(False)
        if self.plist.count():
            self.plist.setCurrentRow(min(row, self.plist.count() - 1))
            self._select(self.plist.currentItem().text())
        else:
            self._set_form_enabled(False)

    def _bm_add(self, name: str = "", path: str = ""):
        r = self.f_bm.rowCount()
        self.f_bm.insertRow(r)
        self.f_bm.setItem(r, 0, QTableWidgetItem(name or ""))
        self.f_bm.setItem(r, 1, QTableWidgetItem(path or "/"))

    def _bm_del(self):
        r = self.f_bm.currentRow()
        if r >= 0:
            self.f_bm.removeRow(r)

    # ================================================================== Schlüssel
    def _key_path(self) -> Path:
        return effective_key_path({"ssh_key": self.f_key.text()})

    def _update_key_info(self):
        p = self._key_path()
        if p.exists():
            try:
                fp = keys.fingerprint(keys.read_public_line(p))
                self.key_info.setText(f"<span style='color:{GREEN};'>✔ {p.name} · {fp}</span>")
            except Exception:  # noqa: BLE001
                self.key_info.setText(f"<span style='color:{YELLOW};'>Schlüssel vorhanden (Passphrase-geschützt)</span>")
        else:
            self.key_info.setText(f"<span style='color:{GREY};'>Noch kein Schlüssel unter {p} – "
                                  "„Schlüssel erzeugen“ legt einen an.</span>")

    def _browse_key(self):
        start = str(self._key_path().parent if self._key_path().parent.exists() else Path.home())
        fn, _ = QFileDialog.getOpenFileName(self, "Privaten Schlüssel wählen", start)
        if fn:
            self.f_key.setText(fn[:-4] if fn.endswith(".pub") else fn)

    def _generate_key(self):
        p = self._key_path()
        if p.exists():
            QMessageBox.information(self, "Schlüssel", f"Unter {p} liegt bereits ein Schlüssel – er wird verwendet.")
            return
        try:
            pub = keys.generate_ed25519(p)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "Schlüssel", str(exc))
            return
        self._update_key_info()
        QMessageBox.information(self, "Schlüssel erzeugt", f"Neuer Schlüssel: {p}\n{keys.fingerprint(pub)}\n\n"
                                "Jetzt „Schlüssel auf Server hinterlegen“ klicken.")

    def _install_key(self):
        self._store_form()
        if self.current is None:
            return
        prof = self.data["profiles"][self.current]
        if not prof["host"] or not prof["ssh_user"]:
            QMessageBox.information(self, "Schlüssel", "Bitte zuerst Host und SSH-Benutzer eintragen.")
            return

        def done(ok: bool):
            if ok and self.current in self.data["profiles"]:
                self.f_auth.setCurrentIndex(0)
                self.data["profiles"][self.current]["auth"] = "key"
                self._update_key_info()

        self.main.install_key(self.current, prof, self, done)

    # ================================================================== Passwörter
    def _edit_secret(self, field: str):
        if self.current is None:
            return
        self._store_form()
        v = self.main.unlock_vault(create=True, parent=self)
        if v is None:
            return
        has = bool(v.get(self.current, field))
        res = PasswordDialog.ask(self, FIELD_LABELS[field],
                                 f"{FIELD_LABELS[field]} für „{self.current}“ "
                                 f"({'ist gespeichert – neu eingeben ersetzt es' if has else 'noch nicht gespeichert'}).\n"
                                 "Leer lassen und „Entfernen“ wählen, um es zu löschen.")
        if res is None:
            if has and QMessageBox.question(self, FIELD_LABELS[field], "Gespeichertes Passwort entfernen?") \
                    == QMessageBox.StandardButton.Yes:
                v.set(self.current, field, "")
                self.main.forget_secret(field, self.current)
            return
        v.set(self.current, field, res[0])
        self.main.forget_secret(field, self.current)
        QMessageBox.information(self, "Tresor", f"{FIELD_LABELS[field]} gespeichert.")

    # ================================================================== Befehle
    CMD_COLS = ["Gruppe", "Button-Name", "Befehl", "root", "Rückfrage", "Profil"]

    def _commands_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        info = QLabel("Eigene Buttons für Aktionen auf dem Server – sie erscheinen in der Seitenleiste, nach Gruppe "
                      "sortiert. <b>root</b> = über den Daemon bzw. mit sudo. <b>Rückfrage</b> = vor dem Ausführen "
                      "bestätigen (für Befehle, die etwas verändern). <b>Profil</b> = nur bei diesem Server zeigen. "
                      "Doppelklick auf eine Zelle zum Bearbeiten.")
        info.setWordWrap(True)
        v.addWidget(info)
        self.cmd_table = QTableWidget(0, len(self.CMD_COLS))
        self.cmd_table.setHorizontalHeaderLabels(self.CMD_COLS)
        self.cmd_table.verticalHeader().setVisible(False)
        self.cmd_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.cmd_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        hh = self.cmd_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.Interactive)
        self.cmd_table.setColumnWidth(5, 150)
        self.cmd_table.setColumnWidth(0, 110)
        self.cmd_table.setColumnWidth(1, 200)
        for c in self.data["custom_commands"]:
            self._cmd_row(c)
        v.addWidget(self.cmd_table)
        row = QHBoxLayout()
        row.addWidget(button("➕ Eigener Befehl", lambda: self._cmd_row(
            {"group": "Eigene", "name": "Neuer Button", "cmd": "uname -a"}, select=True)))
        row.addWidget(button("📚 Aus Vorlagen …", self._from_presets, bg="#2e7d32", fg="#ffffff"))
        row.addWidget(button("⬆️", lambda: self._cmd_move(-1), bg="#2c313a"))
        row.addWidget(button("⬇️", lambda: self._cmd_move(+1), bg="#2c313a"))
        row.addWidget(button("➖ Entfernen", self._cmd_remove, bg="#5a1e1e"))
        row.addStretch()
        v.addLayout(row)
        return w

    @staticmethod
    def _check_item(on: bool) -> QTableWidgetItem:
        it = QTableWidgetItem("")
        it.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        it.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
        return it

    def _cmd_row(self, c: dict, row: int | None = None, select: bool = False):
        r = self.cmd_table.rowCount() if row is None else row
        self.cmd_table.insertRow(r)
        self.cmd_table.setItem(r, 0, QTableWidgetItem(c.get("group", "") or "Allgemein"))
        self.cmd_table.setItem(r, 1, QTableWidgetItem(c.get("name", "")))
        self.cmd_table.setItem(r, 2, QTableWidgetItem(c.get("cmd", "")))
        self.cmd_table.setItem(r, 3, self._check_item(bool(c.get("root"))))
        self.cmd_table.setItem(r, 4, self._check_item(bool(c.get("confirm"))))
        combo = QComboBox()
        self._fill_profile_combo(combo, c.get("profile", ""))
        self.cmd_table.setCellWidget(r, 5, combo)
        if select:
            self.cmd_table.selectRow(r)
            self.cmd_table.scrollToItem(self.cmd_table.item(r, 1))

    def _cmd_get(self, r: int) -> dict:
        t = self.cmd_table
        text = lambda col: (t.item(r, col).text().strip() if t.item(r, col) else "")  # noqa: E731
        checked = lambda col: bool(t.item(r, col) and t.item(r, col).checkState() == Qt.CheckState.Checked)  # noqa: E731
        combo = t.cellWidget(r, 5)
        profile = (combo.currentData() or "") if isinstance(combo, QComboBox) else ""
        return {"group": text(0) or "Allgemein", "name": text(1), "cmd": text(2), "root": checked(3),
                "confirm": checked(4), "profile": profile}

    def _fill_profile_combo(self, combo: QComboBox, selected: str):
        combo.clear()
        combo.addItem("Alle Profile", "")
        for name in self.data["profiles"]:
            combo.addItem(name, name)
        idx = combo.findData(selected)
        combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _sync_profile_combos(self, renamed: tuple[str, str] | None = None):
        """Nach Anlegen/Umbenennen/Löschen von Profilen die Auswahllisten der Befehle anpassen."""
        for r in range(self.cmd_table.rowCount()):
            combo = self.cmd_table.cellWidget(r, 5)
            if not isinstance(combo, QComboBox):
                continue
            sel = combo.currentData() or ""
            if renamed and sel == renamed[0]:
                sel = renamed[1]
            self._fill_profile_combo(combo, sel if sel in self.data["profiles"] else "")

    def _cmd_all(self) -> list[dict]:
        return [c for c in (self._cmd_get(r) for r in range(self.cmd_table.rowCount())) if c["name"] and c["cmd"]]

    def _cmd_move(self, delta: int):
        r = self.cmd_table.currentRow()
        target = r + delta
        if r < 0 or not 0 <= target < self.cmd_table.rowCount():
            return
        c = self._cmd_get(r)
        self.cmd_table.removeRow(r)
        self._cmd_row(c, row=target, select=True)

    def _cmd_remove(self):
        r = self.cmd_table.currentRow()
        if r >= 0:
            self.cmd_table.removeRow(r)

    def _from_presets(self):
        from ..presets import PRESETS
        have = {c["cmd"] for c in self._cmd_all()}
        dlg = QDialog(self)
        dlg.setWindowTitle("📚 Befehls-Vorlagen")
        dlg.resize(720, 560)
        lay = QVBoxLayout(dlg)
        lay.addWidget(QLabel("Häkchen setzen und „Übernehmen“ – übernommene Vorlagen kannst du danach frei ändern. "
                             "Bereits vorhandene sind ausgegraut. ⚠️ = mit Rückfrage."))
        tree = QTreeWidget()
        tree.setHeaderLabels(["Befehl", "Ausführung"])
        tree.setColumnWidth(0, 330)
        groups: dict[str, QTreeWidgetItem] = {}
        for p in PRESETS:
            parent = groups.get(p["group"])
            if parent is None:
                parent = QTreeWidgetItem([p["group"], ""])
                parent.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
                parent.setCheckState(0, Qt.CheckState.Unchecked)
                tree.addTopLevelItem(parent)
                groups[p["group"]] = parent
            label = ("⚠️ " if p["confirm"] else "") + p["name"]
            it = QTreeWidgetItem([label, ("root · " if p["root"] else "") + p["cmd"]])
            it.setToolTip(1, p["cmd"])
            it.setData(0, Qt.ItemDataRole.UserRole, p)
            if p["cmd"] in have:
                it.setFlags(Qt.ItemFlag.NoItemFlags)
            else:
                it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(0, Qt.CheckState.Unchecked)
            parent.addChild(it)
        tree.expandAll()
        lay.addWidget(tree)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Übernehmen")
        bb.accepted.connect(dlg.accept)
        bb.rejected.connect(dlg.reject)
        lay.addWidget(bb)
        if not dlg.exec():
            return
        added = 0
        for gi in range(tree.topLevelItemCount()):
            g = tree.topLevelItem(gi)
            for ci in range(g.childCount()):
                it = g.child(ci)
                if it.flags() & Qt.ItemFlag.ItemIsEnabled and it.checkState(0) == Qt.CheckState.Checked:
                    self._cmd_row(dict(it.data(0, Qt.ItemDataRole.UserRole)), select=True)
                    added += 1
        if added:
            QMessageBox.information(self, "Vorlagen", f"{added} Befehl(e) übernommen. Mit „Speichern“ erscheinen sie "
                                    "in der Seitenleiste.")

    # ================================================================== Tresor
    def _vault_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        info = QLabel(
            "Der Tresor speichert sudo-Passwort, SSH-Passwort und Schlüssel-Passphrase verschlüsselt "
            "(AES-256-GCM, Schlüssel aus dem Master-Passwort per scrypt). Das Master-Passwort wird nur "
            "abgefragt, wenn ein gespeichertes Passwort gebraucht wird – höchstens einmal pro Programmstart.\n\n"
            "Mit SSH-Schlüssel und laufendem Daemon braucht der Commander normalerweise gar kein Passwort.")
        info.setWordWrap(True)
        v.addWidget(info)
        self.vault_state = QLabel("")
        v.addWidget(self.vault_state)
        self.remember_state = QLabel("")
        self.remember_state.setWordWrap(True)
        v.addWidget(self.remember_state)
        rrow = QHBoxLayout()
        self.btn_remember_on = button("💾 Auf diesem Rechner merken", self._remember_on, bg="#2e7d32")
        self.btn_remember_off = button("🚫 Nicht mehr merken", self._remember_off, bg="#5a1e1e")
        rrow.addWidget(self.btn_remember_on)
        rrow.addWidget(self.btn_remember_off)
        rrow.addStretch()
        v.addLayout(rrow)
        agent = QLabel(
            "<b>SSH-Agent:</b> Hat dein Schlüssel eine Passphrase, kann auch der SSH-Agent sie übernehmen – dann "
            "fragt der Commander gar nicht. Windows (PowerShell als Administrator, einmalig):<br>"
            "<code>Get-Service ssh-agent | Set-Service -StartupType Automatic; Start-Service ssh-agent</code><br>"
            "danach als normaler Benutzer: <code>ssh-add $env:USERPROFILE\\.ssh\\id_ed25519</code><br>"
            "Windows merkt sich den Schlüssel dann dauerhaft. macOS/Linux: <code>ssh-add</code>.")
        agent.setWordWrap(True)
        agent.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(agent)
        row = QHBoxLayout()
        row.addWidget(button("🔓 Anlegen / Entsperren", lambda: (self.main.unlock_vault(create=True, parent=self),
                                                                 self._vault_refresh())))
        row.addWidget(button("🔑 Master-Passwort ändern", self._change_master))
        row.addWidget(button("🔒 Sperren", lambda: (self.main.vault.lock(), self.main._session_secrets.clear(),
                                                   self._vault_refresh()), bg="#3e4451"))
        row.addStretch()
        v.addLayout(row)
        v.addStretch()
        self._vault_refresh()
        return w

    def _vault_refresh(self):
        vt = self.main.vault
        if not vt.exists():
            state = "Noch kein Tresor angelegt."
        elif vt.is_unlocked:
            state = f"<span style='color:{GREEN};'>Entsperrt</span> · {vt.path}"
        else:
            state = f"<span style='color:{YELLOW};'>Gesperrt</span> · {vt.path}"
        self.vault_state.setText(state)
        if not remember.available():
            self.remember_state.setText(f"<span style='color:{GREY};'>Merken ist auf diesem System nicht verfügbar "
                                        "(unter Linux: Paket libsecret-tools installieren).</span>")
            self.btn_remember_on.setEnabled(False)
            self.btn_remember_off.setEnabled(False)
            return
        on = remember.is_remembered()
        self.remember_state.setText(
            f"Automatisch entsperren: <b style='color:{GREEN if on else GREY};'>{'an' if on else 'aus'}</b> "
            f"({remember.method()}). Wer an deinem angemeldeten Benutzerkonto sitzt, kann den Tresor dann ohne "
            "Master-Passwort öffnen.")
        self.btn_remember_on.setEnabled(vt.exists() and not on)
        self.btn_remember_off.setEnabled(on)

    def _remember_on(self):
        from ..vault import Vault
        dlg = MasterPasswordDialog(self, create=False, offer_remember=False)
        dlg.setWindowTitle("🔐 Master-Passwort merken")
        if not dlg.exec():
            return
        try:
            Vault(self.main.vault.path).unlock(dlg.password())  # nur prüfen, ob es stimmt
            remember.save(dlg.password())
        except (VaultError, remember.RememberError) as exc:
            QMessageBox.warning(self, "Tresor", str(exc))
            return
        self._vault_refresh()
        QMessageBox.information(self, "Tresor", "Gemerkt. Der Tresor öffnet sich ab jetzt automatisch.")

    def _remember_off(self):
        remember.clear()
        self._vault_refresh()

    def _change_master(self):
        v = self.main.unlock_vault(parent=self)
        if v is None:
            return
        dlg = MasterPasswordDialog(self, create=True, offer_remember=False)
        dlg.setWindowTitle("🔐 Neues Master-Passwort")
        if dlg.exec():
            try:
                v.change_password(dlg.password())
                if remember.available() and remember.is_remembered():
                    remember.save(dlg.password())  # gemerktes Passwort mitziehen
                QMessageBox.information(self, "Tresor", "Master-Passwort geändert.")
            except VaultError as exc:
                QMessageBox.warning(self, "Tresor", str(exc))

    # ================================================================== Speichern
    def _save(self):
        self._store_form()
        for name, p in self.data["profiles"].items():
            if not p["host"] or not p["ssh_user"]:
                QMessageBox.warning(self, "Profil unvollständig", f"Im Profil „{name}“ fehlen Host oder SSH-Benutzer.")
                return
        cmds = self._cmd_all()
        self.data["custom_commands"] = cmds
        if self.data.get("active_profile") not in self.data["profiles"]:
            self.data["active_profile"] = next(iter(self.data["profiles"]), "")
        self.main.config.data = self.data
        self.main.config.save()

        if (self.renames or self.deleted) and self.main.vault.exists():
            v = self.main.unlock_vault(parent=self)
            if v is not None:
                for old, new in self.renames:
                    v.rename_profile(old, new)
                for name in self.deleted:
                    v.remove_profile(name)
        for old, new in self.renames:
            for key in [k for k in self.main._session_secrets if k[0] == old]:
                self.main._session_secrets[(new, key[1])] = self.main._session_secrets.pop(key)
        self.accept()
