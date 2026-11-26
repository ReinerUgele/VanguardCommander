# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""SSH-Verbindung über paramiko: Anmeldung, Host-Key-Prüfung, Befehle, SFTP, Kanäle zum Daemon.

Es wird kein externes ssh-Programm gebraucht – das macht die Exe auf allen Systemen gleich.
"""

from __future__ import annotations

import base64
import hashlib
import socket
import threading
import time
from pathlib import Path
from typing import Any

import paramiko

from .config import effective_key_path


class SshError(Exception):
    pass


class HostKeyUnknown(SshError):
    def __init__(self, host: str, key_type: str, fp: str):
        super().__init__(f"Unbekannter Server-Schlüssel für {host}")
        self.host, self.key_type, self.fingerprint = host, key_type, fp


class HostKeyChanged(SshError):
    pass


class AuthNeeded(SshError):
    """kind = "password" | "key_passphrase" """
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def host_key_fingerprint(key: paramiko.PKey) -> str:
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


class _AskPolicy(paramiko.MissingHostKeyPolicy):
    """Unbekannte Server werden nur nach ausdrücklicher Bestätigung (Fingerabdruck) aufgenommen."""

    def __init__(self, accept_fp: str | None, known_hosts: Path):
        self.accept_fp = accept_fp
        self.known_hosts = known_hosts

    def missing_host_key(self, client, hostname, key):
        fp = host_key_fingerprint(key)
        if self.accept_fp and fp == self.accept_fp:
            client.get_host_keys().add(hostname, key.get_name(), key)
            try:
                client.save_host_keys(str(self.known_hosts))
            except OSError:
                pass
            return
        raise HostKeyUnknown(hostname, key.get_name(), fp)


class SshConnection:
    def __init__(self, profile: dict[str, Any], known_hosts: Path):
        self.profile = profile
        self.known_hosts = known_hosts
        self.client: paramiko.SSHClient | None = None
        self.home = ""
        self._lock = threading.Lock()

    # --- Aufbau ----------------------------------------------------------------
    def connect(self, password: str = "", key_passphrase: str = "", accept_fp: str | None = None,
                force_password: bool = False) -> None:
        host = self.profile.get("host", "").strip()
        user = self.profile.get("ssh_user", "").strip()
        if not host or not user:
            raise SshError("Im Profil fehlen Host oder SSH-Benutzer.")
        port = int(self.profile.get("ssh_port", 22) or 22)

        client = paramiko.SSHClient()
        try:
            client.load_system_host_keys()
        except (OSError, paramiko.SSHException):
            pass
        self.known_hosts.parent.mkdir(parents=True, exist_ok=True)
        if not self.known_hosts.exists():
            self.known_hosts.touch()
        client.load_host_keys(str(self.known_hosts))
        client.set_missing_host_key_policy(_AskPolicy(accept_fp, self.known_hosts))

        kwargs: dict[str, Any] = dict(
            hostname=host, port=port, username=user, timeout=12,
            banner_timeout=20, auth_timeout=20, look_for_keys=False, allow_agent=True,
        )
        use_key = self.profile.get("auth", "key") == "key" and not force_password
        key_path = effective_key_path(self.profile)
        if use_key:
            if not key_path.exists():
                raise AuthNeeded("password", f"Schlüssel {key_path} fehlt. Bitte in den Einstellungen "
                                             "erzeugen und auf dem Server hinterlegen – oder mit Passwort anmelden.")
            kwargs["key_filename"] = str(key_path)
            if key_passphrase:
                kwargs["passphrase"] = key_passphrase
        else:
            kwargs["allow_agent"] = False
        if password:
            kwargs["password"] = password

        try:
            client.connect(**kwargs)
        except HostKeyUnknown:
            client.close()
            raise
        except paramiko.BadHostKeyException as exc:
            client.close()
            raise HostKeyChanged(
                f"ACHTUNG: Der Server-Schlüssel von {host} hat sich geändert! Das kann ein Angriff sein "
                f"oder der Server wurde neu installiert. Verbindung abgebrochen.\n"
                f"Wenn die Änderung bekannt ist: Eintrag für {host} in {self.known_hosts} entfernen."
            ) from exc
        except paramiko.PasswordRequiredException as exc:
            client.close()
            raise AuthNeeded("key_passphrase", f"Der Schlüssel {key_path.name} ist mit einer Passphrase geschützt.") from exc
        except paramiko.AuthenticationException as exc:
            client.close()
            if password:
                raise SshError("Anmeldung abgelehnt – Passwort oder Schlüssel stimmt nicht.") from exc
            if use_key:
                raise AuthNeeded("password", "Der Server hat den Schlüssel nicht angenommen "
                                             "(noch nicht hinterlegt?).") from exc
            raise AuthNeeded("password", "Für diese Anmeldung wird das SSH-Passwort benötigt.") from exc
        except (socket.timeout, TimeoutError) as exc:
            client.close()
            raise SshError(f"Zeitüberschreitung beim Verbinden mit {host}:{port}.") from exc
        except (OSError, paramiko.SSHException) as exc:
            client.close()
            raise SshError(f"Verbindung zu {host}:{port} fehlgeschlagen: {exc}") from exc

        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(20)
        self.client = client
        try:
            sftp = client.open_sftp()
            self.home = sftp.normalize(".")
            sftp.close()
        except Exception:
            rc, out, _ = self.exec("printf %s \"$HOME\"", timeout=10)
            self.home = out.strip() if rc == 0 else f"/home/{user}"

    def close(self) -> None:
        if self.client is not None:
            try:
                self.client.close()
            finally:
                self.client = None

    @property
    def transport(self) -> paramiko.Transport:
        if self.client is None or self.client.get_transport() is None:
            raise SshError("Keine SSH-Verbindung.")
        return self.client.get_transport()

    def is_alive(self) -> bool:
        try:
            return self.client is not None and self.transport.is_active()
        except SshError:
            return False

    # --- Befehle -----------------------------------------------------------------
    def exec(self, command: str, timeout: float = 120, stdin_data: bytes | None = None) -> tuple[int, str, str]:
        """Führt einen Befehl aus. Liest stdout und stderr parallel (kein Hängenbleiben bei viel Ausgabe)."""
        chan = self.transport.open_session(timeout=15)
        try:
            chan.settimeout(timeout)
            chan.exec_command(command)
            if stdin_data:
                chan.sendall(stdin_data)
            chan.shutdown_write()
            out, err = bytearray(), bytearray()
            deadline = time.monotonic() + timeout
            while True:
                moved = False
                while chan.recv_ready():
                    out += chan.recv(65536)
                    moved = True
                while chan.recv_stderr_ready():
                    err += chan.recv_stderr(65536)
                    moved = True
                if chan.exit_status_ready() and not chan.recv_ready() and not chan.recv_stderr_ready():
                    break
                if not moved:
                    if time.monotonic() > deadline:
                        raise SshError(f"Zeitüberschreitung nach {int(timeout)} s: {command[:80]}")
                    time.sleep(0.02)
            rc = chan.recv_exit_status()
            return rc, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
        finally:
            chan.close()

    def open_sftp(self) -> paramiko.SFTPClient:
        if self.client is None:
            raise SshError("Keine SSH-Verbindung.")
        return self.client.open_sftp()

    def open_daemon_channel(self, port: int, timeout: float = 10) -> paramiko.Channel:
        """Kanal direkt zum Daemon auf 127.0.0.1:port des Servers – ohne lokalen Port, ohne ssh.exe."""
        try:
            return self.transport.open_channel("direct-tcpip", ("127.0.0.1", int(port)), ("127.0.0.1", 0),
                                               timeout=timeout)
        except paramiko.ChannelException as exc:
            raise SshError(f"Daemon auf Port {port} nicht erreichbar ({exc.text}).") from exc

    def open_shell(self, cols: int, rows: int) -> paramiko.Channel:
        chan = self.transport.open_session(timeout=15)
        chan.get_pty(term="xterm-256color", width=cols, height=rows)
        chan.invoke_shell()
        return chan
