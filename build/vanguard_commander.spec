# -*- mode: python ; coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
# PyInstaller-Spezifikation für Windows (.exe), Linux (Einzeldatei) und macOS (.app).
# Aufruf aus dem Projektordner:  pyinstaller --noconfirm --clean --workpath .pyi-work build/vanguard_commander.spec
import re
import sys
from pathlib import Path

ROOT = Path(SPECPATH).resolve().parent
NAME = "VanguardCommander"
VERSION = re.search(r'^VERSION = "([^"]+)"', (ROOT / "vanguard_commander" / "__init__.py").read_text(encoding="utf-8"),
                    re.M).group(1)

a = Analysis(
    [str(ROOT / "run_commander.py")],
    pathex=[str(ROOT)],
    datas=[(str(ROOT / "vanguard_commander" / "server" / "vanguard_node_daemon.py"), "vanguard_commander/server"),
           (str(ROOT / "vanguard_commander" / "resources" / "vanguard.png"), "vanguard_commander/resources"),
           (str(ROOT / "LICENSE"), "."), (str(ROOT / "THIRD_PARTY_NOTICES.md"), ".")],
    hiddenimports=["pyte", "bcrypt"],
    excludes=["tkinter", "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets", "PyQt6.QtMultimedia",
              "PyQt6.QtQml", "PyQt6.QtQuick", "PyQt6.Qt3DCore"],
    noarchive=False,
)
pyz = PYZ(a.pure)

if sys.platform == "darwin":
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=NAME, console=False, upx=False,
              icon=str(ROOT / "build" / "icons" / "vanguard.icns"))
    coll = COLLECT(exe, a.binaries, a.datas, name=NAME, upx=False)
    app = BUNDLE(coll, name=f"{NAME}.app", bundle_identifier="de.lunacentauri.vanguardcommander",
                 icon=str(ROOT / "build" / "icons" / "vanguard.icns"),
                 info_plist={"NSHighResolutionCapable": True, "CFBundleShortVersionString": VERSION, "CFBundleVersion": VERSION,
                             "CFBundleName": "Vanguard Commander", "LSMinimumSystemVersion": "10.14"})
else:
    exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name=NAME, console=False, upx=False,
              runtime_tmpdir=None, icon=str(ROOT / "build" / "icons" / "vanguard.ico"))
