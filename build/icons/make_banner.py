# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 The Vanguard Commander Authors (siehe AUTHORS)
"""Erzeugt docs/social-preview.png (1280×640, GitHub-Vorschau) und docs/banner.png (README-Kopf).

Aufruf: python build/icons/make_banner.py   (braucht Pillow und die Schrift Inter oder DejaVu)
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
ICON = Image.open(ROOT / "build" / "icons" / "vanguard.png").convert("RGBA")


def font(name: str, size: int):
    for p in (f"/usr/share/fonts/opentype/inter/{name}.otf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
              if "Bold" in name else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def mono(size: int):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", size)
    except OSError:
        return ImageFont.load_default()


def background(w: int, h: int) -> Image.Image:
    img = Image.new("RGBA", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):  # senkrechter Verlauf
        t = y / h
        d.line([(0, y), (w, y)], fill=(int(20 + 10 * t), int(22 + 15 * t), int(28 + 22 * t), 255))
    grid = Image.new("RGBA", (w, h), (0, 0, 0, 0))  # dezentes Raster, eigene Ebene (sonst geht Transparenz verloren)
    g = ImageDraw.Draw(grid)
    for x in range(0, w, 40):
        g.line([(x, 0), (x, h)], fill=(255, 255, 255, 10))
    for y in range(0, h, 40):
        g.line([(0, y), (w, y)], fill=(255, 255, 255, 10))
    img = Image.alpha_composite(img, grid)
    glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse([-200, -260, 700, 640], fill=(97, 175, 239, 45))
    return Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(90)))


def chip(d: ImageDraw.ImageDraw, x: int, y: int, text: str, color, f) -> int:
    w = int(d.textlength(text, font=f)) + 28
    d.rounded_rectangle([x, y, x + w, y + 38], radius=19, fill=(36, 42, 52, 255), outline=color, width=2)
    d.text((x + 14, y + 8), text, font=f, fill=color)
    return x + w + 12


def terminal(img: Image.Image, x: int, y: int, w: int, h: int, size: int = 17):
    panel = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(panel)
    d.rounded_rectangle([x + 6, y + 10, x + w + 6, y + h + 10], radius=14, fill=(0, 0, 0, 110))
    panel = panel.filter(ImageFilter.GaussianBlur(10))
    img.alpha_composite(panel)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([x, y, x + w, y + h], radius=14, fill=(16, 18, 22, 255), outline=(62, 68, 81, 255), width=2)
    for i, c in enumerate([(224, 108, 117), (229, 192, 123), (152, 195, 121)]):
        d.ellipse([x + 16 + i * 22, y + 14, x + 30 + i * 22, y + 28], fill=c)
    f = mono(size)
    lines = [
        ((97, 175, 239), "$ connect admin@webserver"),
        ((152, 195, 121), "✔ SSH key · daemon v5.1 (root)"),
        ((97, 175, 239), "$ compose pull && up -d"),
        ((171, 178, 191), "  web       Pulled"),
        ((171, 178, 191), "  database  Running"),
        ((152, 195, 121), "  proxy     Recreated"),
        ((229, 192, 123), "CPU 12 % · RAM 2,1/8 GB · / 41 %"),
    ]
    ly = y + 48
    for color, text in lines:
        if ly + size + 4 > y + h - 8:
            break
        d.text((x + 20, ly), text, font=f, fill=color)
        ly += int(size * 1.6)


def render(w: int, h: int, compact: bool) -> Image.Image:
    img = background(w, h)
    d = ImageDraw.Draw(img)
    isz = 170 if compact else 230
    iy = (h - isz) // 2 if compact else 70
    img.alpha_composite(ICON.resize((isz, isz), Image.LANCZOS), (60, iy))
    tx = 60 + isz + 36
    title = font("Inter-Bold", 64 if compact else 72)
    sub = font("Inter-Medium", 25 if compact else 28)
    small = font("Inter-SemiBold", 18)
    ty = iy + (10 if compact else 18)
    d.text((tx, ty), "Vanguard Commander", font=title, fill=(232, 236, 242))
    d.text((tx, ty + (82 if compact else 98)), "Manage your Linux servers from the desktop", font=sub,
           fill=(168, 200, 236))
    d.text((tx, ty + (118 if compact else 140)), "Files · Docker Compose · Live logs · Real SSH terminals",
           font=sub, fill=(152, 160, 172))
    cy = ty + (166 if compact else 196)
    cx = tx
    for text, color in (("Windows", (168, 200, 236)), ("macOS", (168, 200, 236)), ("Linux", (168, 200, 236)),
                        ("Open Source · GPL v3", (143, 209, 138))):
        cx = chip(d, cx, cy, text, color, small)
    if not compact:
        terminal(img, 60, h - 262, w - 120, 222, size=16)
    else:
        terminal(img, w - 400, 40, 360, h - 80, size=14)
    return img.convert("RGB")


if __name__ == "__main__":
    out = ROOT / "docs"
    out.mkdir(exist_ok=True)
    render(1280, 640, compact=False).save(out / "social-preview.png", optimize=True)
    render(1600, 380, compact=True).save(out / "banner.png", optimize=True)
    print("docs/social-preview.png, docs/banner.png erzeugt")
