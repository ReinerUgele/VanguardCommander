# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""SSH-Schlüssel erzeugen, lesen und auf dem Server hinterlegen."""

from __future__ import annotations

import base64
import getpass
import hashlib
import os
import socket
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


class KeyError_(Exception):
    pass


def key_comment() -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    return f"{user}@{socket.gethostname()} (Vanguard Commander)"


def generate_ed25519(path: Path, passphrase: str = "") -> str:
    """Erzeugt ein Ed25519-Schlüsselpaar im OpenSSH-Format. Gibt die Public-Key-Zeile zurück.

    Ein vorhandener Schlüssel wird nie überschrieben.
    """
    path = Path(path).expanduser()
    if path.exists() or Path(str(path) + ".pub").exists():
        raise KeyError_(f"Unter {path} liegt bereits ein Schlüssel – er wird nicht überschrieben.")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    key = Ed25519PrivateKey.generate()
    enc = (serialization.BestAvailableEncryption(passphrase.encode("utf-8"))
           if passphrase else serialization.NoEncryption())
    priv = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, enc)
    pub = key.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    pub_line = pub.decode("ascii") + " " + key_comment()
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(priv)
    Path(str(path) + ".pub").write_text(pub_line + "\n", encoding="ascii")
    return pub_line


def read_public_line(private_path: Path, passphrase: str = "") -> str:
    """Liest die .pub-Datei oder leitet den öffentlichen Schlüssel aus dem privaten ab."""
    private_path = Path(private_path).expanduser()
    pub_path = Path(str(private_path) + ".pub")
    if pub_path.exists():
        line = pub_path.read_text(encoding="utf-8").strip()
        if line:
            return line
    if not private_path.exists():
        raise KeyError_(f"Kein Schlüssel gefunden: {private_path}")
    data = private_path.read_bytes()
    pw = passphrase.encode("utf-8") or None
    try:
        priv = serialization.load_ssh_private_key(data, pw)
    except Exception:  # noqa: BLE001 – falsches Format, Passphrase, fehlendes bcrypt
        try:
            priv = serialization.load_pem_private_key(data, pw)
        except Exception as exc:  # noqa: BLE001
            raise KeyError_("Schlüssel ist verschlüsselt oder ungültig (Passphrase prüfen).") from exc
    pub = priv.public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
    return pub.decode("ascii") + " " + key_comment()


def key_needs_passphrase(private_path: Path) -> bool:
    private_path = Path(private_path).expanduser()
    if not private_path.exists():
        return False
    data = private_path.read_bytes()
    for loader in (serialization.load_ssh_private_key, serialization.load_pem_private_key):
        try:
            loader(data, None)
            return False
        except TypeError:
            return True  # "Password was not given but private key is encrypted"
        except ValueError:
            continue
    return False


def fingerprint(pub_line: str) -> str:
    parts = pub_line.strip().split()
    if len(parts) < 2:
        return "ungültig"
    try:
        raw = base64.b64decode(parts[1])
    except ValueError:
        return "ungültig"
    return "SHA256:" + base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")


def describe_key(private_path: Path) -> str:
    try:
        line = read_public_line(private_path)
        return f"{Path(private_path).name} ({fingerprint(line)[:19]}…)"
    except Exception:
        return "kein Schlüssel"


# Wird mit dem Public Key auf stdin ausgeführt – der Schlüssel landet nie in der Befehlszeile.
INSTALL_SCRIPT = (
    "umask 077; KEY=$(cat); "
    "[ -n \"$KEY\" ] || { echo 'leerer Schluessel' >&2; exit 2; }; "
    "mkdir -p \"$HOME/.ssh\" && chmod 700 \"$HOME/.ssh\" && "
    "touch \"$HOME/.ssh/authorized_keys\" && chmod 600 \"$HOME/.ssh/authorized_keys\" && "
    "if grep -qxF \"$KEY\" \"$HOME/.ssh/authorized_keys\"; then echo VORHANDEN; "
    "else printf '%s\\n' \"$KEY\" >> \"$HOME/.ssh/authorized_keys\" && echo EINGETRAGEN; fi"
)


def install_public_key(conn, pub_line: str) -> str:
    """Trägt den Schlüssel in ~/.ssh/authorized_keys ein (ohne Duplikate). conn = SshConnection."""
    import shlex
    rc, out, err = conn.exec("sh -c " + shlex.quote(INSTALL_SCRIPT), timeout=30,
                             stdin_data=(pub_line.strip() + "\n").encode())
    if rc != 0:
        raise KeyError_(f"Schlüssel konnte nicht eingetragen werden: {err.strip() or out.strip()}")
    return "bereits vorhanden" if "VORHANDEN" in out else "eingetragen"
