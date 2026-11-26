#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""
Vanguard Node Daemon v5 – serverseitiger Teil des Vanguard Commanders.

Wird vom Commander automatisch bereitgestellt:
  * per SSH nach ~/.vanguard/ hochgeladen
  * als Docker-Container "vanguard-node" gestartet (privilegiert, --pid=host, --network=host)
  * im Container wechselt der Prozess beim Start in den Mount-/UTS-/IPC-Namespace des Hosts
    (VANGUARD_HOST_NS=1). Befehle und Dateizugriffe wirken dadurch direkt auf dem Host als root.

Sicherheitsmodell (ehrlich):
  * Der Daemon lauscht nur auf 127.0.0.1 und ist nur über einen SSH-Kanal erreichbar.
  * Jede Anfrage ist per HMAC-SHA256 mit dem Secret signiert (alle Felder), mit Zeitfenster
    und Einmal-Nonce. Jede Antwort ist ebenfalls signiert.
  * Wer per SSH auf den Benutzer zugreifen kann, kann das Secret lesen und hat damit root –
    genau wie jedes Mitglied der Gruppe "docker". Eine Befehls-Positivliste wäre nur Schein.
  * Schutz vor Versehen bleibt: geschützte Pfade (Schatten-Passwörter, sudoers, SSH-Host-Keys, ...)
    werden nie überschrieben, und Systemordner wie /, /etc, /usr oder /home/<user> nie gelöscht.

