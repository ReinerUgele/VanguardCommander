# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Zwei-Fenster-Dateimanager: lokal ↔ Server.

Wichtig gegenüber v5:
* Kein chmod -R / chown -R mehr nach Uploads oder im Editor. Bestehende Dateien behalten Besitzer und
  Rechte, neue Dateien und Ordner erben den Besitzer des Zielordners (macht der Daemon).
* Ordnerinhalte kommen als JSON vom Daemon (keine ls-Auswertung) – Leerzeichen und Links funktionieren.
* Fehler bei einzelnen Dateien werden gesammelt und gemeldet statt verschluckt.
* Lokales Löschen geht in den Papierkorb.
"""

from __future__ import annotations

import json
import posixpath
import sys
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QEvent, QFile, QMimeData, QStandardPaths, QStorageInfo, Qt, QTimer, QUrl
from PyQt6.QtGui import QColor, QDrag
from PyQt6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
    QScrollArea, QSplitter, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import protocol
from ..presets import groups_in_order
from .dialogs import FileEditorDialog, TransferDialog
from .theme import BLUE, GREEN, YELLOW, button, fmt_bytes

ROLE = Qt.ItemDataRole.UserRole
MIME_REMOTE = "application/x-vanguard-remote"
MIME_LOCAL = "application/x-vanguard-local"
MID_W = 46  # schmaler Mittelstreifen mit den Pfeil-Knöpfen
MOD = "Cmd" if sys.platform == "darwin" else "Strg"


class SortItem(QTableWidgetItem):
    """'..' bleibt immer oben; Größe und Datum sortieren numerisch."""

    def __init__(self, text: str, sort_val=None, parent_row: bool = False):
        super().__init__(text)
        self.sort_val = sort_val
        self.parent_row = parent_row

    def __lt__(self, other):
        if not isinstance(other, SortItem):
            return super().__lt__(other)
        tbl = self.tableWidget()
        desc = tbl is not None and tbl.horizontalHeader().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder
        if self.parent_row != other.parent_row:
            return (not desc) if self.parent_row else desc
        if self.sort_val is not None and other.sort_val is not None:
            return self.sort_val < other.sort_val
        return super().__lt__(other)


_PSEUDO_FS = {"tmpfs", "devtmpfs", "squashfs", "overlay", "proc", "sysfs", "cgroup", "cgroup2", "devpts",
              "securityfs", "pstore", "bpf", "tracefs", "debugfs", "fusectl", "configfs", "mqueue", "hugetlbfs",
              "autofs", "efivarfs", "nsfs", "ramfs", "binfmt_misc"}


class PlacesCombo(QComboBox):
    """Laufwerke und häufige Orte. Wird bei jedem Aufklappen neu eingelesen (USB-Sticks usw.)."""

    def __init__(self, on_pick):
        super().__init__()
        self.on_pick = on_pick
        self.setMinimumWidth(150)
        self.setToolTip("Laufwerk oder Ort wählen (Alt+L)")
        self.activated.connect(self._picked)
        self.refill()

    def showPopup(self):
        self.refill()
        super().showPopup()

    def refill(self):
        self.blockSignals(True)
        self.clear()
        self.addItem("💽 Laufwerke / Orte", "")
        places = [("🏠 Benutzerordner", str(Path.home()))]
        for label, loc in (("🖥️ Desktop", QStandardPaths.StandardLocation.DesktopLocation),
                           ("📥 Downloads", QStandardPaths.StandardLocation.DownloadLocation),
                           ("📄 Dokumente", QStandardPaths.StandardLocation.DocumentsLocation)):
            p = QStandardPaths.writableLocation(loc)
            if p and Path(p).is_dir():
                places.append((label, p))
        for label, p in places:
            self.addItem(label, p)
        for vol in QStorageInfo.mountedVolumes():
            if not vol.isValid() or not vol.isReady():
                continue
            root = vol.rootPath()
            fs = bytes(vol.fileSystemType()).decode("ascii", "replace")
            if fs in _PSEUDO_FS or (root != "/" and root.startswith(("/sys", "/proc", "/dev", "/run", "/snap",
                                                                     "/boot", "/System/Volumes"))):
                continue
            name = vol.displayName()
            label = root if name in ("", root) else f"{root}  {name}"
            free, total = vol.bytesAvailable(), vol.bytesTotal()
            extra = f"  ({fmt_bytes(free)} frei von {fmt_bytes(total)})" if total > 0 else ""
            self.addItem(f"💽 {label}{extra}", root)
        self.setCurrentIndex(0)
        self.blockSignals(False)

    def _picked(self, idx: int):
        path = self.itemData(idx)
        self.setCurrentIndex(0)
        if path:
            self.on_pick(path)


def _ts(t: float) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M") if t else "-"


def _decode_text(data: bytes) -> tuple[str, str] | None:
    """Gibt (Text, Zeilenende) zurück oder None, wenn es keine UTF-8-Textdatei ist."""
    if b"\x00" in data[:8192]:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    eol = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), eol


# ============================================================================ Hintergrund-Jobs
def upload_job(backend, items: list[Path], rdest: str, task) -> dict:
    dirs, files = [], []
    for p in items:
        if p.is_dir():
            base = posixpath.join(rdest, p.name)
            dirs.append(base)
            for sub in sorted(p.rglob("*")):
                r = posixpath.join(base, sub.relative_to(p).as_posix())
                if sub.is_dir():
                    dirs.append(r)
                elif sub.is_file():
                    files.append((sub, r, sub.stat().st_size))
        elif p.is_file():
            files.append((p, posixpath.join(rdest, p.name), p.stat().st_size))
    total = sum(s for _, _, s in files)
    done = [0]
    errors: list[str] = []
    for d in dirs:
        if task.cancelled():
            break
        try:
            backend.mkdir(d)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"Ordner {d}: {exc}")

    def on_bytes(n: int) -> None:
        done[0] += n
        task.progress.emit(("bytes", done[0], total))

    for i, (lp, rp, _sz) in enumerate(files, 1):
        if task.cancelled():
            break
        task.progress.emit(("file", i, len(files), lp.name))
        try:
            backend.push_file(lp, rp, on_bytes, task.cancelled)
        except Exception as exc:  # noqa: BLE001
            if task.cancelled():
                break
            errors.append(f"{lp.name}: {exc}")
    return {"count": len(files), "errors": errors, "cancelled": task.cancelled()}


def download_job(backend, entries: list[dict], local_dir: Path, task) -> dict:
    files, dirs = [], []
    for e in entries:
        if e["dir"]:
            task.progress.emit(("text", f"Lese Ordner {e['full']} …"))
            f, d = backend.scan(e["full"])
            files += f
            dirs += d
        else:
            files.append({"full": e["full"], "rel": e["name"], "size": e.get("size", 0)})
    for d in dirs:
        (local_dir / d).mkdir(parents=True, exist_ok=True)
    total = sum(max(0, f.get("size", 0)) for f in files)
    done = [0]
    errors: list[str] = []

    def on_bytes(n: int) -> None:
        done[0] += n
        task.progress.emit(("bytes", done[0], total))

    for i, f in enumerate(files, 1):
        if task.cancelled():
            break
        task.progress.emit(("file", i, len(files), f["rel"]))
        try:
            backend.pull_file(f["full"], local_dir / f["rel"], on_bytes, task.cancelled)
        except Exception as exc:  # noqa: BLE001
            if task.cancelled():
                break
            errors.append(f"{f['rel']}: {exc}")
    return {"count": len(files), "errors": errors, "cancelled": task.cancelled()}


# ============================================================================ Oberfläche
class FileManagerTab(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        self.local_path = Path.home()
        self.remote_path = "/"
        self._remote_entries: dict[str, dict] = {}
        self._build()
        self.refresh_local()

    # --- Aufbau ------------------------------------------------------------------------
    def _build(self):
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)

        # links: lokal
        wl = QWidget()
        vl = QVBoxLayout(wl)
        vl.setContentsMargins(0, 0, 0, 0)
        nav = QHBoxLayout()
        nav.addWidget(QLabel(f"<b style='color:{BLUE};'>💻 Lokal:</b>"))
        self.l_places = PlacesCombo(self.goto_local)
        nav.addWidget(self.l_places)
        self.l_path = QLineEdit(str(self.local_path))
        self.l_path.returnPressed.connect(self.nav_local)
        nav.addWidget(self.l_path)
        nav.addWidget(button("⬆️", self.local_up, bg="#2c313a"))
        nav.addWidget(button("🔄", self.refresh_local, bg="#2c313a"))
        vl.addLayout(nav)
        self.l_filter = QLineEdit()
        self.l_filter.setPlaceholderText("🔍 Filter …")
        self.l_filter.textChanged.connect(lambda q: self._filter(self.l_table, q))
        vl.addWidget(self.l_filter)
        self.l_table = self._table(["Name", "Größe", "Geändert"], remote=False)
        vl.addWidget(self.l_table)
        bl = QHBoxLayout()
        bl.addWidget(button("📄 Neue Datei", lambda: self.create_file(False)))
        bl.addWidget(button("📁 Neuer Ordner", lambda: self.make_dir(False)))
        bl.addWidget(button("🗑️ Papierkorb", lambda: self.delete(False)))
        vl.addLayout(bl)
        self.l_status = QLabel("")
        self.l_status.setStyleSheet("color: #5c6370;")
        vl.addWidget(self.l_status)
        self.splitter.addWidget(wl)

        # Mitte
        wm = QWidget()
        wm.setFixedWidth(MID_W)
        vm = QVBoxLayout(wm)
        vm.addStretch()
        vm.setContentsMargins(0, 0, 0, 0)
        up = button("➔", self.upload, fg=BLUE, tip=f"Auswahl links hochladen ({MOD}+→ oder F6)")
        down = button("⬅", self.download, fg=BLUE, tip=f"Auswahl rechts herunterladen ({MOD}+← oder F6)")
        for b in (up, down):
            b.setFixedSize(MID_W - 8, 34)
        vm.addWidget(up, alignment=Qt.AlignmentFlag.AlignHCenter)
        vm.addWidget(down, alignment=Qt.AlignmentFlag.AlignHCenter)
        vm.addStretch()
        self.splitter.addWidget(wm)

        # rechts: Server
        wr = QWidget()
        vr = QVBoxLayout(wr)
        vr.setContentsMargins(0, 0, 0, 0)
        nav = QHBoxLayout()
        nav.addWidget(QLabel(f"<b style='color:{GREEN};'>🐧 Server:</b>"))
        self.r_path = QLineEdit(self.remote_path)
        self.r_path.returnPressed.connect(lambda: self.jump_remote(self.r_path.text().strip()))
        nav.addWidget(self.r_path)
        nav.addWidget(button("⬆️", self.remote_up, bg="#2c313a"))
        nav.addWidget(button("🔄", self.refresh_remote, bg="#2c313a"))
        vr.addLayout(nav)
        self.r_filter = QLineEdit()
        self.r_filter.setPlaceholderText("🔍 Filter …")
        self.r_filter.textChanged.connect(lambda q: self._filter(self.r_table, q))
        vr.addWidget(self.r_filter)
        self.r_table = self._table(["Name", "Größe", "Geändert", "Rechte", "Besitzer"], remote=True)
        vr.addWidget(self.r_table)
        br = QHBoxLayout()
        br.addWidget(button("📄 Neue Datei", lambda: self.create_file(True)))
        br.addWidget(button("📁 Neuer Ordner", lambda: self.make_dir(True)))
        br.addWidget(button("🗑️ Löschen", lambda: self.delete(True)))
        vr.addLayout(br)
        self.r_status = QLabel("")
        self.r_status.setStyleSheet("color: #5c6370;")
        vr.addWidget(self.r_status)
        self.splitter.addWidget(wr)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 0)
        self.splitter.setStretchFactor(2, 1)
        self.splitter.setChildrenCollapsible(False)
        self._equalized = False
        self._handles = [self.splitter.handle(i) for i in (1, 2)]
        for h in self._handles:
            h.installEventFilter(self)
            h.setToolTip("Doppelklick: beide Seiten gleich breit")
        layout.addWidget(self.splitter, stretch=5)

        # Seitenleiste
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(190)
        self.side = QWidget()
        self.side_layout = QVBoxLayout(self.side)
        self.side_layout.setContentsMargins(2, 0, 2, 0)
        self.side_layout.setSpacing(2)
        scroll.setWidget(self.side)
        layout.addWidget(scroll, stretch=0)
        for w in (self.l_path, self.r_path, self.l_filter, self.r_filter):
            w.installEventFilter(self)
        self.rebuild_sidebar()

    def _table(self, headers, remote: bool) -> QTableWidget:
        t = QTableWidget(0, len(headers))
        t.setHorizontalHeaderLabels(headers)
        t.verticalHeader().setVisible(False)
        t.setSortingEnabled(True)
        t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, len(headers)):
            t.horizontalHeader().setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        t.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        t.cellDoubleClicked.connect(lambda r, _c: self._activate(remote, r))
        t.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        t.customContextMenuRequested.connect(lambda pos: self._context(remote, pos))
        t.sortByColumn(0, Qt.SortOrder.AscendingOrder)

        def key_press(ev, tbl=t):
            k = ev.key()
            mods = ev.modifiers()
            ctrl = bool(mods & Qt.KeyboardModifier.ControlModifier)
            shift = bool(mods & Qt.KeyboardModifier.ShiftModifier)
            alt = bool(mods & Qt.KeyboardModifier.AltModifier)
            K = Qt.Key
            if k == K.Key_Tab.value and not ctrl:
                self.focus_table(not remote)              # Tab: andere Seite
            elif k in (K.Key_Delete.value, K.Key_F8.value):
                self.delete(remote)
            elif k in (K.Key_Return.value, K.Key_Enter.value) and tbl.currentRow() >= 0:
                self._activate(remote, tbl.currentRow())
            elif k == K.Key_Backspace.value or (alt and k == K.Key_Up.value):
                self.remote_up() if remote else self.local_up()
            elif k == K.Key_F2.value:
                self.rename(remote)
            elif k == K.Key_F4.value and shift:
                self.create_file(remote)
            elif k == K.Key_F4.value:
                sel = self._selected(remote)
                if len(sel) == 1 and not sel[0]["dir"]:
                    self.edit_file(remote, sel[0])
            elif ctrl and k == K.Key_Right.value:
                self.upload()                             # Strg+→: links -> Server
            elif ctrl and k == K.Key_Left.value:
                self.download()                           # Strg+←: Server -> links
            elif k == K.Key_F6.value:
                self.download() if remote else self.upload()
            elif k == K.Key_F7.value:
                self.make_dir(remote)
            elif ctrl and k == K.Key_L.value:
                (self.r_path if remote else self.l_path).setFocus()
                (self.r_path if remote else self.l_path).selectAll()
            elif ctrl and k == K.Key_F.value:
                (self.r_filter if remote else self.l_filter).setFocus()
            elif ctrl and shift and k == K.Key_C.value:
                sel = self._selected(remote)
                if sel:
                    self._copy_path("\n".join(d["full"] for d in sel))
            elif not remote and alt and k == K.Key_L.value:
                self.l_places.showPopup()
            else:
                QTableWidget.keyPressEvent(tbl, ev)

        t.keyPressEvent = key_press
        self._enable_dnd(t, remote)
        return t

    # --- Drag & Drop ---------------------------------------------------------------------
    def _enable_dnd(self, t: QTableWidget, remote: bool):
        """Ziehen zwischen lokal und Server; auf die Server-Seite auch aus dem Explorer/Finder.
        Ablegen auf einer Ordnerzeile überträgt in diesen Ordner, sonst in den aktuell angezeigten."""
        t.setDragEnabled(True)
        t.setAcceptDrops(True)
        t.viewport().setAcceptDrops(True)
        t.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        t.setDefaultDropAction(Qt.DropAction.CopyAction)
        t.setDropIndicatorShown(False)

        def start_drag(_actions, tbl=t):
            sel = self._selected(remote)
            if not sel:
                return
            mime = QMimeData()
            if remote:
                mime.setData(MIME_REMOTE, json.dumps(sel).encode("utf-8"))
            else:
                mime.setUrls([QUrl.fromLocalFile(d["full"]) for d in sel])
                mime.setData(MIME_LOCAL, b"1")
            drag = QDrag(tbl)
            drag.setMimeData(mime)
            drag.exec(Qt.DropAction.CopyAction)

        def accepts(mime) -> bool:
            if remote:
                return mime.hasUrls() and not mime.hasFormat(MIME_REMOTE) and any(u.isLocalFile() for u in mime.urls())
            return mime.hasFormat(MIME_REMOTE)

        def drag_enter(ev):
            if accepts(ev.mimeData()) and (not remote or self.main.backend is not None):
                ev.setDropAction(Qt.DropAction.CopyAction)
                ev.accept()
            else:
                ev.ignore()

        def drop(ev, tbl=t):
            mime = ev.mimeData()
            if not accepts(mime):
                ev.ignore()
                return
            ev.setDropAction(Qt.DropAction.CopyAction)
            ev.accept()
            row = tbl.rowAt(int(ev.position().y()))
            target = None
            if row >= 0 and tbl.item(row, 0) is not None:
                d = tbl.item(row, 0).data(ROLE)
                if d.get("dir") and not d.get("parent"):
                    target = d["full"]
            if remote:
                items = [Path(u.toLocalFile()) for u in mime.urls() if u.isLocalFile()]
                # erst nach dem Ablegen weiterarbeiten – Dialoge während des Ziehens sind unter Windows heikel
                QTimer.singleShot(0, lambda: self.upload(items, target))
            else:
                entries = json.loads(bytes(mime.data(MIME_REMOTE)).decode("utf-8"))
                QTimer.singleShot(0, lambda: self.download(entries, Path(target) if target else None))

        t.startDrag = start_drag
        t.dragEnterEvent = drag_enter
        t.dragMoveEvent = drag_enter
        t.dropEvent = drop

    def rebuild_sidebar(self):
        lay = self.side_layout
        while lay.count():
            item = lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        def head(text):
            lbl = QLabel(f"<b>{text}</b>")
            lbl.setStyleSheet("margin-top: 6px;")
            lay.addWidget(lbl)

        prof = self.main.current_profile()
        head("📌 Lesezeichen")
        home = self.main.conn.home if self.main.conn else ""
        if home:
            lay.addWidget(button("🏠 Home", lambda: self.jump_remote(home)))
        lay.addWidget(button("📁 /", lambda: self.jump_remote("/")))
        for bm in prof.get("bookmarks", []):
            b = button(f"📁 {bm['name']}", lambda p=bm["path"]: self.jump_remote(p), tip=bm["path"])
            b.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda _pos, p=bm["path"]: self._remove_bookmark(p))
            lay.addWidget(b)
        lay.addWidget(button("➕ Aktuellen Ordner merken", self._add_bookmark, bg="#2c313a"))

        head("🛠️ Wartung (root)")
        lay.addWidget(button("📦 apt update", lambda: self.main.run_cmd(
            "apt-get update", as_root=True, timeout=1800, title="apt update"), bg="#223344", fg=BLUE))
        lay.addWidget(button("⬆️ apt upgrade", self._apt_upgrade, bg="#223344", fg=BLUE))
        lay.addWidget(button("📡 Systemlog live", lambda: self.main.open_live(
            "Systemlog", "journalctl -f -n 100 --no-pager", as_root=True), bg="#223344", fg=YELLOW))

        head("👤 Rechte (Auswahl)")
        lay.addWidget(button("🌐 Für alle lesbar (a+rX)", lambda: self._perm("chmod -R a+rX {t}")))
        lay.addWidget(button("👤 Besitzer: SSH-Benutzer", lambda: self._perm("chown -R {u}:{u} {t}")))
        lay.addWidget(button("👑 Besitzer: root", lambda: self._perm("chown -R root:root {t}")))

        for group, cmds in groups_in_order(self.main.config.custom_commands, self.main.current_profile_name()):
            head(f"⚡ {group}")
            for c in cmds:
                tip = c["cmd"] + ("\n(als root)" if c.get("root") else "") + ("\n(mit Rückfrage)" if c.get("confirm") else "")
                lay.addWidget(button(f"{'⚠️' if c.get('confirm') else '⚡'} {c['name']}",
                                     lambda c=c: self.run_custom(c), bg="#2b2d42", fg=YELLOW, tip=tip))

        head("🛡️ Vanguard-Daemon")
        lay.addWidget(button("🔍 Status", lambda: self.main.run_cmd(
            "docker ps -a --filter name=vanguard-node --format 'table {{.Names}}\\t{{.Status}}\\t{{.Image}}\\t{{.Labels}}'",
            as_root=True, title="Daemon-Status")))
        lay.addWidget(button("📡 Protokoll live", lambda: self.main.open_live(
            "Vanguard-Daemon", "docker logs -f --tail 100 vanguard-node", as_root=True), fg=YELLOW))
        lay.addWidget(button("🔄 Neu bereitstellen", self.main.redeploy_daemon, bg="#2e7d32", fg="#ffffff"))
        lay.addWidget(button("⏏️ Daemon entfernen", self.main.remove_daemon, bg="#5a1e1e"))
        lay.addStretch()

    def showEvent(self, ev):
        super().showEvent(ev)
        if not self._equalized:
            self._equalized = True
            QTimer.singleShot(0, self.equalize_panes)

    def equalize_panes(self):
        """Lokal und Server gleich breit (auch per Doppelklick auf einen Trenner)."""
        total = self.splitter.width() - MID_W
        if total > 200:
            self.splitter.setSizes([total // 2, MID_W, total - total // 2])

    # --- Tastatur ------------------------------------------------------------------------
    def focus_table(self, remote: bool):
        t = self.r_table if remote else self.l_table
        t.setFocus()
        if t.currentRow() < 0 and t.rowCount():
            t.selectRow(0)

    def eventFilter(self, obj, ev):
        """Esc / Pfeil runter in Pfad- und Filterfeldern springt zurück in die Liste."""
        if ev.type() == QEvent.Type.MouseButtonDblClick and obj in getattr(self, "_handles", []):
            self.equalize_panes()
            return True
        if ev.type() == QEvent.Type.KeyPress and ev.key() in (Qt.Key.Key_Escape.value, Qt.Key.Key_Down.value):
            remote = obj in (self.r_path, self.r_filter)
            if ev.key() == Qt.Key.Key_Escape.value:
                if obj is self.l_filter or obj is self.r_filter:
                    obj.clear()
                elif obj is self.l_path:
                    obj.setText(str(self.local_path))
                else:
                    obj.setText(self.remote_path)
            self.focus_table(remote)
            return True
        return super().eventFilter(obj, ev)

    def run_custom(self, c: dict):
        if c.get("confirm") and QMessageBox.question(
                self, c["name"], f"Diesen Befehl{' als root' if c.get('root') else ''} ausführen?\n\n{c['cmd']}") \
                != QMessageBox.StandardButton.Yes:
            return
        self.main.run_cmd(c["cmd"], as_root=c.get("root", False), title=c["name"], timeout=1800, focus_log=True)

    # --- Lesezeichen ---------------------------------------------------------------------
    def _add_bookmark(self):
        name = self.main.current_profile_name()
        if not name or not self.main.conn:
            return
        label, ok = QInputDialog.getText(self, "Lesezeichen", f"Name für {self.remote_path}:",
                                         text=posixpath.basename(self.remote_path) or "/")
        if ok and label.strip():
            self.main.config.profiles[name]["bookmarks"].append({"name": label.strip(), "path": self.remote_path})
            self.main.config.save()
            self.rebuild_sidebar()

    def _remove_bookmark(self, path: str):
        name = self.main.current_profile_name()
        if QMessageBox.question(self, "Lesezeichen", f"Lesezeichen {path} entfernen?") == QMessageBox.StandardButton.Yes:
            bms = self.main.config.profiles[name]["bookmarks"]
            self.main.config.profiles[name]["bookmarks"] = [b for b in bms if b["path"] != path]
            self.main.config.save()
            self.rebuild_sidebar()

    # --- Lokal ---------------------------------------------------------------------------
    def goto_local(self, path: str):
        p = Path(path)
        if p.is_dir():
            self.local_path = p
            self.refresh_local()
            self.focus_table(False)
        else:
            QMessageBox.information(self, "Lokal", f"{path} ist nicht erreichbar (Laufwerk leer?).")

    def nav_local(self):
        p = Path(self.l_path.text().strip()).expanduser()
        if p.is_dir():
            self.local_path = p
            self.refresh_local()

    def local_up(self):
        if self.local_path.parent != self.local_path:
            self.local_path = self.local_path.parent
            self.refresh_local()

    def refresh_local(self):
        t = self.l_table
        self.l_path.setText(str(self.local_path))
        t.setSortingEnabled(False)
        t.setRowCount(0)
        if self.local_path.parent != self.local_path:
            self._row(t, "..", {"full": str(self.local_path.parent), "dir": True, "parent": True}, -1, 0, BLUE)
        try:
            entries = list(self.local_path.iterdir())
        except OSError as exc:
            QMessageBox.warning(self, "Lokal", f"Ordner nicht lesbar: {exc}")
            entries = []
        for e in entries:
            try:
                is_dir = e.is_dir()
                st = e.stat()
                size, mtime = (-1 if is_dir else st.st_size), st.st_mtime
            except OSError:
                is_dir, size, mtime = False, -1, 0
            self._row(t, f"📁 {e.name}" if is_dir else f"📄 {e.name}",
                      {"full": str(e), "name": e.name, "dir": is_dir}, size, mtime, BLUE if is_dir else None)
        t.setSortingEnabled(True)
        self._filter(t, self.l_filter.text())
        try:
            from PyQt6.QtCore import QStorageInfo as _SI
            vol = _SI(str(self.local_path))
            free = f" · {fmt_bytes(vol.bytesAvailable())} frei" if vol.isValid() and vol.bytesTotal() > 0 else ""
        except Exception:  # noqa: BLE001
            free = ""
        self.l_status.setText(f"{len(entries)} Einträge{free}")

    # --- Server --------------------------------------------------------------------------
    def on_connected(self):
        prof = self.main.current_profile()
        start = prof.get("remote_workdir") or (self.main.conn.home if self.main.conn else "/")
        self.rebuild_sidebar()
        self.jump_remote(start)

    def on_disconnected(self):
        self.r_table.setRowCount(0)
        self.r_status.setText("")
        self.rebuild_sidebar()

    def jump_remote(self, path: str):
        if not path:
            return
        if not path.startswith("/"):
            path = posixpath.join(self.main.conn.home if self.main.conn else "/", path)
        self.remote_path = protocol.norm_remote(path)
        self.r_path.setText(self.remote_path)
        self.refresh_remote()

    def remote_up(self):
        if self.remote_path != "/":
            self.jump_remote(posixpath.dirname(self.remote_path) or "/")

    def refresh_remote(self):
        b = self.main.backend
        if b is None:
            return
        path = self.remote_path
        self.r_status.setText("Lade …")
        self.main.tasks.run(b.list_dir, path, on_ok=lambda entries: self._fill_remote(path, entries),
                            on_fail=lambda exc: self._remote_error(path, exc))

    def _remote_error(self, path, exc):
        self.r_status.setText(self.main.masker.text(f"⚠️ {exc}"))
        self.main.log(f"{path}: {exc}", error=True)

    def _fill_remote(self, path: str, entries: list[dict]):
        if path != self.remote_path:
            return  # inzwischen woanders hin navigiert
        t = self.r_table
        t.setSortingEnabled(False)
        t.setRowCount(0)
        self._remote_entries = {}
        if path != "/":
            self._row(t, "..", {"full": posixpath.dirname(path) or "/", "dir": True, "parent": True}, -1, 0, GREEN,
                      remote=True)
        for e in entries:
            e = dict(e, full=posixpath.join(path, e["name"]))
            self._remote_entries[e["name"]] = e
            icon = "🔗" if e["link"] else ("📁" if e["dir"] else "📄")
            label = f"{icon} {e['name']}" + (f"  → {e['target']}" if e["link"] and e.get("target") else "")
            label = self.main.masker.text(label)            # nur Anzeige – Daten bleiben unverändert
            self._row(t, label, e, -1 if e["dir"] else e["size"], e["mtime"], GREEN if e["dir"] else None,
                      remote=True, mode=e["mode"], owner=self.main.masker.text(e["owner"]))
        t.setSortingEnabled(True)
        self._filter(t, self.r_filter.text())
        self.r_status.setText(self.main.masker.text(
            f"{len(entries)} Einträge · {self.main.backend.label if self.main.backend else ''}"))

    # --- Tabellenzeilen ------------------------------------------------------------------
    def _row(self, t, name, data, size, mtime, color=None, remote=False, mode="", owner=""):
        r = t.rowCount()
        t.insertRow(r)
        parent = bool(data.get("parent"))
        it = SortItem(name, (0 if data.get("dir") else 1, name.lower()), parent)
        it.setData(ROLE, data)
        if color:
            it.setForeground(QColor(color))
        t.setItem(r, 0, it)
        t.setItem(r, 1, SortItem("" if size < 0 else fmt_bytes(size), size, parent))
        t.setItem(r, 2, SortItem(_ts(mtime) if not parent else "", mtime, parent))
        if remote:
            t.setItem(r, 3, SortItem(mode, None, parent))
            t.setItem(r, 4, SortItem(owner, None, parent))

    @staticmethod
    def _filter(t, query):
        q = query.strip().lower()
        for r in range(t.rowCount()):
            it = t.item(r, 0)
            if it is not None:
                t.setRowHidden(r, bool(q) and not it.data(ROLE).get("parent") and q not in it.text().lower())

    def _selected(self, remote: bool) -> list[dict]:
        t = self.r_table if remote else self.l_table
        rows = sorted({i.row() for i in t.selectedIndexes()})
        out = []
        for r in rows:
            it = t.item(r, 0)
            if it is not None and not it.data(ROLE).get("parent"):
                out.append(it.data(ROLE))
        return out

    def _activate(self, remote: bool, row: int):
        t = self.r_table if remote else self.l_table
        it = t.item(row, 0)
        if it is None:
            return
        d = it.data(ROLE)
        if d["dir"]:
            if remote:
                self.jump_remote(d["full"])
            else:
                self.local_path = Path(d["full"])
                self.refresh_local()
        else:
            self.edit_file(remote, d)

    def _context(self, remote: bool, pos):
        t = self.r_table if remote else self.l_table
        sel = self._selected(remote)
        m = QMenu(self)
        if sel:
            if remote:
                m.addAction(f"⬅️ Herunterladen ({len(sel)})\t{MOD}+←", lambda: self.download())
            else:
                m.addAction(f"➡️ Hochladen ({len(sel)})\t{MOD}+→", lambda: self.upload())
            if len(sel) == 1 and not sel[0]["dir"]:
                m.addAction("📝 Im Editor öffnen", lambda: self.edit_file(remote, sel[0]))
            if len(sel) == 1:
                m.addAction("✏️ Umbenennen (F2)", lambda: self.rename(remote))
                m.addAction(f"📋 Pfad kopieren ({MOD}+Umschalt+C)", lambda: self._copy_path(sel[0]["full"]))
            m.addSeparator()
            m.addAction("🗑️ Löschen" if remote else "🗑️ In den Papierkorb", lambda: self.delete(remote))
            m.addSeparator()
        m.addAction("📄 Neue Datei …", lambda: self.create_file(remote))
        m.addAction("📁 Neuer Ordner …", lambda: self.make_dir(remote))
        m.addAction("🔄 Aktualisieren (F5)", self.refresh_remote if remote else self.refresh_local)
        m.exec(t.viewport().mapToGlobal(pos))

    @staticmethod
    def _copy_path(path: str):
        from PyQt6.QtGui import QGuiApplication
        QGuiApplication.clipboard().setText(path)

    # --- Aktionen ------------------------------------------------------------------------
    def _need_backend(self):
        if self.main.backend is None:
            QMessageBox.information(self, "Nicht verbunden", "Bitte zuerst mit einem Server verbinden.")
            return None
        return self.main.backend

    def create_file(self, remote: bool):
        name, ok = QInputDialog.getText(self, "Neue Datei", "Dateiname:")
        name = name.strip()
        if not ok or not name or "/" in name or "\\" in name:
            return
        if remote:
            b = self._need_backend()
            if not b:
                return
            target = posixpath.join(self.remote_path, name)
            if name in self._remote_entries:
                QMessageBox.warning(self, "Neue Datei", "Datei existiert bereits.")
                return
            self.main.tasks.run(b.push_bytes, b"", target,
                                on_ok=lambda _r: (self.refresh_remote(), self.edit_file(True, {"full": target, "name": name})),
                                on_fail=lambda e: QMessageBox.warning(self, "Neue Datei", str(e)))
        else:
            p = self.local_path / name
            if p.exists():
                QMessageBox.warning(self, "Neue Datei", "Datei existiert bereits.")
                return
            p.touch()
            self.refresh_local()
            self.edit_file(False, {"full": str(p), "name": name})

    def rename(self, remote: bool):
        sel = self._selected(remote)
        if len(sel) != 1:
            return
        d = sel[0]
        old = d.get("name") or (posixpath.basename(d["full"]) if remote else Path(d["full"]).name)
        new, ok = QInputDialog.getText(self, "Umbenennen", f"Neuer Name für „{old}“:", text=old)
        new = new.strip()
        if not ok or not new or new == old or "/" in new or "\\" in new:
            return
        if remote:
            b = self._need_backend()
            if not b:
                return
            src = d["full"]
            dst = posixpath.join(posixpath.dirname(src), new)
            self.main.tasks.run(b.rename, src, dst,
                                on_ok=lambda _r: (self.main.log(f"Umbenannt: {src} → {new}"), self.refresh_remote()),
                                on_fail=lambda e: QMessageBox.warning(self, "Umbenennen", str(e)))
        else:
            src = Path(d["full"])
            dst = src.with_name(new)
            if dst.exists():
                QMessageBox.warning(self, "Umbenennen", f"{new} existiert bereits.")
                return
            try:
                src.rename(dst)
            except OSError as exc:
                QMessageBox.warning(self, "Umbenennen", str(exc))
            self.refresh_local()

    def make_dir(self, remote: bool):
        name, ok = QInputDialog.getText(self, "Neuer Ordner", "Ordnername:")
        name = name.strip()
        if not ok or not name or "/" in name or "\\" in name:
            return
        if remote:
            b = self._need_backend()
            if b:
                self.main.tasks.run(b.mkdir, posixpath.join(self.remote_path, name),
                                    on_ok=lambda _r: self.refresh_remote(),
                                    on_fail=lambda e: QMessageBox.warning(self, "Neuer Ordner", str(e)))
        else:
            (self.local_path / name).mkdir(parents=True, exist_ok=True)
            self.refresh_local()

    def delete(self, remote: bool):
        sel = self._selected(remote)
        if not sel:
            return
        names = "\n".join(f"• {d['full']}" for d in sel[:8]) + (f"\n… und {len(sel) - 8} weitere" if len(sel) > 8 else "")
        if remote:
            problems = [p for p in (protocol.delete_problem(d["full"]) for d in sel) if p]
            if problems:
                QMessageBox.warning(self, "Löschen gesperrt", "\n".join(problems))
                return
            if QMessageBox.warning(self, "Endgültig löschen?",
                                   f"Auf dem Server gibt es keinen Papierkorb. Wirklich endgültig löschen?\n\n{names}",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                                   QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return
            b = self._need_backend()
            if b:
                paths = [d["full"] for d in sel]
                self.main.tasks.run(b.delete, paths,
                                    on_ok=lambda _r: (self.main.log(f"Gelöscht: {', '.join(paths)}"), self.refresh_remote()),
                                    on_fail=lambda e: (QMessageBox.warning(self, "Löschen", str(e)), self.refresh_remote()))
        else:
            if QMessageBox.question(self, "Papierkorb", f"In den Papierkorb verschieben?\n\n{names}") \
                    != QMessageBox.StandardButton.Yes:
                return
            failed = []
            for d in sel:
                res = QFile.moveToTrash(d["full"])
                ok = res[0] if isinstance(res, tuple) else bool(res)
                if not ok:
                    failed.append(d["full"])
            if failed:
                QMessageBox.warning(self, "Papierkorb", "Konnte nicht in den Papierkorb verschieben:\n" + "\n".join(failed))
            self.refresh_local()

    def _apt_upgrade(self):
        if QMessageBox.question(self, "apt upgrade", "Alle Pakete auf dem Server aktualisieren (apt-get upgrade -y)?") \
                == QMessageBox.StandardButton.Yes:
            self.main.run_cmd("apt-get update && apt-get upgrade -y", as_root=True, timeout=3600, title="apt upgrade")

    def _perm(self, template: str):
        sel = self._selected(True)
        if len(sel) != 1:
            QMessageBox.information(self, "Rechte", "Bitte genau einen Eintrag auf der Server-Seite auswählen.")
            return
        import shlex
        target = sel[0]["full"]
        if problem := protocol.delete_problem(target):
            QMessageBox.warning(self, "Rechte", problem.replace("gelöscht", "rekursiv geändert"))
            return
        user = self.main.current_profile().get("ssh_user", "")
        cmd = template.format(t=shlex.quote(target), u=shlex.quote(user))
        if QMessageBox.question(self, "Rechte ändern", f"Diesen Befehl als root ausführen?\n\n{cmd}") \
                == QMessageBox.StandardButton.Yes:
            self.main.run_cmd(cmd, as_root=True, on_done=lambda *_: self.refresh_remote())

    def upload(self, items: list[Path] | None = None, dest: str | None = None):
        b = self._need_backend()
        if items is None:
            items = [Path(d["full"]) for d in self._selected(False)]
        items = [p for p in items if p.exists()]
        if not b or not items:
            return
        dest = dest or self.remote_path
        if dest == self.remote_path:
            conflicts = [p.name for p in items if p.name in self._remote_entries]
            if conflicts:
                lst = "\n".join(f"• {c}" for c in conflicts[:8])
                if QMessageBox.warning(self, "Überschreiben?", f"Existiert schon in {dest}:\n\n{lst}\n\nÜberschreiben?",
                                       QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) \
                        != QMessageBox.StandardButton.Yes:
                    return
        elif QMessageBox.question(self, "Hochladen", f"{len(items)} Element(e) nach {dest} hochladen?\n"
                                  "(Gleichnamige Dateien dort werden ersetzt.)") != QMessageBox.StandardButton.Yes:
            return
        self._transfer("Hochladen", upload_job, b, items, dest, after=self.refresh_remote)

    def download(self, entries: list[dict] | None = None, dest: Path | None = None):
        b = self._need_backend()
        if entries is None:
            entries = self._selected(True)
        if not b or not entries:
            return
        dest = Path(dest or self.local_path)
        conflicts = [e["name"] for e in entries if (dest / e["name"]).exists()]
        if conflicts:
            lst = "\n".join(f"• {c}" for c in conflicts[:8])
            if QMessageBox.warning(self, "Überschreiben?", f"Existiert schon in {dest}:\n\n{lst}\n\nÜberschreiben?",
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No) \
                    != QMessageBox.StandardButton.Yes:
                return
        self._transfer("Herunterladen", download_job, b, entries, dest, after=self.refresh_local)

    def _transfer(self, title, job, *args, after):
        dlg = TransferDialog(self, title)

        def done(res):
            dlg.accept()
            msg = f"{title}: {res['count']} Datei(en)" + (" – abgebrochen" if res["cancelled"] else "")
            if res["errors"]:
                self.main.log(msg + f", {len(res['errors'])} Fehler:\n" + "\n".join(res["errors"]), error=True)
                QMessageBox.warning(self, title, f"{len(res['errors'])} Fehler:\n\n" + "\n".join(res["errors"][:15]))
            else:
                self.main.log(msg + " ✔")
            after()

        def fail(exc):
            dlg.accept()
            self.main.log(f"{title}: {exc}", error=True)
            QMessageBox.warning(self, title, str(exc))
            after()

        task = self.main.tasks.run(job, *args, with_task=True, on_ok=done, on_fail=fail, on_progress=dlg.on_progress)
        dlg.cancel_requested.connect(task.cancel)
        dlg.exec()

    # --- Editor --------------------------------------------------------------------------
    def edit_file(self, remote: bool, d: dict):
        if not remote:
            p = Path(d["full"])
            try:
                data = p.read_bytes()
            except OSError as exc:
                QMessageBox.warning(self, "Editor", str(exc))
                return
            dec = _decode_text(data)
            if dec is None:
                QMessageBox.information(self, "Editor", "Keine UTF-8-Textdatei – Bearbeiten wäre unsicher.")
                return
            text, eol = dec
            dlg = FileEditorDialog(self, p.name, text, str(p))
            if dlg.exec():
                p.write_bytes(dlg.content().replace("\n", eol).encode("utf-8"))
                self.refresh_local()
            return

        b = self._need_backend()
        if not b:
            return
        path = d["full"]
        if problem := protocol.write_problem(path):
            QMessageBox.warning(self, "Editor", problem)
            return

        def open_editor(text: str, eol: str):
            dlg = FileEditorDialog(self, posixpath.basename(path), text, f"{path} · {b.label}")
            if not dlg.exec():
                return
            edited = dlg.content()

            def save_failed(e):
                QMessageBox.critical(self, "Speichern fehlgeschlagen", f"{path}\n\n{e}\n\nDer Editor öffnet sich "
                                     "wieder mit deinem Text, damit nichts verloren geht.")
                open_editor(edited, eol)

            self.main.tasks.run(b.push_bytes, edited.replace("\n", eol).encode("utf-8"), path,
                                on_ok=lambda _r: (self.main.log(f"Gespeichert: {path} (Besitzer und Rechte unverändert)"),
                                                  self.refresh_remote()),
                                on_fail=save_failed)

        def loaded(data: bytes):
            dec = _decode_text(data)
            if dec is None:
                QMessageBox.information(self, "Editor", "Keine UTF-8-Textdatei – Bearbeiten wäre unsicher.")
                return
            open_editor(*dec)

        self.main.tasks.run(b.pull_bytes, path, on_ok=loaded,
                            on_fail=lambda e: QMessageBox.warning(self, "Editor", f"{path}\n\n{e}"))

