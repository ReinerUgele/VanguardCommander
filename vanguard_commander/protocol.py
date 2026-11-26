# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Client-Seite des Vanguard-Protokolls v5 (Gegenstück zu server/vanguard_node_daemon.py).

Rahmen: 4 Byte Länge (big endian) + kanonisches JSON.
Anfrage: alle Felder + "token" = HMAC-SHA256(secret, kanonisches JSON ohne token).
Antwort: {"payload": {...}, "sig": HMAC(secret, nonce + ":" + kanonisches JSON von payload)}.
Nur reine Standardbibliothek – kann ohne Qt und paramiko getestet werden.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import posixpath
import re
import time
from typing import Any

PROTOCOL = 5
CHUNK = 1024 * 1024
MAX_RESPONSE = 64 * 1024 * 1024


class DaemonError(Exception):
    pass


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def build_request(secret: bytes, action: str, **params: Any) -> dict:
    hdr = dict(params)
    hdr["action"] = action
    hdr["ts"] = int(time.time())
    hdr["nonce"] = os.urandom(16).hex()
    hdr["token"] = hmac.new(secret, canonical(hdr), hashlib.sha256).hexdigest()
    return hdr


def recv_exact(sock, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), CHUNK))
        if not chunk:
            raise DaemonError("Verbindung zum Daemon unerwartet getrennt.")
        buf.extend(chunk)
    return bytes(buf)


def send_frame(sock, obj: dict) -> None:
    data = canonical(obj)
    sock.sendall(len(data).to_bytes(4, "big") + data)


def recv_frame(sock) -> dict:
    size = int.from_bytes(recv_exact(sock, 4), "big")
    if size < 2 or size > MAX_RESPONSE:
        raise DaemonError("Ungültige Antwort vom Daemon (Länge).")
    try:
        obj = json.loads(recv_exact(sock, size).decode("utf-8"))
    except ValueError as exc:
        raise DaemonError("Ungültige Antwort vom Daemon (JSON).") from exc
    if not isinstance(obj, dict):
        raise DaemonError("Ungültige Antwort vom Daemon.")
    return obj


def recv_signed(sock, secret: bytes, nonce: str) -> dict:
    """Liest eine signierte Antwort. Unsignierte Antworten werden nur als Fehlermeldung akzeptiert."""
    return check_signed(recv_frame(sock), secret, nonce)


def parse_frame(data: bytes) -> dict:
    try:
        obj = json.loads(data.decode("utf-8"))
    except ValueError as exc:
        raise DaemonError("Ungültige Antwort vom Daemon (JSON).") from exc
    if not isinstance(obj, dict):
        raise DaemonError("Ungültige Antwort vom Daemon.")
    return obj


def check_signed(env: dict, secret: bytes, nonce: str) -> dict:
    if "payload" not in env or "sig" not in env:
        if env.get("status") == "ERROR":
            raise DaemonError(str(env.get("error", "Unbekannter Fehler")))
        raise DaemonError("Unsignierte Antwort vom Daemon verworfen.")
    payload = env["payload"]
    expected = hmac.new(secret, nonce.encode("ascii") + b":" + canonical(payload), hashlib.sha256).hexdigest()
    if not isinstance(env["sig"], str) or not hmac.compare_digest(env["sig"].lower(), expected):
        raise DaemonError("Sicherheitsfehler: Antwort des Daemons ist nicht korrekt signiert.")
    if payload.get("status") == "ERROR":
        raise DaemonError(str(payload.get("error", "Unbekannter Fehler")))
    return payload


# --------------------------------------------------------------------------- Schutzregeln
# Gleiche Regeln wie im Daemon – für den SSH-Modus ohne Daemon und als frühe Rückmeldung in der Oberfläche.
NEVER_DELETE_DEPTH2 = re.compile(r"^/(bin|boot|etc|home|lib|lib32|lib64|libx32|root|sbin|usr|var|proc|sys|dev|run)/[^/]+$")
PROTECTED_WRITE = [re.compile(p) for p in (
    r"^/etc/shadow.*$", r"^/etc/gshadow.*$", r"^/etc/passwd-?$", r"^/etc/group-?$",
    r"^/etc/sudoers(\.d(/.*)?)?$", r"^/etc/pam\.d(/.*)?$", r"^/etc/ssh/ssh_host_.*$",
    r"^/root/\.ssh(/.*)?$", r"^/home/[^/]+/\.vanguard(/.*)?$", r"^/etc/vanguard(/.*)?$",
    r"^/opt/vanguard_ssh/daemon\.secret$", r"^/(proc|sys|dev)(/.*)?$", r"^/boot(/.*)?$",
)]


def norm_remote(path: str) -> str:
    if not path.startswith("/"):
        raise ValueError(f"Pfad muss absolut sein: {path}")
    return "/" if path == "/" else posixpath.normpath(path)


def write_problem(path: str) -> str:
    p = norm_remote(path)
    for pat in PROTECTED_WRITE:
        if pat.match(p):
            return f"{p} ist geschützt und wird nicht verändert."
    return ""


def delete_problem(path: str) -> str:
    p = norm_remote(path)
    if p == "/" or p.count("/") < 2 or NEVER_DELETE_DEPTH2.match(p):
        return f"{p} ist ein Systemordner und wird nicht gelöscht."
    return write_problem(p)


def sha256_file(path, cancelled=lambda: False) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            if cancelled():
                raise DaemonError("Abgebrochen.")
            h.update(chunk)
    return h.hexdigest()
