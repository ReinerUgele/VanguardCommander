# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Master-Passwort merken – Linux-Weg mit nachgebautem secret-tool (DPAPI/Schlüsselbund nur auf dem echten System)."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("VANGUARD_CONFIG_DIR", tempfile.mkdtemp(prefix="vg-cfg-"))

from vanguard_commander import remember  # noqa: E402

FAKE = """#!/bin/sh
store="$FAKE_STORE"
case "$1" in
  store) cat > "$store" ;;
  lookup) [ -f "$store" ] && cat "$store" || exit 1 ;;
  clear) rm -f "$store" ;;
esac
"""


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux-Weg")
class SecretToolTest(unittest.TestCase):
    def setUp(self):
        self.bin = Path(tempfile.mkdtemp())
        (self.bin / "secret-tool").write_text(FAKE)
        (self.bin / "secret-tool").chmod(0o755)
        self.old_path = os.environ["PATH"]
        os.environ["PATH"] = f"{self.bin}:{self.old_path}"
        os.environ["FAKE_STORE"] = str(self.bin / "store")

    def tearDown(self):
        os.environ["PATH"] = self.old_path

    def test_roundtrip(self):
        self.assertTrue(remember.available())
        self.assertIsNone(remember.load())
        remember.save("mein Master-Passwort ä")
        self.assertEqual(remember.load(), "mein Master-Passwort ä")
        self.assertTrue(remember.is_remembered())
        remember.clear()
        self.assertFalse(remember.is_remembered())


if __name__ == "__main__":
    unittest.main(verbosity=2)
