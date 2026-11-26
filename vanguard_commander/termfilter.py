# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""DEC-Sonderzeichensatz (Linien/Rahmen) für das Terminal.

Programme wie whiptail, needrestart, mc oder dialog zeichnen Rahmen über den VT100-Grafikzeichensatz
(ESC ( 0 … ESC ( B bzw. ESC ) 0 + SO/SI). pyte ignoriert diese Umschaltung im UTF-8-Modus – dann
erscheinen Buchstaben wie "lqqk x mqj" statt Linien. Dieser Filter übersetzt die Zeichen vorher selbst.
Escape-Sequenzen bleiben unverändert, auch wenn sie über zwei Datenpakete verteilt ankommen.
"""

from __future__ import annotations

ACS = {
    "`": "◆", "a": "▒", "b": "␉", "c": "␌", "d": "␍", "e": "␊", "f": "°", "g": "±", "h": "␤", "i": "␋",
    "j": "┘", "k": "┐", "l": "┌", "m": "└", "n": "┼", "o": "⎺", "p": "⎻", "q": "─", "r": "⎼", "s": "⎽",
    "t": "├", "u": "┤", "v": "┴", "w": "┬", "x": "│", "y": "≤", "z": "≥", "{": "π", "|": "≠", "}": "£",
    "~": "·", "_": " ",
}
_ACS_BYTES = {ord(k): v.encode("utf-8") for k, v in ACS.items()}
ESC, SO, SI = 0x1B, 0x0E, 0x0F


class AcsTranslator:
    def __init__(self):
        self.g0_graphics = False
        self.g1_graphics = False
        self.use_g1 = False
        self._pending = b""

    def reset(self) -> None:
        self.__init__()

    @property
    def active(self) -> bool:
        return self.g1_graphics if self.use_g1 else self.g0_graphics

    def feed(self, data: bytes) -> bytes:
        data = self._pending + data
        self._pending = b""
        out = bytearray()
        i, n = 0, len(data)
        while i < n:
            b = data[i]
            if b == ESC:
                end = self._escape_end(data, i)
                if end is None:            # unvollständig – mit dem nächsten Paket weitermachen
                    self._pending = data[i:]
                    break
                seq = data[i:end]
                if len(seq) == 3 and seq[1] in (0x28, 0x29):   # ESC ( x  bzw.  ESC ) x
                    graphics = seq[2] == 0x30
                    if seq[1] == 0x28:
                        self.g0_graphics = graphics
                    else:
                        self.g1_graphics = graphics
                    # Umschaltung selbst erledigt – an pyte nur "ASCII" melden
                    out += b"\x1b" + bytes([seq[1]]) + b"B"
                else:
                    out += seq
                i = end
            elif b == SO:
                self.use_g1 = True
                i += 1
            elif b == SI:
                self.use_g1 = False
                i += 1
            elif self.active and b in _ACS_BYTES:
                out += _ACS_BYTES[b]
                i += 1
            else:
                out.append(b)
                i += 1
        return bytes(out)

    @staticmethod
    def _escape_end(data: bytes, i: int) -> int | None:
        """Ende (exklusiv) der Escape-Sequenz ab Position i, None wenn unvollständig."""
        n = len(data)
        if i + 1 >= n:
            return None
        kind = data[i + 1]
        if kind == 0x5B:                                   # CSI: ESC [ … Endbyte 0x40–0x7E
            j = i + 2
            while j < n:
                if 0x40 <= data[j] <= 0x7E:
                    return j + 1
                j += 1
            return None
        if kind in (0x5D, 0x50, 0x5F, 0x5E):              # OSC / DCS / APC / PM: bis BEL oder ESC \
            j = i + 2
            while j < n:
                if data[j] == 0x07:
                    return j + 1
                if data[j] == ESC and j + 1 < n and data[j + 1] == 0x5C:
                    return j + 2
                if data[j] == ESC and j + 1 >= n:
                    return None
                j += 1
            return None
        if 0x20 <= kind <= 0x2F:                           # ESC ( x, ESC ) x, ESC # x …
            return i + 3 if i + 2 < n else None
        return i + 2                                       # ESC x
