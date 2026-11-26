# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Protokolldatei des Programms (statt Meldungen in der Konsole).

Ablage: vanguard.log im Konfigurationsordner, höchstens 3 × 1 MB.
Hier landen auch technische Meldungen von paramiko (z. B. „channel open FAILED“, wenn beim Verbinden
geprüft wird, ob der Daemon schon läuft – das ist normal) und unerwartete Programmfehler.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import VERSION, paths

_LOG = logging.getLogger("vanguard")


def log_file() -> Path:
    return paths.config_dir() / "vanguard.log"


def setup() -> Path:
    path = log_file()
    root = logging.getLogger()
    if not any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        try:
            handler: logging.Handler = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=3,
                                                           encoding="utf-8")
        except OSError:
            handler = logging.NullHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    logging.getLogger("paramiko").setLevel(logging.WARNING)   # nur Auffälliges, nicht jedes Paket
    _LOG.info("Vanguard Commander %s gestartet (Python %s, %s)", VERSION, sys.version.split()[0], sys.platform)
    return path


def install_excepthook() -> None:
    """Unerwartete Fehler protokollieren und anzeigen, statt das Programm hart zu beenden.

    PyQt6 bricht das Programm bei einer unbehandelten Ausnahme in einem Slot sonst sofort ab.
    """
    def hook(etype, value, tb):
        _LOG.error("Unbehandelter Fehler", exc_info=(etype, value, tb))
        try:
            from PyQt6.QtWidgets import QApplication, QMessageBox
            if QApplication.instance() is not None:
                QMessageBox.critical(None, "Unerwarteter Fehler",
                                     f"{etype.__name__}: {value}\n\nDas Programm läuft weiter. "
                                     f"Details stehen in:\n{log_file()}")
        except Exception:  # noqa: BLE001
            pass

    sys.excepthook = hook
