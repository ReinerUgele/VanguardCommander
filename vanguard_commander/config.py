# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Profile und Einstellungen – ohne Passwörter oder Secrets.

Passwörter liegen ausschließlich im verschlüsselten Tresor (vault.py).
Das Daemon-Secret wird gar nicht lokal gespeichert: der Commander liest es bei jeder
Verbindung per SSH aus dem Home-Verzeichnis des Server-Benutzers.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from . import paths
from .presets import DEFAULT_COMMANDS, normalize_command

CONFIG_VERSION = 6
DEFAULT_DAEMON_PORT = 50105

PROFILE_TEMPLATE: dict[str, Any] = {
    "host": "",
    "ssh_port": 22,
    "ssh_user": "",
    "auth": "key",                 # "key" oder "password"
    "ssh_key": "",                 # leer = Standardschlüssel (~/.ssh/vanguard_ed25519)
    "daemon_mode": "auto",         # "auto" = bei laufendem Docker bereitstellen, "off" = nur SSH
    "daemon_port": DEFAULT_DAEMON_PORT,
    "remote_workdir": "",          # leer = Home-Verzeichnis des Benutzers
    "bookmarks": [],               # [{"name": "...", "path": "/..."}]
    "compose_files": [],           # zusätzlich zu den automatisch gefundenen Compose-Projekten
    "compose_search_paths": ["/opt", "/srv", "/home", "/root"],   # hier wird nach Compose-Dateien gesucht
}

DEFAULT_CUSTOM_COMMANDS = DEFAULT_COMMANDS


def new_profile(**values: Any) -> dict[str, Any]:
    prof = copy.deepcopy(PROFILE_TEMPLATE)
    prof.update({k: v for k, v in values.items() if k in PROFILE_TEMPLATE})
    return prof


def normalize_profile(raw: dict[str, Any]) -> dict[str, Any]:
    prof = new_profile()
    for key in PROFILE_TEMPLATE:
        if key in raw:
            prof[key] = raw[key]
    prof["ssh_port"] = _as_int(prof.get("ssh_port"), 22)
    prof["daemon_port"] = _as_int(prof.get("daemon_port"), DEFAULT_DAEMON_PORT)
    if prof["auth"] not in ("key", "password"):
        prof["auth"] = "key"
    if prof["daemon_mode"] not in ("auto", "off"):
        prof["daemon_mode"] = "auto"
    prof["bookmarks"] = [
        {"name": str(b.get("name", "")).strip() or str(b.get("path", "")), "path": str(b.get("path", "")).strip()}
        for b in prof.get("bookmarks") or []
        if isinstance(b, dict) and str(b.get("path", "")).strip()
    ]
    prof["compose_files"] = [str(f).strip() for f in prof.get("compose_files") or []
                             if isinstance(f, str) and str(f).strip().startswith("/")]
    prof["compose_search_paths"] = [str(f).strip().rstrip("/") or "/" for f in prof.get("compose_search_paths") or []
                                    if isinstance(f, str) and str(f).strip().startswith("/")] \
        or list(PROFILE_TEMPLATE["compose_search_paths"])
    for key in ("host", "ssh_user", "ssh_key", "remote_workdir"):
        prof[key] = str(prof.get(key) or "").strip()
    return prof


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def effective_key_path(profile: dict[str, Any]) -> Path:
    raw = str(profile.get("ssh_key") or "").strip()
    return Path(raw).expanduser() if raw else paths.default_key_path()


class AppConfig:
    def __init__(self, data: dict[str, Any]):
        self.data = data

    # --- Laden / Speichern -------------------------------------------------
    @classmethod
    def load(cls) -> "AppConfig":
        path = paths.profiles_file()
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                return cls(cls._normalize(raw))
            except (OSError, ValueError):
                broken = path.with_name("profiles.defekt.json")
                try:
                    path.replace(broken)
                except OSError:
                    pass
        cfg = cls(cls._normalize(cls._migrate_legacy()))
        cfg.save()
        return cfg

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
        profiles = {}
        for name, prof in (raw.get("profiles") or {}).items():
            if isinstance(prof, dict) and str(name).strip():
                profiles[str(name).strip()] = normalize_profile(prof)
        cmds = [n for n in (normalize_command(c) for c in raw.get("custom_commands", DEFAULT_CUSTOM_COMMANDS)) if n]
        active = raw.get("active_profile", "")
        if active not in profiles:
            active = next(iter(profiles), "")
        return {
            "version": CONFIG_VERSION,
            "active_profile": active,
            "profiles": profiles,
            "custom_commands": cmds,
            "terminal_font_size": _as_int(raw.get("terminal_font_size"), 11),
        }

    @staticmethod
    def _migrate_legacy() -> dict[str, Any]:
        """Übernimmt Profile aus v5 (~/.vanguard_commander.json) – ohne das alte Daemon-Secret.

        Das alte Secret gehört zum Protokoll v4 und passt nicht zum neuen Daemon.
        Die alte Datei wird nicht verändert.
        """
        legacy = paths.legacy_config_file()
        if not legacy.exists():
            return {"profiles": {}, "custom_commands": DEFAULT_CUSTOM_COMMANDS}
        try:
            old = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"profiles": {}, "custom_commands": DEFAULT_CUSTOM_COMMANDS}
        profiles = {}
        for name, p in (old.get("profiles") or {}).items():
            if not isinstance(p, dict):
                continue
            host = str(p.get("host", "")).strip()
            if host in ("", "127.0.0.1", "localhost") and not p.get("use_ssh_tunnel"):
                continue  # Vorlage ohne echten Server
            profiles[name] = new_profile(
                host=host,
                ssh_user=str(p.get("ssh_user", "")).strip(),
                ssh_key=str(p.get("ssh_key", "")).strip(),
                remote_workdir=str(p.get("remote_workdir", "")).strip(),
            )
        cmds = [
            {"name": c.get("name", ""), "cmd": c.get("cmd", ""), "root": False}
            for c in old.get("custom_commands", []) if isinstance(c, dict) and c.get("name") and c.get("cmd")
        ] or DEFAULT_CUSTOM_COMMANDS
        return {"profiles": profiles, "custom_commands": cmds, "active_profile": old.get("active_profile", "")}

    def save(self) -> None:
        data = json.dumps(self.data, indent=2, ensure_ascii=False).encode("utf-8")
        paths.write_private(paths.profiles_file(), data)

    # --- Zugriff -------------------------------------------------------------
    @property
    def profiles(self) -> dict[str, dict[str, Any]]:
        return self.data["profiles"]

    @property
    def custom_commands(self) -> list[dict[str, Any]]:
        return self.data["custom_commands"]

    @property
    def active_profile(self) -> str:
        return self.data.get("active_profile", "")

    @active_profile.setter
    def active_profile(self, name: str) -> None:
        self.data["active_profile"] = name

    def profile(self, name: str) -> dict[str, Any]:
        return self.profiles.get(name, {})


def legacy_config_path() -> Path:
    return paths.legacy_config_file()
