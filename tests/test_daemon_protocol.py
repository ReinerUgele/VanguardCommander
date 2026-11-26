# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Ende-zu-Ende-Test: echter Daemon-Prozess + DaemonBackend über lokale Sockets.

Läuft ohne Qt und ohne paramiko:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vanguard_commander import protocol  # noqa: E402
from vanguard_commander.backends import BackendError, DaemonBackend  # noqa: E402
from vanguard_commander.protocol import DaemonError  # noqa: E402

DAEMON = ROOT / "vanguard_commander" / "server" / "vanguard_node_daemon.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeConn:
    """Ersetzt die SSH-Verbindung: liefert einen normalen Socket statt eines paramiko-Kanals."""

    profile = {"ssh_user": "tester"}

    def open_daemon_channel(self, port, timeout=10):
        return socket.create_connection(("127.0.0.1", port), timeout=timeout)


@unittest.skipIf(os.name == "nt", "Der Daemon läuft nur auf Linux-Servern")
class DaemonTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="vg-test-"))
        cls.secret = os.urandom(32).hex().encode()
        (cls.tmp / "daemon.secret").write_bytes(cls.secret + b"\n")
        cls.port = free_port()
        env = dict(os.environ, VANGUARD_PORT=str(cls.port), VANGUARD_SECRET_FILE=str(cls.tmp / "daemon.secret"),
                   VANGUARD_HOST_NS="0")
        cls.proc = subprocess.Popen([sys.executable, "-u", str(DAEMON)], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", cls.port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        cls.backend = DaemonBackend(FakeConn(), cls.port, cls.secret)
        cls.work = cls.tmp / "work"
        cls.work.mkdir()

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)

    def test_ping(self):
        info = self.backend.ping()
        self.assertEqual(info["protocol"], protocol.PROTOCOL)
        from vanguard_commander.deploy import bundled_daemon
        self.assertEqual(info["version"], bundled_daemon()[1])

    def test_exec_shell_features(self):
        # Pipes, &&, Umleitung, Variablen und stderr – alles, was früher nicht ging
        res = self.backend.exec("echo hallo | tr a-z A-Z && X=7; echo $X; echo fehler >&2; exit 3", timeout=20)
        self.assertEqual(res.rc, 3)
        self.assertIn("HALLO", res.out)
        self.assertIn("7", res.out)
        self.assertIn("fehler", res.err)  # stderr geht nicht mehr verloren

    def test_exec_quiet_not_logged(self):
        res = self.backend.exec("echo leise-abfrage-xyz", quiet=True)
        self.assertTrue(res.ok)

    def test_exec_cwd_and_timeout(self):
        res = self.backend.exec("pwd", cwd=str(self.work))
        self.assertEqual(os.path.realpath(res.out.strip()), os.path.realpath(self.work))  # macOS: /var -> /private/var
        t0 = time.time()
        res = self.backend.exec("sleep 30", timeout=2)
        self.assertLess(time.time() - t0, 15)
        self.assertEqual(res.rc, -1)
        self.assertIn("Zeitüberschreitung", res.err)

    def test_list_with_spaces_and_symlink(self):
        d = self.work / "liste"
        d.mkdir()
        (d / "Datei mit Leerzeichen.txt").write_text("x")
        (d / "unter ordner").mkdir()
        os.symlink(d / "unter ordner", d / "link")
        names = {e["name"]: e for e in self.backend.list_dir(str(d))}
        self.assertIn("Datei mit Leerzeichen.txt", names)
        self.assertTrue(names["unter ordner"]["dir"])
        self.assertTrue(names["link"]["link"] and names["link"]["dir"])

    def test_push_pull_roundtrip_and_mode_kept(self):
        src = self.tmp / "src.bin"
        data = os.urandom(3 * 1024 * 1024 + 17)
        src.write_bytes(data)
        remote = str(self.work / "neu" / "tief" / "ziel.bin")
        seen = []
        self.backend.push_file(src, remote, on_bytes=seen.append)
        self.assertEqual(sum(seen), len(data))
        self.assertEqual(Path(remote).read_bytes(), data)
        self.assertEqual(os.stat(remote).st_mode & 0o777, 0o644)
        # bestehende Datei behält Rechte beim Überschreiben (früher: chown -R auf den ganzen Ordner)
        os.chmod(remote, 0o600)
        self.backend.push_bytes(b"neu", remote)
        self.assertEqual(Path(remote).read_bytes(), b"neu")
        self.assertEqual(os.stat(remote).st_mode & 0o777, 0o600)
        back = self.tmp / "back.bin"
        self.backend.push_file(src, remote)
        self.backend.pull_file(remote, back)
        self.assertEqual(hashlib.sha256(back.read_bytes()).hexdigest(), hashlib.sha256(data).hexdigest())
        self.assertEqual(self.backend.pull_bytes(remote, limit=10 * 1024 * 1024), data)

    def test_push_through_symlink_keeps_link(self):
        target = self.work / "echt.conf"
        target.write_text("alt")
        link = self.work / "verweis.conf"
        os.symlink(target, link)
        self.backend.push_bytes(b"neu", str(link))
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(), "neu")

    def test_scan(self):
        d = self.work / "baum"
        (d / "a" / "leer").mkdir(parents=True)
        (d / "a" / "f.txt").write_text("1")
        (d / "g.txt").write_text("22")
        files, dirs = self.backend.scan(str(d))
        self.assertEqual({f["rel"] for f in files}, {"baum/a/f.txt", "baum/g.txt"})
        self.assertIn("baum/a/leer", dirs)

    def test_mkdir_and_delete_guard(self):
        p = self.work / "x" / "y"
        self.backend.mkdir(str(p))
        self.assertTrue(p.is_dir())
        self.backend.delete([str(self.work / "x")])
        self.assertFalse((self.work / "x").exists())
        for forbidden in ("/", "/etc", "/home/alice", "/usr/lib", "/etc/shadow"):
            with self.assertRaises((BackendError, DaemonError), msg=forbidden):
                self.backend.delete([forbidden])

    def test_stream_complete(self):
        got = []
        rc = self.backend.stream("for i in 1 2 3; do echo zeile$i; sleep 0.2; done; echo fehler >&2; exit 4",
                                 got.append, lambda: False)
        text = b"".join(got).decode()
        self.assertEqual(rc, 4)
        self.assertIn("zeile3", text)
        self.assertIn("fehler", text)  # stderr kommt mit

    def test_stream_cancel_kills_process(self):
        marker = self.work / "stream_marker"
        got, stop = [], [False]

        def on_data(d):
            got.append(d)
            stop[0] = True  # nach der ersten Zeile abbrechen

        t0 = time.time()
        rc = self.backend.stream(f"echo start; sleep 3; touch {marker}", on_data, lambda: stop[0])
        self.assertIsNone(rc)
        self.assertLess(time.time() - t0, 2.5)
        time.sleep(4)
        self.assertFalse(marker.exists(), "Prozess hätte beim Abbruch beendet werden müssen")

    def test_rename(self):
        a = self.work / "alt name.txt"
        a.write_text("x")
        b = self.work / "neu name.txt"
        self.backend.rename(str(a), str(b))
        self.assertTrue(b.exists() and not a.exists())
        c = self.work / "belegt.txt"
        c.write_text("bleibt")
        with self.assertRaises(BackendError):
            self.backend.rename(str(b), str(c))  # nie überschreiben
        self.assertEqual(c.read_text(), "bleibt")
        with self.assertRaises(BackendError):
            self.backend.rename("/etc", "/etc2")

    def test_protected_write(self):
        with self.assertRaises((BackendError, DaemonError)):
            self.backend.push_bytes(b"x", "/etc/sudoers")

    def test_wrong_secret_rejected(self):
        bad = DaemonBackend(FakeConn(), self.port, b"f" * 64)
        with self.assertRaises(DaemonError) as cm:
            bad.exec("id")
        self.assertIn("Authentifizierung", str(cm.exception))

    def test_replay_rejected(self):
        hdr = protocol.build_request(self.secret, "PING")
        for expect_ok in (True, False):
            with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
                protocol.send_frame(s, hdr)
                if expect_ok:
                    protocol.recv_signed(s, self.secret, hdr["nonce"])
                else:
                    with self.assertRaises(DaemonError):
                        protocol.recv_signed(s, self.secret, hdr["nonce"])

    def test_tampered_field_rejected(self):
        hdr = protocol.build_request(self.secret, "EXEC", command="echo harmlos", timeout=5)
        hdr["command"] = "echo manipuliert"  # Feld nach dem Signieren geändert
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as s:
            protocol.send_frame(s, hdr)
            with self.assertRaises(DaemonError):
                protocol.recv_signed(s, self.secret, hdr["nonce"])

    def test_forged_response_rejected(self):
        class Fake:
            def __init__(self, data): self.data = data
            def recv(self, n):
                chunk, self.data = self.data[:n], self.data[n:]
                return chunk
        body = json.dumps({"payload": {"status": "OK"}, "sig": "00" * 32}).encode()
        with self.assertRaises(DaemonError):
            protocol.recv_signed(Fake(len(body).to_bytes(4, "big") + body), self.secret, "ab" * 16)


class GuardTest(unittest.TestCase):
    def test_client_rules_match_daemon(self):
        for p in ("/", "/etc", "/opt", "/home/x", "/usr/bin", "/var/lib"):
            self.assertTrue(protocol.delete_problem(p), p)
        for p in ("/opt/myapp/alt", "/var/www/html/alt", "/home/x/projekt", "/tmp/a"):
            self.assertFalse(protocol.delete_problem(p), p)
        self.assertTrue(protocol.write_problem("/etc/shadow"))
        self.assertFalse(protocol.write_problem("/etc/nginx/nginx.conf"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
