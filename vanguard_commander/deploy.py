# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Automatische Bereitstellung des Daemons beim Verbinden (wenn Docker auf dem Server läuft).

Ablauf:
 1. Secret in ~/.vanguard/daemon.secret des SSH-Benutzers lesen oder neu erzeugen (0600).
 2. Läuft schon ein Daemon in passender Version auf dem Port? -> fertig.
 3. Sonst: Docker prüfen, Skript nach ~/.vanguard/ hochladen, Container "vanguard-node" (neu) starten.
 4. Warten, bis der Daemon antwortet.
Auf dem Server wird nichts gelöscht; alte Container mit gleichem Namen werden ersetzt.
"""

from __future__ import annotations

import os
import re
import shlex
import time
from typing import Callable

from . import paths
from .backends import DaemonBackend
from .protocol import PROTOCOL

CONTAINER = "vanguard-node"
IMAGE = "python:3.12-slim"
REMOTE_DIR = ".vanguard"
DAEMON_RES = "vanguard_commander/server/vanguard_node_daemon.py"

Log = Callable[[str], None]


class DeployError(Exception):
    pass


class DockerUnavailable(DeployError):
    pass


class NeedSudo(DeployError):
    """Docker braucht sudo und es ist (noch) kein sudo-Passwort bekannt bzw. es war falsch."""


def bundled_daemon() -> tuple[bytes, str]:
    src = paths.resource_path(DAEMON_RES).read_bytes()
    m = re.search(rb'^VERSION = "([^"]+)"', src, re.M)
    return src, (m.group(1).decode() if m else "0")


def remote_dir(conn) -> str:
    return f"{conn.home.rstrip('/')}/{REMOTE_DIR}"


def get_or_create_secret(conn) -> bytes:
    rdir = remote_dir(conn)
    sftp = conn.open_sftp()
    try:
        try:
            sftp.stat(rdir)
        except FileNotFoundError:
            sftp.mkdir(rdir, 0o700)
        sftp.chmod(rdir, 0o700)
        path = f"{rdir}/daemon.secret"
        try:
            with sftp.open(path, "rb") as fh:
                secret = fh.read().strip()
        except FileNotFoundError:
            secret = b""
        if len(secret) < 32:
            secret = os.urandom(32).hex().encode("ascii")
            with sftp.open(path, "wb") as fh:
                fh.write(secret + b"\n")
        sftp.chmod(path, 0o600)
        return secret
    finally:
        sftp.close()


def try_ping(conn, port: int, secret: bytes) -> DaemonBackend | None:
    backend = DaemonBackend(conn, port, secret)
    try:
        backend.ping()
        return backend
    except Exception:
        return None


def docker_prefix(conn, sudo_password: str) -> tuple[str, bytes | None]:
    probe = "info --format '{{.ServerVersion}}'"
    rc, out, err = conn.exec(f"docker {probe}", timeout=40)
    if rc == 0:
        return "docker", None
    low = (out + err).lower()
    if rc == 127 or "not found" in low:
        raise DockerUnavailable("Docker ist auf dem Server nicht installiert.")
    if "permission denied" in low:
        if not sudo_password:
            raise NeedSudo("Docker darf nur mit sudo benutzt werden.")
        rc, out, err = conn.exec(f"sudo -S -p '' docker {probe}", timeout=40,
                                 stdin_data=(sudo_password + "\n").encode())
        if rc == 0:
            return "sudo -S -p '' docker", (sudo_password + "\n").encode()
        if "incorrect password" in err.lower() or "sorry" in err.lower():
            raise NeedSudo("Das sudo-Passwort wurde abgelehnt.")
        low = (out + err).lower()
    if "cannot connect" in low or "is the docker daemon running" in low:
        raise DockerUnavailable("Docker ist installiert, läuft aber nicht.")
    raise DockerUnavailable(f"Docker nicht nutzbar: {(err or out).strip()[:300]}")


def ensure_daemon(conn, profile: dict, sudo_password: str = "", log: Log = lambda m: None,
                  force: bool = False) -> DaemonBackend:
    port = int(profile.get("daemon_port", 50105))
    src, version = bundled_daemon()
    secret = get_or_create_secret(conn)

    backend = try_ping(conn, port, secret)
    if not force and backend and backend.info.get("version") == version and backend.info.get("protocol") == PROTOCOL:
        log(f"Daemon v{version} läuft bereits auf Port {port}.")
        return backend
    if backend:
        log(f"Daemon v{backend.info.get('version')} gefunden – aktualisiere auf v{version}.")

    prefix, stdin = docker_prefix(conn, sudo_password)

    def docker(args: str, timeout: int = 120) -> tuple[int, str, str]:
        return conn.exec(f"{prefix} {args}", timeout=timeout, stdin_data=stdin)

    rdir = remote_dir(conn)
    log(f"Lade Daemon v{version} nach {rdir} hoch …")
    sftp = conn.open_sftp()
    try:
        with sftp.open(f"{rdir}/vanguard_node_daemon.py", "wb") as fh:
            fh.write(src)
        sftp.chmod(f"{rdir}/vanguard_node_daemon.py", 0o600)
    finally:
        sftp.close()

    docker(f"rm -f {CONTAINER}", timeout=60)
    run = (
        f"run -d --name {CONTAINER} --restart unless-stopped "
        f"--privileged --pid=host --network=host "
        f"--label vanguard.version={shlex.quote(version)} "
        f"-v {shlex.quote(rdir)}:/opt/vanguard_ssh:ro "
        f"-e VANGUARD_HOST_NS=1 -e VANGUARD_PORT={port} "
        f"-e VANGUARD_SECRET_FILE=/opt/vanguard_ssh/daemon.secret "
        f"{IMAGE} python3 -u /opt/vanguard_ssh/vanguard_node_daemon.py"
    )
    log(f"Starte Container {CONTAINER} ({IMAGE}; beim ersten Mal wird das Image geladen) …")
    rc, out, err = docker(run, timeout=900)
    if rc != 0:
        raise DeployError(f"Container konnte nicht gestartet werden:\n{(err or out).strip()[:800]}")

    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        backend = try_ping(conn, port, secret)
        if backend:
            log(f"Daemon v{backend.info.get('version')} bereit (root auf {backend.info.get('hostname')}).")
            return backend
        time.sleep(1)

    _, logs, logs_err = docker(f"logs --tail 25 {CONTAINER}", timeout=30)
    hint = ""
    if "address already in use" in (logs + logs_err).lower():
        hint = (f"\nPort {port} ist auf dem Server schon belegt (z. B. vom alten vanguard_daemon). "
                f"Im Profil einen anderen Daemon-Port wählen.")
    raise DeployError(f"Der Daemon antwortet nicht.{hint}\nLetzte Container-Meldungen:\n{(logs + logs_err).strip()[-1500:]}")


def remove_daemon(conn, sudo_password: str = "") -> str:
    """Stoppt und entfernt nur den Container. Die Dateien in ~/.vanguard bleiben erhalten."""
    prefix, stdin = docker_prefix(conn, sudo_password)
    rc, out, err = conn.exec(f"{prefix} rm -f {CONTAINER}", timeout=60, stdin_data=stdin)
    if rc != 0:
        raise DeployError((err or out).strip())
    return f"Container {CONTAINER} entfernt (Dateien in ~/{REMOTE_DIR} bleiben erhalten)."
