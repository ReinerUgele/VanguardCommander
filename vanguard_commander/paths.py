# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Plattformgerechte Ablageorte. Nichts davon liegt im Programmcode oder im Programmordner.

Windows: %APPDATA%\\VanguardCommander
macOS:   ~/Library/Application Support/VanguardCommander
Linux:   $XDG_CONFIG_HOME/VanguardCommander (Standard ~/.config/VanguardCommander)

Mit der Umgebungsvariable VANGUARD_CONFIG_DIR lässt sich ein eigener Ordner setzen
(z. B. für eine portable Installation auf einem USB-Stick).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_NAME


def _restrict(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def config_dir() -> Path:
    override = os.environ.get("VANGUARD_CONFIG_DIR", "").strip()
    if override:
        base_dir = Path(override).expanduser()
    elif sys.platform.startswith("win"):
        base_dir = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming")) / APP_NAME
    elif sys.platform == "darwin":
        base_dir = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        base_dir = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / APP_NAME
    base_dir.mkdir(parents=True, exist_ok=True)
    _restrict(base_dir, 0o700)
    return base_dir


def profiles_file() -> Path:
    return config_dir() / "profiles.json"


def vault_file() -> Path:
    return config_dir() / "vault.bin"


def known_hosts_file() -> Path:
    return config_dir() / "known_hosts"


def default_key_path() -> Path:
    """Eigener Schlüssel im üblichen ~/.ssh, damit ihn auch andere SSH-Programme nutzen können."""
    return Path.home() / ".ssh" / "vanguard_ed25519"


def legacy_config_file() -> Path:
    """Konfiguration der alten Version 5.x (wird nur gelesen, nie verändert)."""
    return Path.home() / ".vanguard_commander.json"


def resource_path(relative: str) -> Path:
    """Pfad zu mitgelieferten Dateien – funktioniert im Quellcode und in der PyInstaller-Exe."""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base) / relative
    return Path(__file__).resolve().parent.parent / relative


def write_private(path: Path, data: bytes) -> None:
    """Atomar schreiben, Rechte 0600 (unter Windows greift der Schutz des Benutzerprofils)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    os.replace(tmp, path)
    _restrict(path, 0o600)
