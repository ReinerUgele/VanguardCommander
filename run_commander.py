#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Startpunkt für Quellcode-Start und PyInstaller."""

import sys

from vanguard_commander.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
