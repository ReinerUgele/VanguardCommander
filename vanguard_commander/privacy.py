# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Datenschutz-Modus: private Angaben in der Anzeige ersetzen (für Screenshots, Vorführungen, Videos).

Ohne Qt, damit testbar. Ersetzt nur die *Anzeige* – Daten und Befehle bleiben unverändert.
"""

from __future__ import annotations

import re

IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
IPV6 = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{1,4}:){2,7}[0-9A-Fa-f]{1,4}(?![0-9A-Fa-f:])")
_EDGE = r"A-Za-z0-9_"
_HARMLESS = {"0.0.0.0", "127.0.0.1", "255.255.255.255", "255.255.255.0"}


class Masker:
    def __init__(self):
        self.enabled = False
        self._terms: list[tuple[str, str]] = []
        self._pattern: re.Pattern | None = None

    def set_terms(self, pairs: list[tuple[str, str]]) -> None:
        """pairs = [(privat, Ersatz), …]; längere Angaben zuerst, damit nichts halb ersetzt wird."""
        seen, terms = set(), []
        for secret, repl in pairs:
            secret = (secret or "").strip()
            if len(secret) >= 2 and secret.lower() not in seen:
                seen.add(secret.lower())
                terms.append((secret, repl))
        terms.sort(key=lambda t: len(t[0]), reverse=True)
        self._terms = terms
        if terms:
            alts = "|".join(re.escape(s) for s, _ in terms)
            self._pattern = re.compile(rf"(?<![{_EDGE}])(?:{alts})(?![{_EDGE}])", re.IGNORECASE)
        else:
            self._pattern = None

    def _replacement(self, found: str) -> str:
        for secret, repl in self._terms:
            if found.lower() == secret.lower():
                return repl
        return "•••"

    def text(self, s: str) -> str:
        if not self.enabled or not s:
            return s
        if self._pattern is not None:
            s = self._pattern.sub(lambda m: self._replacement(m.group(0)), s)
        s = IPV4.sub(lambda m: m.group(0) if m.group(0) in _HARMLESS else "192.0.2.10", s)  # RFC 5737
        return IPV6.sub("2001:db8::10", s)  # Dokumentations-Adressbereich (RFC 3849)

    def spans(self, line: str) -> list[tuple[int, int]]:
        """Bereiche (Start, Ende) mit privaten Angaben – fürs Terminal, das Spalten nicht verschieben darf."""
        if not self.enabled or not line.strip():
            return []
        out = []
        if self._pattern is not None:
            out += [m.span() for m in self._pattern.finditer(line)]
        out += [m.span() for m in IPV4.finditer(line) if m.group(0) not in _HARMLESS]
        out += [m.span() for m in IPV6.finditer(line)]
        return out
