#!/usr/bin/env python3
"""Draw the Map Animator app icon (map + route with A/B markers) and build AppIcon.icns."""
import math
import os
import shutil
import subprocess
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from scipy.interpolate import splev, splprep

OUT = sys.argv[1] if len(sys.argv) > 1 else "AppIcon.icns"
S = 2048               # supersampled canvas; downscaled to 1024
ART = 1648             # artwork square (macOS grid: 824/1024 of the canvas)
M = (S - ART) // 2
R = int(ART * 0.225)   # corner radius


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# --- background: soft terrain gradient (mint -> green) ---------------------------------
bg = Image.new("RGB", (ART, ART))
px = bg.load()
c1, c2 = (214, 240, 224), (150, 205, 170)
for y in range(ART):
    for x in range(ART):
        px[x, y] = lerp(c1, c2, (x * 0.35 + y * 0.65) / ART)
d = ImageDraw.Draw(bg, "RGBA")

# lake (water) bottom-left
lake = Image.new("L", (ART, ART), 0)
ld = ImageDraw.Draw(lake)
ld.ellipse([-260, ART - 700, 620, ART + 120], fill=255)
ld.ellipse([120, ART - 520, 700, ART - 120], fill=255)
lake = lake.filter(ImageFilter.GaussianBlur(6))
water = Image.new("RGB", (ART, ART), (150, 208, 240))
bg.paste(water, (0, 0), lake)
d = ImageDraw.Draw(bg, "RGBA")

# contour lines
for k in range(9):
    xs = np.linspace(-50, ART + 50, 200)
    ys = ART * (0.08 + 0.115 * k) + 60 * np.sin(xs / 260 + k * 0.9) + 40 * np.sin(xs / 130 + k)
    d.line(list(zip(xs.tolist(), ys.tolist())), fill=(60, 140, 100, 70), width=7, joint="curve")

# --- route -------------------------------------------------------------------------------
pts = np.array([[1330, 1370], [1090, 1250], [1200, 980], [900, 860], [760, 640], [980, 470], [1160, 330]], float)
tck, _ = splprep(pts.T, s=0, k=3)
u = np.linspace(0, 1, 1600)
rx, ry = splev(u, tck)
path = list(zip(rx.tolist(), ry.tolist()))


def stroke(draw, pts, width, fill):
    """Thick smooth line made of round stamps (PIL's wide lines get jagged on curves)."""
    r = width / 2
    for x, y in pts:
        draw.ellipse([x - r, y - r, x + r, y + r], fill=fill)


shadow = Image.new("RGBA", (ART, ART), (0, 0, 0, 0))
stroke(ImageDraw.Draw(shadow), [(x + 10, y + 16) for x, y in path], 92, (0, 40, 90, 110))
shadow = shadow.filter(ImageFilter.GaussianBlur(14))
bg.paste(shadow, (0, 0), shadow)
d = ImageDraw.Draw(bg, "RGBA")
stroke(d, path, 96, (255, 255, 255, 255))
stroke(d, path, 64, (0, 122, 255, 255))
for (x, y) in (path[0], path[-1]):
    d.ellipse([x - 32 - 16, y - 32 - 16, x + 32 + 16, y + 32 + 16], fill=(255, 255, 255, 255))
    d.ellipse([x - 32, y - 32, x + 32, y + 32], fill=(0, 122, 255, 255))

# --- markers A (start) and B (end) ------------------------------------------------------
font = None
for cand, idx in (("/System/Library/Fonts/Helvetica.ttc", 1), ("/System/Library/Fonts/SFNS.ttf", 0)):
    try:
        font = ImageFont.truetype(cand, 150, index=idx)
        break
    except OSError:
        pass
font = font or ImageFont.load_default()


def marker(cx, cy, label, r=118):
    sh = Image.new("RGBA", (ART, ART), (0, 0, 0, 0))
    ImageDraw.Draw(sh).ellipse([cx - r, cy - r + 14, cx + r, cy + r + 14], fill=(0, 30, 80, 120))
    sh = sh.filter(ImageFilter.GaussianBlur(12))
    bg.paste(sh, (0, 0), sh)
    dd = ImageDraw.Draw(bg, "RGBA")
    dd.ellipse([cx - r - 18, cy - r - 18, cx + r + 18, cy + r + 18], fill=(255, 255, 255, 255))
    dd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(0, 122, 255, 255))
    l, t, rr, b = dd.textbbox((0, 0), label, font=font)   # centre by the real ink box
    dd.text((cx - (l + rr) / 2, cy - (t + b) / 2), label, font=font, fill=(255, 255, 255, 255))


marker(*path[0], "A")
marker(*path[-1], "B")

# --- squircle mask, shadow, assemble ----------------------------------------------------
mask = Image.new("L", (ART, ART), 0)
ImageDraw.Draw(mask).rounded_rectangle([0, 0, ART - 1, ART - 1], R, fill=255)
canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
sh = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(sh).rounded_rectangle([M, M + 22, M + ART, M + ART + 22], R, fill=(0, 0, 0, 95))
canvas = Image.alpha_composite(canvas, sh.filter(ImageFilter.GaussianBlur(26)))
art = bg.convert("RGBA")
art.putalpha(mask)
canvas.alpha_composite(art, (M, M))
# thin highlight on the edge
edge = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(edge).rounded_rectangle([M, M, M + ART - 1, M + ART - 1], R, outline=(255, 255, 255, 120), width=4)
canvas.alpha_composite(edge)
icon = canvas.resize((1024, 1024), Image.LANCZOS)

# --- iconset -> icns ---------------------------------------------------------------------
tmp = "AppIcon.iconset"
shutil.rmtree(tmp, ignore_errors=True)
os.makedirs(tmp)
for size in (16, 32, 128, 256, 512):
    icon.resize((size, size), Image.LANCZOS).save(f"{tmp}/icon_{size}x{size}.png")
    icon.resize((size * 2, size * 2), Image.LANCZOS).save(f"{tmp}/icon_{size}x{size}@2x.png")
subprocess.run(["iconutil", "-c", "icns", tmp, "-o", OUT], check=True)
shutil.rmtree(tmp)
icon.save("/tmp/appicon_preview.png")
print("wrote", OUT)
