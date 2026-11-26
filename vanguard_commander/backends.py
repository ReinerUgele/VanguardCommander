# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Zwei Wege zum Server mit gleicher Schnittstelle:

* DaemonBackend – über den Vanguard-Daemon (läuft als root, signiert, schnell)
* SshBackend    – nur SSH/SFTP als angemeldeter Benutzer (Fallback ohne Docker); root-Befehle per sudo

Alle Methoden blockieren und laufen in Hintergrund-Threads der Oberfläche.
"""

from __future__ import annotations

import io
import os
import posixpath
import shlex
import socket
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import protocol
from .protocol import DaemonError, build_request, check_signed, parse_frame, recv_exact, recv_signed, send_frame

OnBytes = Callable[[int], None]
OnData = Callable[[bytes], None]
Cancelled = Callable[[], bool]


@dataclass
class ExecResult:
    rc: int
    out: str
    err: str
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0

    def text(self) -> str:
        parts = []
        if self.out.strip():
            parts.append(self.out.rstrip("\n"))
        if self.err.strip():
            parts.append(self.err.rstrip("\n"))
        if self.truncated:
            parts.append("[Ausgabe gekürzt]")
        if self.rc not in (0,):
            parts.append(f"[Rückgabewert {self.rc}]")
        return "\n".join(parts) if parts else "(keine Ausgabe)"


class BackendError(Exception):
    pass


class _Stop(Exception):
    """Interner Abbruch der Live-Ausgabe."""


def _no(*_a) -> bool:
    return False


RENAME_SCRIPT = ("if [ -e {dst} ] || [ -L {dst} ]; then echo \"$(basename {dst}) existiert bereits.\" >&2; exit 1; fi; "
                 "mv -T -- {src} {dst}")


def check_rename(src: str, dst: str) -> None:
    for p in (src, dst):
        if problem := protocol.write_problem(p):
            raise BackendError(problem)
    if problem := protocol.delete_problem(src):  # Systemordner nicht wegbenennen
        raise BackendError(problem.replace("gelöscht", "umbenannt"))


# =============================================================================== Daemon
class DaemonBackend:
    kind = "daemon"
    is_root = True

    def __init__(self, conn, port: int, secret: bytes):
        self.conn = conn
        self.port = int(port)
        self.secret = secret
        self.info: dict = {}

    @property
    def label(self) -> str:
        v = self.info.get("version", "?")
        return f"Daemon v{v} (root)"

    def _channel(self, timeout: float):
        ch = self.conn.open_daemon_channel(self.port)
        ch.settimeout(timeout)
        return ch

    def _call(self, action: str, params: dict, wait: float = 60) -> dict:
        hdr = build_request(self.secret, action, **params)
        ch = self._channel(wait)
        try:
            send_frame(ch, hdr)
            return recv_signed(ch, self.secret, hdr["nonce"])
        except (OSError, EOFError) as exc:
            raise DaemonError(f"Daemon-Verbindung: {exc or 'Zeitüberschreitung'}") from exc
        finally:
            ch.close()

    def ping(self) -> dict:
        self.info = self._call("PING", {}, wait=10)
        return self.info

    def exec(self, cmd: str, timeout: int = 600, as_root: bool = False, sudo_password: str = "",
             cwd: str | None = None, quiet: bool = False) -> ExecResult:
        params = {"command": cmd, "timeout": int(timeout)}
        if cwd:
            params["cwd"] = cwd
        if quiet:
            params["quiet"] = True  # Hintergrundabfragen nicht ins Daemon-Protokoll schreiben
        p = self._call("EXEC", params, wait=timeout + 30)
        return ExecResult(int(p.get("rc", -1)), p.get("stdout", ""), p.get("stderr", ""), bool(p.get("truncated")))

    def list_dir(self, path: str) -> list[dict]:
        return self._call("LIST", {"path": path}, wait=60)["entries"]

    def scan(self, path: str) -> tuple[list[dict], list[str]]:
        p = self._call("SCAN", {"path": path}, wait=300)
        return p["files"], p["dirs"]

    def mkdir(self, path: str) -> None:
        self._call("MKDIR", {"path": path})

    def delete(self, paths: list[str]) -> None:
        for p in paths:
            if problem := protocol.delete_problem(p):
                raise BackendError(problem)
        self._call("DELETE", {"paths": paths}, wait=600)

    def stream(self, cmd: str, on_data: OnData, cancelled: Cancelled, as_root: bool = False,
               sudo_password: str = "", cwd: str | None = None) -> int | None:
        """Fortlaufende Ausgabe. Gibt den Rückgabewert zurück oder None, wenn abgebrochen."""
        params = {"command": cmd}
        if cwd:
            params["cwd"] = cwd
        hdr = build_request(self.secret, "STREAM", **params)
        ch = self._channel(30)
        try:
            send_frame(ch, hdr)
            recv_signed(ch, self.secret, hdr["nonce"])  # READY
            ch.settimeout(0.4)
            buf = bytearray()

            def need(n: int) -> bytes:
                while len(buf) < n:
                    if cancelled():
                        raise _Stop()
                    try:
                        chunk = ch.recv(65536)
                    except socket.timeout:
                        continue
                    if not chunk:
                        raise DaemonError("Live-Ausgabe: Verbindung getrennt.")
                    buf.extend(chunk)
                out = bytes(buf[:n])
                del buf[:n]
                return out

            while True:
                size = int.from_bytes(need(4), "big")
                if size == 0:
                    break
                on_data(need(size))
            trailer = check_signed(parse_frame(need(int.from_bytes(need(4), "big"))), self.secret, hdr["nonce"])
            return int(trailer.get("rc", -1))
        except _Stop:
            return None
        finally:
            ch.close()

    def rename(self, src: str, dst: str) -> None:
        check_rename(src, dst)
        res = self.exec(RENAME_SCRIPT.format(src=shlex.quote(src), dst=shlex.quote(dst)), timeout=120)
        if not res.ok:
            raise BackendError(res.err.strip() or res.text())

    # --- Übertragung ---------------------------------------------------------------
    def _push_stream(self, reader: io.BufferedIOBase, size: int, sha: str, remote: str,
                     on_bytes: OnBytes, cancelled: Cancelled) -> None:
        if problem := protocol.write_problem(remote):
            raise BackendError(problem)
        hdr = build_request(self.secret, "PUSH", path=remote, size=size, sha256=sha)
        ch = self._channel(120)
        try:
            send_frame(ch, hdr)
            recv_signed(ch, self.secret, hdr["nonce"])  # READY
            left = size
            while left > 0:
                if cancelled():
                    raise DaemonError("Abgebrochen.")
                chunk = reader.read(min(protocol.CHUNK, left))
                if not chunk:
                    raise DaemonError("Lokale Datei wurde während des Uploads kürzer.")
                ch.sendall(chunk)
                left -= len(chunk)
                on_bytes(len(chunk))
            recv_signed(ch, self.secret, hdr["nonce"])
        finally:
            ch.close()

    def push_file(self, local: Path, remote: str, on_bytes: OnBytes = lambda n: None,
                  cancelled: Cancelled = _no) -> None:
        sha = protocol.sha256_file(local, cancelled)
        with open(local, "rb") as fh:
            self._push_stream(fh, os.path.getsize(local), sha, remote, on_bytes, cancelled)

    def push_bytes(self, data: bytes, remote: str) -> None:
        import hashlib
        self._push_stream(io.BytesIO(data), len(data), hashlib.sha256(data).hexdigest(), remote, lambda n: None, _no)

    def _pull_stream(self, remote: str, writer, on_bytes: OnBytes, cancelled: Cancelled, limit: int = 0) -> None:
        import hashlib
        hdr = build_request(self.secret, "PULL", path=remote)
        ch = self._channel(120)
        try:
            send_frame(ch, hdr)
            meta = recv_signed(ch, self.secret, hdr["nonce"])
            size = int(meta["size"])
            if limit and size > limit:
                raise BackendError(f"Datei ist zu groß für den Editor ({size // 1024 // 1024} MB).")
            h = hashlib.sha256()
            left = size
            while left > 0:
                if cancelled():
                    raise DaemonError("Abgebrochen.")
                chunk = recv_exact(ch, min(protocol.CHUNK, left))
                writer.write(chunk)
                h.update(chunk)
                left -= len(chunk)
                on_bytes(len(chunk))
            trailer = recv_signed(ch, self.secret, hdr["nonce"])
            if trailer.get("sha256") != h.hexdigest() or not trailer.get("complete", True):
                raise DaemonError(f"Prüfsumme falsch – {remote} wurde während der Übertragung verändert.")
        finally:
            ch.close()

    def pull_file(self, remote: str, local: Path, on_bytes: OnBytes = lambda n: None,
                  cancelled: Cancelled = _no) -> None:
        local = Path(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        part = local.with_name(local.name + ".vanguard-part")
        try:
            with open(part, "wb") as fh:
                self._pull_stream(remote, fh, on_bytes, cancelled)
            os.replace(part, local)
        finally:
            if part.exists():
                part.unlink()

    def pull_bytes(self, remote: str, limit: int = 5 * 1024 * 1024) -> bytes:
        buf = io.BytesIO()
        self._pull_stream(remote, buf, lambda n: None, _no, limit)
        return buf.getvalue()


# =============================================================================== SSH
class SshBackend:
    kind = "ssh"

    def __init__(self, conn, reason: str = ""):
        self.conn = conn
        self.reason = reason
        self.user = conn.profile.get("ssh_user", "")
        self.is_root = self.user == "root"

    @property
    def label(self) -> str:
        return f"Nur SSH ({self.user})"

    def exec(self, cmd: str, timeout: int = 600, as_root: bool = False, sudo_password: str = "",
             cwd: str | None = None, quiet: bool = False) -> ExecResult:
        if cwd:
            cmd = f"cd {shlex.quote(cwd)} && {cmd}"
        stdin = None
        if as_root and not self.is_root:
            if sudo_password:
                cmd = f"sudo -S -p '' bash -c {shlex.quote(cmd)}"
                stdin = (sudo_password + "\n").encode()
            else:
                cmd = f"sudo -n bash -c {shlex.quote(cmd)}"
        rc, out, err = self.conn.exec(cmd, timeout=timeout, stdin_data=stdin)
        if as_root and rc != 0 and ("a password is required" in err or "incorrect password" in err):
            err += "\n[Vanguard] sudo-Passwort fehlt oder ist falsch (Einstellungen → Tresor)."
        return ExecResult(rc, out, err)

    def stream(self, cmd: str, on_data: OnData, cancelled: Cancelled, as_root: bool = False,
               sudo_password: str = "", cwd: str | None = None) -> int | None:
        if cwd:
            cmd = f"cd {shlex.quote(cwd)} && {cmd}"
        if as_root and not self.is_root:
            cmd = f"sudo -S -p '' sh -c {shlex.quote(cmd)}" if sudo_password else f"sudo -n sh -c {shlex.quote(cmd)}"
        chan = self.conn.transport.open_session(timeout=15)
        try:
            chan.set_combine_stderr(True)
            chan.exec_command(cmd)
            if as_root and not self.is_root and sudo_password:
                chan.sendall((sudo_password + "\n").encode())
            chan.settimeout(0.4)
            while True:
                if cancelled():
                    return None
                try:
                    data = chan.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    break
                on_data(data)
            return chan.recv_exit_status()
        finally:
            chan.close()

    def list_dir(self, path: str) -> list[dict]:
        sftp = self.conn.open_sftp()
        try:
            entries = []
            for a in sftp.listdir_attr(path):
                mode = a.st_mode or 0
                is_link = stat.S_ISLNK(mode)
                is_dir = stat.S_ISDIR(mode)
                target = ""
                full = posixpath.join(path, a.filename)
                if is_link:
                    try:
                        target = sftp.readlink(full) or ""
                        is_dir = stat.S_ISDIR(sftp.stat(full).st_mode or 0)
                    except OSError:
                        pass
                parts = (a.longname or "").split()
                owner = f"{parts[2]}:{parts[3]}" if len(parts) >= 4 else f"{a.st_uid}:{a.st_gid}"
                entries.append({
                    "name": a.filename, "dir": is_dir, "link": is_link, "target": target,
                    "size": a.st_size or 0, "mtime": int(a.st_mtime or 0),
                    "mode": stat.filemode(mode), "owner": owner,
                })
            return entries
        except PermissionError as exc:
            raise BackendError(f"Keine Leserechte für {path} (im SSH-Modus läuft alles als {self.user}).") from exc
        finally:
            sftp.close()

    def scan(self, path: str) -> tuple[list[dict], list[str]]:
        sftp = self.conn.open_sftp()
        try:
            base = posixpath.basename(path.rstrip("/")) or "root"
            files, dirs = [], [base]
            stack = [(path, base)]
            while stack:
                cur, rel = stack.pop()
                for a in sftp.listdir_attr(cur):
                    mode = a.st_mode or 0
                    full, r = posixpath.join(cur, a.filename), f"{rel}/{a.filename}"
                    if stat.S_ISDIR(mode):
                        dirs.append(r)
                        stack.append((full, r))
                    elif stat.S_ISREG(mode):
                        files.append({"full": full, "rel": r, "size": a.st_size or 0})
            return files, dirs
        finally:
            sftp.close()

    def mkdir(self, path: str) -> None:
        sftp = self.conn.open_sftp()
        try:
            missing, cur = [], path
            while cur not in ("", "/"):
                try:
                    sftp.stat(cur)
                    break
                except FileNotFoundError:
                    missing.append(cur)
                    cur = posixpath.dirname(cur)
            for d in reversed(missing):
                sftp.mkdir(d, 0o755)
        finally:
            sftp.close()

    def delete(self, paths: list[str]) -> None:
        for p in paths:
            if problem := protocol.delete_problem(p):
                raise BackendError(problem)
        res = self.exec("rm -rf -- " + " ".join(shlex.quote(p) for p in paths), timeout=600)
        if not res.ok:
            raise BackendError(res.text())

    def rename(self, src: str, dst: str) -> None:
        check_rename(src, dst)
        sftp = self.conn.open_sftp()
        try:
            try:
                sftp.lstat(dst)
                raise BackendError(f"{posixpath.basename(dst)} existiert bereits.")
            except FileNotFoundError:
                pass
            sftp.posix_rename(src, dst)
        except PermissionError as exc:
            raise BackendError(f"Keine Schreibrechte (im SSH-Modus läuft alles als {self.user}).") from exc
        finally:
            sftp.close()

    def _push_reader(self, reader, remote: str, on_bytes: OnBytes, cancelled: Cancelled) -> None:
        if problem := protocol.write_problem(remote):
            raise BackendError(problem)
        sftp = self.conn.open_sftp()
        tmp = posixpath.join(posixpath.dirname(remote), f".vanguard-up-{os.urandom(4).hex()}")
        try:
            try:
                old_mode = stat.S_IMODE(sftp.stat(remote).st_mode or 0o644)
            except FileNotFoundError:
                old_mode = 0o644
            with sftp.open(tmp, "wb") as fh:
                fh.set_pipelined(True)
                while True:
                    if cancelled():
                        raise BackendError("Abgebrochen.")
                    chunk = reader.read(protocol.CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    on_bytes(len(chunk))
            sftp.chmod(tmp, old_mode)
            sftp.posix_rename(tmp, remote)
            tmp = ""
        finally:
            if tmp:
                try:
                    sftp.remove(tmp)
                except OSError:
                    pass
            sftp.close()

    def push_file(self, local: Path, remote: str, on_bytes: OnBytes = lambda n: None,
                  cancelled: Cancelled = _no) -> None:
        with open(local, "rb") as fh:
            self._push_reader(fh, remote, on_bytes, cancelled)

    def push_bytes(self, data: bytes, remote: str) -> None:
        self._push_reader(io.BytesIO(data), remote, lambda n: None, _no)

    def pull_file(self, remote: str, local: Path, on_bytes: OnBytes = lambda n: None,
                  cancelled: Cancelled = _no) -> None:
        local = Path(local)
        local.parent.mkdir(parents=True, exist_ok=True)
        part = local.with_name(local.name + ".vanguard-part")
        sftp = self.conn.open_sftp()
        try:
            with sftp.open(remote, "rb") as src, open(part, "wb") as dst:
                src.prefetch()
                while True:
                    if cancelled():
                        raise BackendError("Abgebrochen.")
                    chunk = src.read(protocol.CHUNK)
                    if not chunk:
                        break
                    dst.write(chunk)
                    on_bytes(len(chunk))
            os.replace(part, local)
        finally:
            sftp.close()
            if part.exists():
                part.unlink()

    def pull_bytes(self, remote: str, limit: int = 5 * 1024 * 1024) -> bytes:
        sftp = self.conn.open_sftp()
        try:
            if (sftp.stat(remote).st_size or 0) > limit:
                raise BackendError("Datei ist zu groß für den Editor.")
            with sftp.open(remote, "rb") as fh:
                return fh.read()
        finally:
            sftp.close()