Nur Python-Standardbibliothek. Ausführung im Container mit Python >= 3.12 (os.setns).
"""

from __future__ import annotations

# Alle Module VOR dem Namespace-Wechsel laden – danach sieht der Prozess das Dateisystem des Hosts.
import grp
import hashlib
import hmac
import json
import os
import posixpath
import pwd
import re
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections import OrderedDict
from typing import Any

VERSION = "5.1.0"
PROTOCOL = 5


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


BIND = os.environ.get("VANGUARD_BIND", "127.0.0.1")
PORT = _env_int("VANGUARD_PORT", 50105)
SECRET_FILE = os.environ.get("VANGUARD_SECRET_FILE", "/opt/vanguard_ssh/daemon.secret")
HOST_NS = os.environ.get("VANGUARD_HOST_NS", "0") == "1"

REPLAY_WINDOW = 300
MAX_HEADER = 1024 * 1024
MAX_CLIENTS = 32
SOCKET_TIMEOUT = 120
EXEC_DEFAULT_TIMEOUT = 600
EXEC_MAX_TIMEOUT = 7200
OUTPUT_LIMIT = 8 * 1024 * 1024
LIST_LIMIT = 20000
SCAN_LIMIT = 200000
CHUNK = 1024 * 1024

PROTECTED_WRITE = [re.compile(p) for p in (
    r"^/etc/shadow.*$", r"^/etc/gshadow.*$", r"^/etc/passwd-?$", r"^/etc/group-?$",
    r"^/etc/sudoers(\.d(/.*)?)?$", r"^/etc/pam\.d(/.*)?$", r"^/etc/ssh/ssh_host_.*$",
    r"^/root/\.ssh(/.*)?$", r"^/home/[^/]+/\.vanguard(/.*)?$", r"^/etc/vanguard(/.*)?$",
    r"^/opt/vanguard_ssh/daemon\.secret$", r"^/(proc|sys|dev)(/.*)?$", r"^/boot(/.*)?$",
)]
NEVER_DELETE_DEPTH2 = re.compile(r"^/(bin|boot|etc|home|lib|lib32|lib64|libx32|root|sbin|usr|var|proc|sys|dev|run)/[^/]+$")

NONCES: "OrderedDict[str, float]" = OrderedDict()
NONCE_LOCK = threading.Lock()
SLOTS = threading.BoundedSemaphore(MAX_CLIENTS)
LOG_LOCK = threading.Lock()


class RequestError(Exception):
    pass


class AuthError(Exception):
    pass


def log(msg: str) -> None:
    with LOG_LOCK:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- Protokoll
def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), CHUNK))
        if not chunk:
            raise ConnectionError("Verbindung getrennt")
        buf.extend(chunk)
    return bytes(buf)


def recv_frame(sock: socket.socket, limit: int) -> dict:
    size = int.from_bytes(recv_exact(sock, 4), "big")
    if size < 2 or size > limit:
        raise AuthError("Ungültige Rahmenlänge")
    try:
        obj = json.loads(recv_exact(sock, size).decode("utf-8"))
    except ValueError as exc:
        raise AuthError("Ungültiges JSON") from exc
    if not isinstance(obj, dict):
        raise AuthError("Ungültige Anfrage")
    return obj


def send_frame(sock: socket.socket, obj: dict) -> None:
    data = canonical(obj)
    sock.sendall(len(data).to_bytes(4, "big") + data)


def send_signed(sock: socket.socket, payload: dict, secret: bytes, nonce: str) -> None:
    sig = hmac.new(secret, nonce.encode("ascii") + b":" + canonical(payload), hashlib.sha256).hexdigest()
    send_frame(sock, {"payload": payload, "sig": sig})


def verify_request(hdr: dict, secret: bytes) -> str:
    token = hdr.get("token")
    nonce = hdr.get("nonce")
    ts = hdr.get("ts")
    if not isinstance(token, str) or not isinstance(nonce, str) or not isinstance(ts, int):
        raise AuthError("Unvollständige Anfrage")
    if not re.fullmatch(r"[0-9a-f]{32}", nonce):
        raise AuthError("Ungültige Nonce")
    signed = {k: v for k, v in hdr.items() if k != "token"}
    expected = hmac.new(secret, canonical(signed), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(token.lower(), expected):
        raise AuthError("Ungültige Signatur")
    now = time.time()
    if abs(now - ts) > REPLAY_WINDOW:
        raise AuthError("Zeitfenster überschritten (Uhrzeit von Client und Server prüfen)")
    with NONCE_LOCK:
        while NONCES:
            oldest, t = next(iter(NONCES.items()))
            if now - t <= REPLAY_WINDOW * 2:
                break
            NONCES.popitem(last=False)
        if nonce in NONCES:
            raise AuthError("Nonce bereits verwendet")
        NONCES[nonce] = now
    return nonce


# --------------------------------------------------------------------------- Pfade
def abs_path(raw: Any) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw or not raw.startswith("/"):
        raise RequestError("Pfad muss absolut sein")
    return posixpath.normpath(raw) if raw != "/" else "/"


def check_write(path: str) -> None:
    for pat in PROTECTED_WRITE:
        if pat.match(path):
            raise RequestError(f"{path} ist geschützt und wird nicht verändert")


def check_delete(path: str) -> None:
    if path == "/" or path.count("/") < 2 or NEVER_DELETE_DEPTH2.match(path):
        raise RequestError(f"{path} ist ein Systemordner und wird nicht gelöscht")
    check_write(path)


_OWNER_CACHE: dict[tuple[int, int], str] = {}


def owner_str(uid: int, gid: int) -> str:
    key = (uid, gid)
    if key not in _OWNER_CACHE:
        try:
            u = pwd.getpwuid(uid).pw_name
        except KeyError:
            u = str(uid)
        try:
            g = grp.getgrgid(gid).gr_name
        except KeyError:
            g = str(gid)
        _OWNER_CACHE[key] = f"{u}:{g}"
    return _OWNER_CACHE[key]


def mkdir_inherit(path: str) -> None:
    """Legt fehlende Ordner an; jeder neue Ordner erbt Besitzer und Gruppe seines Elternordners."""
    missing = []
    cur = path
    while not os.path.isdir(cur):
        if os.path.exists(cur):
            raise RequestError(f"{cur} existiert und ist kein Ordner")
        missing.append(cur)
        cur = posixpath.dirname(cur)
    for d in reversed(missing):
        check_write(d)
        parent_st = os.stat(posixpath.dirname(d))
        os.mkdir(d, 0o755)
        try:
            os.chown(d, parent_st.st_uid, parent_st.st_gid)
        except OSError:
            pass


# --------------------------------------------------------------------------- Aktionen
def act_ping(_: dict) -> dict:
    return {"status": "OK", "version": VERSION, "protocol": PROTOCOL, "host_ns": HOST_NS,
            "uid": os.geteuid(), "hostname": socket.gethostname()}


def _spawn(cmd: str, cwd: Any, stderr: int) -> subprocess.Popen:
    cwd = cwd if isinstance(cwd, str) and os.path.isdir(cwd) else "/"
    shell = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
    env = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin",
        "HOME": "/root", "USER": "root", "LOGNAME": "root",
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "dumb",
        "DEBIAN_FRONTEND": "noninteractive", "PAGER": "cat", "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0",
    }
    return subprocess.Popen([shell, "-c", cmd], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=stderr, cwd=cwd, env=env, start_new_session=True)


def _kill_group(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    for sig, wait in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
        try:
            os.killpg(proc.pid, sig)
        except OSError:
            return
        try:
            proc.wait(wait)
            return
        except subprocess.TimeoutExpired:
            continue


def handle_stream(sock: socket.socket, hdr: dict, secret: bytes, nonce: str) -> None:
    """Fortlaufende Ausgabe (docker logs -f, journalctl -f …).

    Ablauf: signiertes READY, dann Datenrahmen (4 Byte Länge + Bytes), Ende = Rahmen der Länge 0,
    danach signiertes Ergebnis mit Rückgabewert. Schließt der Client den Kanal, wird der Prozess beendet.
    """
    cmd = hdr.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        raise RequestError("Leerer Befehl")
    proc = _spawn(cmd, hdr.get("cwd"), stderr=subprocess.STDOUT)
    client_gone = False
    try:
        send_signed(sock, {"status": "READY"}, secret, nonce)
        fd = proc.stdout.fileno()
        while True:
            ready, _, _ = select.select([fd, sock], [], [], 30)
            if sock in ready:          # der Client sendet nichts – Lesbarkeit heißt: Kanal geschlossen
                client_gone = True
                break
            if fd in ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                sock.sendall(len(chunk).to_bytes(4, "big") + chunk)
    except OSError:
        client_gone = True
    finally:
        _kill_group(proc)
        proc.wait()
        proc.stdout.close()
    if not client_gone:
        sock.sendall((0).to_bytes(4, "big"))
        send_signed(sock, {"status": "OK", "rc": proc.returncode}, secret, nonce)


def act_exec(hdr: dict) -> dict:
    cmd = hdr.get("command")
    if not isinstance(cmd, str) or not cmd.strip():
        raise RequestError("Leerer Befehl")
    timeout = max(1, min(int(hdr.get("timeout") or EXEC_DEFAULT_TIMEOUT), EXEC_MAX_TIMEOUT))
    proc = _spawn(cmd, hdr.get("cwd"), stderr=subprocess.PIPE)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        out, err = proc.communicate()
    truncated = len(out) > OUTPUT_LIMIT or len(err) > OUTPUT_LIMIT
    res = {
        "status": "OK",
        "rc": -1 if timed_out else proc.returncode,
        "stdout": out[:OUTPUT_LIMIT].decode("utf-8", "replace"),
        "stderr": err[:OUTPUT_LIMIT].decode("utf-8", "replace"),
        "truncated": truncated,
    }
    if timed_out:
        res["stderr"] += f"\n[Vanguard] Abgebrochen nach {timeout} s Zeitüberschreitung."
    return res


def act_list(hdr: dict) -> dict:
    path = abs_path(hdr.get("path"))
    if not os.path.isdir(path):
        raise RequestError(f"Kein Ordner: {path}")
    entries = []
    with os.scandir(path) as it:
        for e in it:
            if len(entries) >= LIST_LIMIT:
                break
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            is_link = stat.S_ISLNK(st.st_mode)
            is_dir = stat.S_ISDIR(st.st_mode)
            target = ""
            if is_link:
                try:
                    target = os.readlink(e.path)
                    is_dir = os.path.isdir(e.path)
                except OSError:
                    pass
            entries.append({
                "name": e.name, "dir": is_dir, "link": is_link, "target": target,
                "size": st.st_size, "mtime": int(st.st_mtime),
                "mode": stat.filemode(st.st_mode), "owner": owner_str(st.st_uid, st.st_gid),
            })
    return {"status": "OK", "path": path, "entries": entries, "truncated": len(entries) >= LIST_LIMIT}


def act_scan(hdr: dict) -> dict:
    path = abs_path(hdr.get("path"))
    if not os.path.isdir(path):
        raise RequestError(f"Kein Ordner: {path}")
    base = posixpath.basename(path.rstrip("/")) or "root"
    files, dirs = [], [base]
    for root, dnames, fnames in os.walk(path, followlinks=False):
        rel_root = posixpath.relpath(root, path)
        prefix = base if rel_root == "." else f"{base}/{rel_root}"
        for d in dnames:
            dirs.append(f"{prefix}/{d}")
        for f in fnames:
            full = posixpath.join(root, f)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            files.append({"full": full, "rel": f"{prefix}/{f}", "size": st.st_size})
            if len(files) >= SCAN_LIMIT:
                raise RequestError(f"Mehr als {SCAN_LIMIT} Dateien – bitte kleineren Ordner wählen")
    return {"status": "OK", "files": files, "dirs": dirs}


def act_mkdir(hdr: dict) -> dict:
    path = abs_path(hdr.get("path"))
    check_write(path)
    mkdir_inherit(path)
    return {"status": "OK"}


def act_delete(hdr: dict) -> dict:
    raw = hdr.get("paths")
    if not isinstance(raw, list) or not raw:
        raise RequestError("Keine Pfade angegeben")
    targets = [abs_path(p) for p in raw]
    for t in targets:
        check_delete(t)
    deleted = 0
    for t in targets:
        if os.path.islink(t) or os.path.isfile(t):
            os.unlink(t)
            deleted += 1
        elif os.path.isdir(t):
            shutil.rmtree(t)
            deleted += 1
    return {"status": "OK", "deleted": deleted}


def handle_push(sock: socket.socket, hdr: dict, secret: bytes, nonce: str) -> str:
    path = abs_path(hdr.get("path"))
    size = int(hdr.get("size", -1))
    expected = str(hdr.get("sha256", "")).lower()
    if size < 0 or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise RequestError("Größe oder Prüfsumme fehlt")
    if os.path.islink(path) and os.path.isfile(path):
        path = os.path.realpath(path)          # Ziel des Links beschreiben, Link erhalten
    check_write(path)
    if os.path.isdir(path):
        raise RequestError(f"{path} ist ein Ordner")
    parent = posixpath.dirname(path)
    mkdir_inherit(parent)
    send_signed(sock, {"status": "READY"}, secret, nonce)

    fd, tmp = tempfile.mkstemp(prefix=".vanguard-up-", dir=parent)
    hasher = hashlib.sha256()
    try:
        with os.fdopen(fd, "wb") as fh:
            left = size
            while left > 0:
                chunk = sock.recv(min(CHUNK, left))
                if not chunk:
                    raise ConnectionError("Upload abgebrochen")
                fh.write(chunk)
                hasher.update(chunk)
                left -= len(chunk)
        if hasher.hexdigest() != expected:
            raise RequestError("Prüfsumme stimmt nicht – Datei wurde nicht übernommen")
        if os.path.exists(path):
            st = os.stat(path)                 # bestehende Datei: Besitzer und Rechte bleiben
            uid, gid, mode = st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)
        else:
            st = os.stat(parent)               # neue Datei: erbt Besitzer des Ordners
            uid, gid, mode = st.st_uid, st.st_gid, 0o644
        try:
            os.chown(tmp, uid, gid)
        except OSError:
            pass
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        tmp = ""
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass
    send_signed(sock, {"status": "OK", "bytes": size, "path": path}, secret, nonce)
    return path


def handle_pull(sock: socket.socket, hdr: dict, secret: bytes, nonce: str) -> str:
    path = abs_path(hdr.get("path"))
    if not os.path.isfile(path):
        raise RequestError(f"Datei nicht gefunden: {path}")
    with open(path, "rb") as fh:
        size = os.fstat(fh.fileno()).st_size
        send_signed(sock, {"status": "OK", "size": size}, secret, nonce)
        hasher = hashlib.sha256()
        left = size
        while left > 0:
            chunk = fh.read(min(CHUNK, left))
            if not chunk:
                break
            hasher.update(chunk)
            sock.sendall(chunk)
            left -= len(chunk)
        if left:  # Datei wurde während des Lesens kürzer – mit Nullen auffüllen, Prüfsumme meldet den Fehler
            sock.sendall(b"\0" * left)
            hasher.update(b"\0" * left)
    send_signed(sock, {"status": "OK", "sha256": hasher.hexdigest(), "complete": left == 0}, secret, nonce)
    return path


SIMPLE_ACTIONS = {"PING": act_ping, "EXEC": act_exec, "LIST": act_list, "SCAN": act_scan,
                  "MKDIR": act_mkdir, "DELETE": act_delete}


def describe(hdr: dict) -> str:
    action = hdr.get("action")
    if action in ("EXEC", "STREAM"):
        return f"{action} {str(hdr.get('command', ''))[:160]!r}"
    if action == "DELETE":
        return f"DELETE {hdr.get('paths')}"
    return f"{action} {hdr.get('path', '')}"


def handle_client(sock: socket.socket, secret: bytes) -> None:
    with sock:
        sock.settimeout(SOCKET_TIMEOUT)
        nonce = ""
        hdr: dict = {}
        try:
            hdr = recv_frame(sock, MAX_HEADER)
            nonce = verify_request(hdr, secret)
            action = hdr.get("action")
            if action == "PUSH":
                handle_push(sock, hdr, secret, nonce)
            elif action == "PULL":
                handle_pull(sock, hdr, secret, nonce)
            elif action == "STREAM":
                handle_stream(sock, hdr, secret, nonce)
            elif action in SIMPLE_ACTIONS:
                send_signed(sock, SIMPLE_ACTIONS[action](hdr), secret, nonce)
            else:
                raise RequestError(f"Unbekannte Aktion {action!r}")
            if action != "PING" and not hdr.get("quiet"):
                log(f"OK   {describe(hdr)}")
        except AuthError as exc:
            log(f"AUTH abgelehnt: {exc}")
            time.sleep(1.0)  # bremst Rateversuche
            try:
                send_frame(sock, {"status": "ERROR", "error": f"Authentifizierung: {exc}"})
            except OSError:
                pass
        except Exception as exc:  # noqa: BLE001 – jede Ausnahme wird dem Client gemeldet
            msg = str(exc) if isinstance(exc, (RequestError, OSError)) else f"Interner Fehler: {exc!r}"
            log(f"FEHL {describe(hdr)}: {msg}")
            try:
                if nonce:
                    send_signed(sock, {"status": "ERROR", "error": msg}, secret, nonce)
                else:
                    send_frame(sock, {"status": "ERROR", "error": msg})
            except OSError:
                pass


# --------------------------------------------------------------------------- Start
def load_secret() -> bytes:
    try:
        with open(SECRET_FILE, "rb") as fh:
            secret = fh.read().strip()
    except OSError as exc:
        sys.exit(f"Secret-Datei {SECRET_FILE} nicht lesbar: {exc}")
    if len(secret) < 32:
        sys.exit("Secret ist zu kurz (mindestens 32 Zeichen).")
    return secret


def enter_host_namespaces() -> None:
    """Wechselt in Mount-, UTS- und IPC-Namespace von PID 1 des Hosts (Container mit --pid=host)."""
    if not hasattr(os, "setns"):
        sys.exit("VANGUARD_HOST_NS=1 braucht Python >= 3.12 (os.setns).")
    pwd.getpwuid(0)
    grp.getgrgid(0)  # NSS-Module laden, solange noch das Container-Dateisystem sichtbar ist
    for name, flag in (("mnt", os.CLONE_NEWNS), ("uts", os.CLONE_NEWUTS), ("ipc", os.CLONE_NEWIPC)):
        try:
            fd = os.open(f"/proc/1/ns/{name}", os.O_RDONLY)
            try:
                os.setns(fd, flag)
            finally:
                os.close(fd)
        except OSError as exc:
            sys.exit(f"Wechsel in den Host-Namespace '{name}' fehlgeschlagen ({exc}). "
                     f"Der Container braucht --privileged und --pid=host (rootless Docker reicht nicht).")
    os.chdir("/")


def main() -> None:
    secret = load_secret()
    if HOST_NS:
        enter_host_namespaces()  # muss vor dem ersten Thread passieren
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((BIND, PORT))
    server.listen(MAX_CLIENTS)
    log(f"Vanguard Node Daemon {VERSION} aktiv auf {BIND}:{PORT} "
        f"(Host-Namespace: {'ja' if HOST_NS else 'nein'}, uid {os.geteuid()}, Host {socket.gethostname()})")

    def _stop(*_):
        log("Beende Daemon.")
        server.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _stop)

    while True:
        client, _addr = server.accept()
        if not SLOTS.acquire(blocking=False):
            client.close()
            continue

        def _work(s=client):
            try:
                handle_client(s, secret)
            finally:
                SLOTS.release()

        threading.Thread(target=_work, daemon=True).start()


if __name__ == "__main__":
    main()
