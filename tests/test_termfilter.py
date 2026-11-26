# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Rahmen-Zeichen im Terminal (ohne Qt)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vanguard_commander.termfilter import AcsTranslator  # noqa: E402


class AcsTest(unittest.TestCase):
    def test_g0_box(self):
        t = AcsTranslator()
        out = t.feed(b"\x1b(0lqqk\x1b(B text").decode()
        self.assertIn("┌──┐", out)
        self.assertIn(" text", out)  # danach wieder normal

    def test_so_si_with_g1(self):
        t = AcsTranslator()
        out = t.feed(b"\x1b)0\x0ex\x0fx").decode()
        self.assertTrue(out.endswith("│x"), out)

    def test_escape_sequences_untouched(self):
        # 'm' in ESC[31m darf im Grafikmodus nicht zu '└' werden
        t = AcsTranslator()
        out = t.feed(b"\x1b(0\x1b[31mq\x1b[0m\x1b(B")
        self.assertIn(b"\x1b[31m", out)
        self.assertIn(b"\x1b[0m", out)
        self.assertIn("─".encode(), out)

    def test_split_across_chunks(self):
        t = AcsTranslator()
        a = t.feed(b"\x1b(")
        b = t.feed(b"0lk\x1b[1")
        c = t.feed(b";2Hx\x1b(B")
        out = (a + b + c).decode()
        self.assertIn("┌┐", out)
        self.assertIn("\x1b[1;2H│", out)

    def test_plain_text_and_utf8_unchanged(self):
        t = AcsTranslator()
        s = "Grüße lqk\r\n".encode()
        self.assertEqual(t.feed(s), s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
