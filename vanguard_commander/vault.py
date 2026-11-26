# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Verschlüsselter Tresor für Passwörter (SSH-Passwort, sudo-Passwort, Key-Passphrase).

Format (JSON, Datei vault.bin):
  {"v": 1, "kdf": "scrypt", "n": 32768, "r": 8, "p": 1,
   "salt": b64, "nonce": b64, "ct": b64}
Verschlüsselung: AES-256-GCM, Schlüssel per scrypt aus dem Master-Passwort.
Das Master-Passwort wird nie gespeichert; entsperrt bleibt der Tresor nur im Arbeitsspeicher.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from . import paths

_AAD = b"vanguard-vault-v1"
_FIELDS = ("ssh_password", "sudo_password", "key_passphrase")
FIELD_LABELS = {
    "ssh_password": "SSH-Passwort",
    "sudo_password": "sudo-Passwort",
    "key_passphrase": "Schlüssel-Passphrase",
}


class VaultError(Exception):
    pass


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=32, n=n, r=r, p=p).derive(password.encode("utf-8"))


class Vault:
    def __init__(self, path: Path | None = None):
        self.path = path or paths.vault_file()
        self._key: bytes | None = None
        self._params: dict[str, Any] = {}
        self._data: dict[str, Any] = {}

    # --- Zustand ---------------------------------------------------------------
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def is_unlocked(self) -> bool:
        return self._key is not None

    def lock(self) -> None:
        self._key = None
        self._data = {}

    # --- Anlegen / Öffnen ------------------------------------------------------
    def create(self, master_password: str) -> None:
        if len(master_password) < 8:
            raise VaultError("Das Master-Passwort muss mindestens 8 Zeichen haben.")
        self._params = {"n": 2 ** 15, "r": 8, "p": 1, "salt": os.urandom(16)}
        self._key = _derive(master_password, self._params["salt"], 2 ** 15, 8, 1)
        self._data = {"profiles": {}}
        self.save()

    def unlock(self, master_password: str) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            salt = base64.b64decode(raw["salt"])
            nonce = base64.b64decode(raw["nonce"])
            ct = base64.b64decode(raw["ct"])
            n, r, p = int(raw["n"]), int(raw["r"]), int(raw["p"])
        except (OSError, ValueError, KeyError) as exc:
            raise VaultError(f"Tresor-Datei ist nicht lesbar: {exc}") from exc
        key = _derive(master_password, salt, n, r, p)
        try:
            plain = AESGCM(key).decrypt(nonce, ct, _AAD)
        except InvalidTag as exc:
            raise VaultError("Falsches Master-Passwort.") from exc
        self._key = key
        self._params = {"n": n, "r": r, "p": p, "salt": salt}
        self._data = json.loads(plain.decode("utf-8"))
        self._data.setdefault("profiles", {})

    def change_password(self, new_password: str) -> None:
        self._require()
        if len(new_password) < 8:
            raise VaultError("Das Master-Passwort muss mindestens 8 Zeichen haben.")
        self._params = {"n": 2 ** 15, "r": 8, "p": 1, "salt": os.urandom(16)}
        self._key = _derive(new_password, self._params["salt"], 2 ** 15, 8, 1)
        self.save()

    def save(self) -> None:
        self._require()
        nonce = os.urandom(12)
        ct = AESGCM(self._key).encrypt(nonce, json.dumps(self._data).encode("utf-8"), _AAD)
        doc = {
            "v": 1, "kdf": "scrypt",
            "n": self._params["n"], "r": self._params["r"], "p": self._params["p"],
            "salt": base64.b64encode(self._params["salt"]).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ct": base64.b64encode(ct).decode(),
        }
        paths.write_private(self.path, json.dumps(doc).encode("utf-8"))

    # --- Einträge --------------------------------------------------------------
    def get(self, profile: str, field: str) -> str:
        self._require()
        return str(self._data["profiles"].get(profile, {}).get(field, ""))

    def set(self, profile: str, field: str, value: str, *, save: bool = True) -> None:
        self._require()
        if field not in _FIELDS:
            raise VaultError(f"Unbekanntes Feld: {field}")
        entry = self._data["profiles"].setdefault(profile, {})
        if value:
            entry[field] = value
        else:
            entry.pop(field, None)
        if not entry:
            self._data["profiles"].pop(profile, None)
        if save:
            self.save()

    def rename_profile(self, old: str, new: str) -> None:
        self._require()
        if old in self._data["profiles"]:
            self._data["profiles"][new] = self._data["profiles"].pop(old)
            self.save()

    def remove_profile(self, profile: str) -> None:
        self._require()
        if self._data["profiles"].pop(profile, None) is not None:
            self.save()

    def _require(self) -> None:
        if self._key is None:
            raise VaultError("Der Tresor ist gesperrt.")
