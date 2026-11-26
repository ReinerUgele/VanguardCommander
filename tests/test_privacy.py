# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Datenschutz-Modus (ohne Qt)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vanguard_commander.privacy import Masker  # noqa: E402


class MaskerTest(unittest.TestCase):
    def setUp(self):
        self.m = Masker()
        self.m.set_terms([("Webserver (Privat)", "Mein Server"), ("alice", "admin"),
                          ("host123.provider.example", "server"), ("x", "nie")])
        self.m.enabled = True

    def test_off_changes_nothing(self):
        self.m.enabled = False
        self.assertEqual(self.m.text("alice@10.1.2.3"), "alice@10.1.2.3")
        self.assertEqual(self.m.spans("alice"), [])

    def test_text(self):
        s = self.m.text("🟢 Verbunden (alice@178.254.33.133) · Home /home/alice · host123.provider.example")
        self.assertNotIn("alice", s)
        self.assertNotIn("178.254", s)
        self.assertNotIn("host123", s)
        self.assertIn("admin@192.0.2.10", s)
        self.assertIn("/home/admin", s)
        self.assertEqual(self.m.text("Profil Webserver (Privat)"), "Profil Mein Server")

    def test_word_boundaries_and_short_terms(self):
        self.assertEqual(self.m.text("malice alicex"), "malice alicex")   # nur ganze Wörter
        self.assertEqual(self.m.text("x"), "x")                          # zu kurze Angaben werden ignoriert
        self.assertEqual(self.m.text("v1.2.3"), "v1.2.3")                 # Versionsnummer ist keine IP
        self.assertIn("2001:db8::10", self.m.text("fe80:0:0:0:1:2:3:4"))
        self.assertEqual(self.m.text("0.0.0.0:22 127.0.0.1:631"), "0.0.0.0:22 127.0.0.1:631")  # nichts Privates

    def test_spans_for_terminal(self):
        line = "alice@host123.provider.example:~$ ping 10.0.0.1"
        spans = sorted(self.m.spans(line))
        covered = "".join("•" if any(a <= i < b for a, b in spans) else c for i, c in enumerate(line))
        self.assertEqual(len(covered), len(line))                         # Spalten bleiben gleich
        self.assertNotIn("alice", covered)
        self.assertNotIn("host123", covered)
        self.assertNotIn("10.0.0.1", covered)
        self.assertIn(":~$ ping ", covered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
