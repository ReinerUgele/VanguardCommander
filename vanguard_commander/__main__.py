# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Start: python -m vanguard_commander"""

import sys


def main() -> int:
    from PyQt6.QtWidgets import QApplication

    from . import APP_NAME, applog
    from .ui.main_window import MainWindow
    from .ui.theme import APP_QSS

    if sys.platform.startswith("win"):
        try:  # eigenes Symbol in der Taskleiste statt dem Python-Symbol
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("LunaCentauri.VanguardCommander")
        except Exception:  # noqa: BLE001
            pass
    applog.setup()
    applog.install_excepthook()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setStyleSheet(APP_QSS)
    from PyQt6.QtGui import QIcon

    from .paths import resource_path
    icon = resource_path("vanguard_commander/resources/vanguard.png")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
