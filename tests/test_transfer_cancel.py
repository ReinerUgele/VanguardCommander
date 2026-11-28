# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Abbrechen von SSH-Übertragungen darf nicht auf den Rest der Datei warten (ohne Netz, ohne Qt)."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vanguard_commander import backends  # noqa: E402


class Hang(Exception):
    """Steht für „würde warten, bis der ganze Rest der Datei angekommen ist“."""


class FakeChannel:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeFile:
    def __init__(self, sftp, size, limited=True):
        self.sftp, self.left, self.limited = sftp, size, limited
        self._prefetch_extents = {}
        self.prefetch_limit = None

    def prefetch(self, file_size=None, max_concurrent_requests=None):
        if not self.limited:
            raise TypeError("alte paramiko-Version")
        self.prefetch_limit = max_concurrent_requests
        self._prefetch_extents = {i: (i, 32768) for i in range(max_concurrent_requests or 10_000)}

    def read(self, n):
        n = min(n, self.left)
        self.left -= n
        return b"x" * n

    def set_pipelined(self, on=True):
        pass

    def write(self, data):
        self._prefetch_extents[len(self._prefetch_extents)] = (0, len(data))

    def close(self):
        # paramiko wartet beim Schließen auf alle ausstehenden Antworten – außer der Kanal ist schon zu.
        if self._prefetch_extents and not self.sftp.chan.closed:
            raise Hang()


class FakeSFTP:
    def __init__(self, size, limited=True):
        self.chan, self.size, self.limited = FakeChannel(), size, limited
        self.removed = []

    def get_channel(self):
        return self.chan

    def open(self, path, mode):
        return FakeFile(self, self.size, self.limited)

    def stat(self, path):
        raise FileNotFoundError(path)

    def remove(self, path):
        self.removed.append(path)

    def chmod(self, *a):
        pass

    def posix_rename(self, *a):
        pass

    def close(self):
        pass


class FakeConn:
    def __init__(self, size, limited=True):
        self.sessions = []
        self.size, self.limited = size, limited

    def open_sftp(self):
        s = FakeSFTP(self.size, self.limited)
        self.sessions.append(s)
        return s


def ssh_backend(conn):
    b = object.__new__(backends.SshBackend)
    b.conn = conn
    return b


class PullCancelTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = Path(self.tmp.name) / "model.gguf"

    def tearDown(self):
        self.tmp.cleanup()

    def test_cancel_mid_file_returns_at_once(self):
        conn = FakeConn(50 * 1024 * 1024)
        seen = [0]

        def on_bytes(n):
            seen[0] += n

        with self.assertRaises(backends.BackendError):
            ssh_backend(conn).pull_file("/r/model.gguf", self.dest, on_bytes, lambda: seen[0] >= 3 * 1024 * 1024)
        sftp = conn.sessions[0]
        self.assertTrue(sftp.chan.closed, "Kanal muss beim Abbruch sofort gekappt werden")
        self.assertFalse(self.dest.exists())
        self.assertFalse(self.dest.with_name("model.gguf.vanguard-part").exists())

    def test_prefetch_is_limited(self):
        conn = FakeConn(2 * 1024 * 1024)
        ssh_backend(conn).pull_file("/r/a.bin", self.dest)
        self.assertEqual(self.dest.stat().st_size, 2 * 1024 * 1024)
        self.assertFalse(conn.sessions[0].chan.closed)

    def test_old_paramiko_without_limit_still_works(self):
        conn = FakeConn(1024 * 1024, limited=False)
        ssh_backend(conn).pull_file("/r/a.bin", self.dest)
        self.assertEqual(self.dest.stat().st_size, 1024 * 1024)


class PushCancelTest(unittest.TestCase):
    def test_cancel_upload_cuts_channel_and_removes_temp(self):
        import io
        conn = FakeConn(0)
        calls = [0]

        def cancelled():
            calls[0] += 1
            return calls[0] > 2

        with self.assertRaises(backends.BackendError):
            ssh_backend(conn)._push_reader(io.BytesIO(b"y" * (8 * 1024 * 1024)), "/r/up.bin", lambda n: None, cancelled)
        self.assertTrue(conn.sessions[0].chan.closed)
        self.assertEqual(len(conn.sessions), 2, "Reste über eine frische Sitzung entfernen")
        self.assertTrue(conn.sessions[1].removed[0].startswith("/r/.vanguard-up-"))


if __name__ == "__main__":
    unittest.main()
