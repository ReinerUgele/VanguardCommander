# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Tests für Tresor, Schlüsselerzeugung und Profil-Migration (ohne Qt/paramiko)."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["VANGUARD_CONFIG_DIR"] = tempfile.mkdtemp(prefix="vg-cfg-")

from cryptography.hazmat.primitives import serialization  # noqa: E402

from vanguard_commander import config, keys, paths  # noqa: E402
from vanguard_commander.vault import Vault, VaultError  # noqa: E402


class VaultTest(unittest.TestCase):
    def test_roundtrip_and_wrong_password(self):
        path = Path(tempfile.mkdtemp()) / "vault.bin"
        v = Vault(path)
        v.create("geheim-123")
        v.set("Homelab", "sudo_password", "s3cret")
        raw = path.read_bytes()
        self.assertNotIn(b"s3cret", raw)  # nie im Klartext
        v2 = Vault(path)
        with self.assertRaises(VaultError):
            v2.unlock("falsch-falsch")
        v2.unlock("geheim-123")
        self.assertEqual(v2.get("Homelab", "sudo_password"), "s3cret")
        v2.change_password("neues-pass-9")
        v3 = Vault(path)
        v3.unlock("neues-pass-9")
        self.assertEqual(v3.get("Homelab", "sudo_password"), "s3cret")
        v3.rename_profile("Homelab", "Backup-Server")
        self.assertEqual(v3.get("Backup-Server", "sudo_password"), "s3cret")

    def test_short_master_password(self):
        with self.assertRaises(VaultError):
            Vault(Path(tempfile.mkdtemp()) / "v.bin").create("kurz")


class KeyTest(unittest.TestCase):
    def test_generate_and_read(self):
        d = Path(tempfile.mkdtemp())
        pub = keys.generate_ed25519(d / "id_test")
        self.assertTrue(pub.startswith("ssh-ed25519 "))
        self.assertEqual(keys.read_public_line(d / "id_test"), pub)
        self.assertTrue(keys.fingerprint(pub).startswith("SHA256:"))
        serialization.load_ssh_private_key((d / "id_test").read_bytes(), None)  # gültiges OpenSSH-Format
        self.assertFalse(keys.key_needs_passphrase(d / "id_test"))
        if os.name != "nt":
            self.assertEqual((d / "id_test").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(keys.KeyError_):
            keys.generate_ed25519(d / "id_test")  # nie überschreiben

    @unittest.skipUnless(__import__("importlib").util.find_spec("bcrypt"), "bcrypt nicht installiert")
    def test_passphrase_key_and_derive_pub(self):
        d = Path(tempfile.mkdtemp())
        pub = keys.generate_ed25519(d / "id_pw", passphrase="pp-123")
        self.assertTrue(keys.key_needs_passphrase(d / "id_pw"))
        (d / "id_pw.pub").rename(d / "weg.pub")
        derived = keys.read_public_line(d / "id_pw", "pp-123")
        self.assertEqual(derived.split()[:2], pub.split()[:2])


class ConfigTest(unittest.TestCase):
    def test_migration_drops_secret(self):
        home = Path(tempfile.mkdtemp())
        legacy = home / ".vanguard_commander.json"
        legacy.write_text(json.dumps({
            "active_profile": "Mini",
            "profiles": {
                "Mini": {"host": "192.168.1.50", "port": 50005, "secret": "ALTES-SECRET", "ssh_user": "alice",
                         "ssh_key": "~/.ssh/id_ed25519", "remote_workdir": "/opt/myapp", "use_ssh_tunnel": True},
                "Standard Server": {"host": "127.0.0.1", "secret": "", "ssh_user": "user"},
            },
            "custom_commands": [{"name": "df", "cmd": "df -h"}],
        }))
        orig = paths.legacy_config_file
        paths.legacy_config_file = lambda: legacy
        try:
            if paths.profiles_file().exists():
                paths.profiles_file().unlink()
            cfg = config.AppConfig.load()
        finally:
            paths.legacy_config_file = orig
        self.assertEqual(list(cfg.profiles), ["Mini"])  # Vorlage ohne Server fällt weg
        self.assertEqual(cfg.profiles["Mini"]["host"], "192.168.1.50")
        self.assertEqual(cfg.profiles["Mini"]["remote_workdir"], "/opt/myapp")
        stored = paths.profiles_file().read_text()
        self.assertNotIn("ALTES-SECRET", stored)
        self.assertNotIn("secret", stored)
        self.assertIn("ALTES-SECRET", legacy.read_text())  # alte Datei unverändert


if __name__ == "__main__":
    unittest.main(verbosity=2)
