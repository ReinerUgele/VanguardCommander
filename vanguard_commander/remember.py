# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Master-Passwort auf diesem Rechner merken (optional, vom Benutzer eingeschaltet).

* Windows: DPAPI (CryptProtectData) – nur derselbe Windows-Benutzer auf diesem Rechner kann es entschlüsseln.
  Ablage: remember.bin im Konfigurationsordner.
* macOS:   Schlüsselbund (Programm "security").
* Linux:   Secret Service über "secret-tool" (GNOME Schlüsselbund / KWallet), falls installiert.

Abwägung: Wer an deinem entsperrten Benutzerkonto sitzt, kann den Tresor dann ohne Master-Passwort öffnen.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys

from . import paths

SERVICE = "VanguardCommander Tresor"
ACCOUNT = "master"
_ENTROPY = b"VanguardCommander-vault-v1"


class RememberError(Exception):
    pass


def method() -> str:
    """Name des Verfahrens oder "" wenn auf diesem System nicht verfügbar."""
    if sys.platform.startswith("win"):
        return "Windows-Anmeldung (DPAPI)"
    if sys.platform == "darwin":
        return "macOS-Schlüsselbund" if shutil.which("security") else ""
    return "Linux-Schlüsselbund (secret-tool)" if shutil.which("secret-tool") else ""


def available() -> bool:
    return bool(method())


def _win_file():
    return paths.config_dir() / "remember.bin"


# ------------------------------------------------------------------------------- Windows DPAPI
def _dpapi(data: bytes, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(raw: bytes):
        buf = ctypes.create_string_buffer(raw, len(raw))
        return DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    p_blob = ctypes.POINTER(DATA_BLOB)
    if protect:
        fn = crypt32.CryptProtectData
        fn.argtypes = [p_blob, wintypes.LPCWSTR, p_blob, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, p_blob]
    else:
        fn = crypt32.CryptUnprotectData
        fn.argtypes = [p_blob, ctypes.c_void_p, p_blob, ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, p_blob]
    fn.restype = wintypes.BOOL

    inp, _keep1 = blob(data)
    ent, _keep2 = blob(_ENTROPY)
    out = DATA_BLOB()
    CRYPTPROTECT_UI_FORBIDDEN = 0x01
    desc = "Vanguard Commander" if protect else None
    if not fn(ctypes.byref(inp), desc, ctypes.byref(ent), None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise RememberError(f"DPAPI-Fehler {ctypes.get_last_error()}")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(out.pbData, ctypes.c_void_p))


# ------------------------------------------------------------------------------- Öffentliche API
def _run(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=15)


def save(password: str) -> None:
    if not password:
        raise RememberError("Leeres Passwort")
    if sys.platform.startswith("win"):
        blob = _dpapi(password.encode("utf-8"), protect=True)
        paths.write_private(_win_file(), base64.b64encode(blob))
        return
    if sys.platform == "darwin":
        r = _run(["security", "add-generic-password", "-U", "-a", ACCOUNT, "-s", SERVICE, "-w", password])
    elif shutil.which("secret-tool"):
        r = _run(["secret-tool", "store", "--label", SERVICE, "service", SERVICE, "account", ACCOUNT], stdin=password)
    else:
        raise RememberError("Auf diesem System ist kein Schlüsselbund verfügbar.")
    if r.returncode != 0:
        raise RememberError((r.stderr or r.stdout).strip() or "Speichern fehlgeschlagen")


def load() -> str | None:
    try:
        if sys.platform.startswith("win"):
            f = _win_file()
            if not f.exists():
                return None
            return _dpapi(base64.b64decode(f.read_bytes()), protect=False).decode("utf-8")
        if sys.platform == "darwin":
            r = _run(["security", "find-generic-password", "-a", ACCOUNT, "-s", SERVICE, "-w"])
        elif shutil.which("secret-tool"):
            r = _run(["secret-tool", "lookup", "service", SERVICE, "account", ACCOUNT])
        else:
            return None
    except (OSError, RememberError, ValueError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    value = r.stdout.rstrip("\n")
    return value or None


def clear() -> None:
    try:
        if sys.platform.startswith("win"):
            f = _win_file()
            if f.exists():
                f.unlink()
        elif sys.platform == "darwin":
            _run(["security", "delete-generic-password", "-a", ACCOUNT, "-s", SERVICE])
        elif shutil.which("secret-tool"):
            _run(["secret-tool", "clear", "service", SERVICE, "account", ACCOUNT])
    except (OSError, subprocess.SubprocessError):
        pass


def is_remembered() -> bool:
    if sys.platform.startswith("win"):
        return _win_file().exists()
    return load() is not None
