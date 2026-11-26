# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Erzeugt das Programm-Symbol (eigener Entwurf: Schild mit V und Terminal-Strich).

Aufruf: python build/icons/make_icon.py  (braucht Pillow; die fertigen Dateien liegen schon bei)
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
PKG = HERE.parent.parent / "vanguard_commander" / "resources"

S = 1024
img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
# abgerundetes dunkles Quadrat
d.rounded_rectangle([40, 40, S - 40, S - 40], radius=200, fill=(30, 33, 40, 255), outline=(62, 68, 81, 255), width=14)
# Schild
cx, top, bot, w = S // 2, 170, 880, 560
shield = [(cx - w // 2, top + 60), (cx, top), (cx + w // 2, top + 60), (cx + w // 2, 520),
          (cx + 120, 780), (cx, bot), (cx - 120, 780), (cx - w // 2, 520)]
glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(glow).polygon(shield, fill=(97, 175, 239, 140))
img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(28)))
d.polygon(shield, fill=(31, 58, 88, 255), outline=(97, 175, 239, 255), width=18)
# V
d.line([(cx - 170, 340), (cx, 680), (cx + 170, 340)], fill=(152, 195, 121, 255), width=70, joint="curve")
# Terminal-Cursor
d.rectangle([cx + 70, 700, cx + 160, 728], fill=(229, 192, 123, 255))
PKG.mkdir(exist_ok=True)
img.save(HERE / "vanguard.png")
img.resize((256, 256), Image.LANCZOS).save(PKG / "vanguard.png")
img.save(HERE / "vanguard.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
img.save(HERE / "vanguard.icns")
print("ok")
