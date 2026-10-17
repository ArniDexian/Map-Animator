#!/usr/bin/env python3
"""Generate the demo material for the README.

* examples/demo_clean_map.jpg  – an original synthetic "map" (no third-party map data, so it can be published)
* examples/demo_track_map.jpg  – the same map with a blue track and stop markers A..E (the tool's input format)
* docs/assets/hero.gif         – clean map + track map  ->  animation (what the README shows at the very top)

Run from the project folder:   .venv/bin/python tools/make_demo.py
"""
import copy
import json
import logging
import os
import subprocess
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.interpolate import splev, splprep

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import animate_map as am  # noqa: E402

W, H = 1600, 900
WATER_LEVEL = 0.34
logging.disable(logging.CRITICAL)


# ------------------------------------------------------------------ synthetic map
def noise(rng, w, h, octaves=6):
    out = np.zeros((h, w), np.float32)
    amp, total = 1.0, 0.0
    for o in range(octaves):
        cells = 3 * 2 ** o
        grid = rng.random((max(2, int(cells * h / w)) + 1, cells + 1)).astype(np.float32)
        out += amp * cv2.resize(grid, (w, h), interpolation=cv2.INTER_CUBIC)
        total += amp
        amp *= 0.5
    out /= total
    return (out - out.min()) / (out.max() - out.min())


def make_map(rng):
    e = noise(rng, W, H)
    e = cv2.GaussianBlur(e, (0, 0), 2)
    water = e < WATER_LEVEL
    # colour ramp over elevation (all colours are deliberately NOT saturated blue except water)
    stops = [(WATER_LEVEL, (196, 226, 178)), (0.52, (170, 214, 150)), (0.64, (222, 218, 176)), (0.78, (205, 190, 165)), (1.0, (238, 236, 232))]
    img = np.zeros((H, W, 3), np.float32)
    xs = np.array([s[0] for s in stops])
    for ch in range(3):
        img[..., ch] = np.interp(e, xs, [s[1][ch] for s in stops])
    # hill shading
    gy, gx = np.gradient(cv2.GaussianBlur(e, (0, 0), 1.2))
    shade = np.clip(1.0 + 38 * (-gx * 0.7 - gy * 0.7), 0.72, 1.28)
    img *= shade[..., None]
    wcol = np.array((170, 211, 223), np.float32)               # water (RGB)
    soft = cv2.GaussianBlur(water.astype(np.float32), (0, 0), 1.4)[..., None]
    img = img * (1 - soft) + wcol * soft
    img = np.clip(img, 0, 255).astype(np.uint8)
    return cv2.cvtColor(img, cv2.COLOR_RGB2BGR), e


TOWNS = [(130, 150), (420, 80), (760, 150), (1180, 90), (1450, 210), (210, 430), (560, 380), (930, 330),
         (1330, 470), (1500, 640), (170, 700), (520, 780), (880, 760), (1230, 810), (1030, 560)]
NAMES = ["Alderton", "Brookmere", "Cinderfell", "Dunmoor", "Eastwick", "Fairhaven", "Greywater", "Highcliff",
         "Ironbridge", "Kestrel Bay", "Larkspur", "Millhaven", "Northgate", "Oakridge", "Pinecrest"]


def draw_roads_and_labels(bgr, water_mask):
    """Roads join neighbouring towns (kept on land); towns get labels with a white halo."""
    land = bgr.copy()
    pil = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(pil)
    pts = np.array(TOWNS, float)
    for i, a in enumerate(pts):
        dist = np.hypot(*(pts - a).T)
        for j in np.argsort(dist)[1:3]:
            if j < i:
                continue
            b = pts[j]
            n = np.array([-(b[1] - a[1]), b[0] - a[0]]) * 0.08
            t = np.linspace(0, 1, 60)[:, None]
            line = (1 - t) ** 2 * a + 2 * (1 - t) * t * ((a + b) / 2 + n) + t ** 2 * b
            xy = [tuple(p) for p in line.tolist()]
            d.line(xy, fill=(150, 150, 150), width=4, joint="curve")
            d.line(xy, fill=(255, 252, 235), width=2, joint="curve")
    out = cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)
    out[water_mask] = land[water_mask]                      # no roads over the sea
    pil = Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB))
    d = ImageDraw.Draw(pil)
    try:
        big = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 17); big.set_variation_by_name("Medium")
        small = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 13); small.set_variation_by_name("Medium")
    except Exception:  # noqa: BLE001
        big = small = ImageFont.load_default()
    for k, ((x, y), name) in enumerate(zip(TOWNS, NAMES)):
        if water_mask[y, x]:
            continue
        d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=(255, 255, 255), outline=(90, 90, 90), width=2)
        d.text((x + 9, y - 10), name, font=big if k < 8 else small, fill=(55, 60, 66), stroke_width=3, stroke_fill=(255, 255, 255))
    return cv2.cvtColor(np.asarray(pil), cv2.COLOR_RGB2BGR)


def route_points(e, jitter):
    """Waypoints A..E, snapped onto land."""
    wp = np.array([[200, 760], [480, 600], [800, 660], [1120, 420], [1400, 200]], float) + jitter
    ys, xs = np.nonzero(e > WATER_LEVEL + 0.06)
    out = []
    for x, y in wp:
        x, y = float(np.clip(x, 20, W - 20)), float(np.clip(y, 20, H - 20))
        if e[int(y), int(x)] < WATER_LEVEL + 0.06:
            i = np.argmin((xs - x) ** 2 + (ys - y) ** 2)
            x, y = float(xs[i]), float(ys[i])
        out.append((x, y))
    return np.array(out)


def draw_track(clean, wp, cfg):
    img = clean.copy()
    # route: smooth curve through the waypoints with a gentle wiggle between them
    ctrl = [wp[0]]
    for a, b in zip(wp[:-1], wp[1:]):
        mid = (a + b) / 2 + np.array([-(b[1] - a[1]), b[0] - a[0]]) * 0.12
        ctrl += [mid, b]
    ctrl = np.array(ctrl)
    tck, _ = splprep(ctrl.T, s=0, k=3)
    x, y = splev(np.linspace(0, 1, 800), tck)
    poly = np.round(np.stack([x, y], 1)).astype(np.int32).reshape(-1, 1, 2)
    colour = (255, 122, 0)
    cv2.polylines(img, [poly], False, colour, 9, cv2.LINE_AA)
    cv2.circle(img, tuple(poly[0, 0]), 4, colour, -1, cv2.LINE_AA)
    cv2.circle(img, tuple(poly[-1, 0]), 4, colour, -1, cv2.LINE_AA)
    rs = copy.deepcopy(cfg["redraw_settings"])
    rs.update(marker_radius=15, marker_outline_px=2)
    for label, (px, py) in zip("ABCDE", wp):
        am.draw_marker(img, (px, py), label, rs)
    return img, np.stack([x, y], 1)


# ------------------------------------------------------------------ hero animation
def font(size, weight="Semibold"):
    try:
        f = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", size)
        f.set_variation_by_name(weight)
        return f
    except Exception:  # noqa: BLE001
        return ImageFont.load_default()


def rounded(img, radius):
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, img.size[0] - 1, img.size[1] - 1], radius, fill=255)
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def build_hero(clean, track, frames, out_path, fps):
    cw, ch = 1120, 452
    bg = (246, 248, 250)
    base = Image.new("RGB", (cw, ch), bg)
    d = ImageDraw.Draw(base)
    t_small = (290, 163)
    thumbs = [Image.fromarray(cv2.cvtColor(cv2.resize(im, t_small, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB)) for im in (clean, track)]
    cap = font(15)
    for i, (th, label) in enumerate(zip(thumbs, ("Clean map", "Same map with the track drawn"))):
        y = 44 + i * 218
        d.text((26, y - 24), label, font=cap, fill=(60, 68, 77))
        card = rounded(th, 12)
        base.paste(card, (26, y), card)
        d.rounded_rectangle([26, y, 26 + t_small[0], y + t_small[1]], 12, outline=(208, 215, 222), width=2)
    d.text((159, 207), "+", font=font(28, "Bold"), fill=(120, 130, 140))
    # arrow
    ax, ay = 338, 226
    d.line([(ax, ay), (ax + 62, ay)], fill=(0, 122, 255), width=6)
    d.polygon([(ax + 56, ay - 16), (ax + 84, ay), (ax + 56, ay + 16)], fill=(0, 122, 255))
    d.text((ax - 6, ay + 24), "Map Animator", font=font(15), fill=(0, 100, 220))
    # animation panel
    pw, ph = 640, 360
    px, py = 456, 46
    d.rounded_rectangle([px - 3, py - 3, px + pw + 3, py + ph + 3], 14, fill=(255, 255, 255), outline=(208, 215, 222), width=2)
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [ff, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{cw}x{ch}", "-r", str(fps), "-i", "-",
           "-vf", "split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle",
           "-loop", "0", out_path]
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for fr in frames:
        pic = Image.fromarray(cv2.cvtColor(cv2.resize(fr, (pw, ph), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2RGB))
        card = rounded(pic, 11)
        canvas = base.copy()
        canvas.paste(card, (px, py), card)
        p.stdin.write(np.asarray(canvas).tobytes())
    p.stdin.close()
    p.wait()


def main():
    os.makedirs(os.path.join(ROOT, "examples"), exist_ok=True)
    os.makedirs(os.path.join(ROOT, "docs", "assets"), exist_ok=True)
    cfg = am.deep_merge(am.DEFAULT_CONFIG, json.load(open(os.path.join(ROOT, "config.json"))))
    best = None
    for seed in range(1, 80):                              # first terrain whose route stays (almost) on land
        rng = np.random.default_rng(seed)
        base_map, elev = make_map(rng)
        wp = route_points(elev, rng.normal(0, 25, (5, 2)))
        track_try, line = draw_track(base_map, wp, cfg)
        wet = float(np.mean(elev[np.clip(line[:, 1].astype(int), 0, H - 1), np.clip(line[:, 0].astype(int), 0, W - 1)] < WATER_LEVEL))
        if best is None or wet < best[0]:
            best = (wet, seed)
        if wet < 0.02:
            break
    wet, seed = best
    print(f"terrain seed {seed}: {wet:.1%} of the route over water")
    rng = np.random.default_rng(seed)
    base_map, elev = make_map(rng)
    wp = route_points(elev, rng.normal(0, 25, (5, 2)))
    clean = draw_roads_and_labels(base_map, elev < WATER_LEVEL)
    track, _ = draw_track(clean, wp, cfg)
    p_clean = os.path.join(ROOT, "examples", "demo_clean_map.jpg")
    p_track = os.path.join(ROOT, "examples", "demo_track_map.jpg")
    cv2.imwrite(p_clean, clean, [cv2.IMWRITE_JPEG_QUALITY, 92])
    cv2.imwrite(p_track, track, [cv2.IMWRITE_JPEG_QUALITY, 92])
    print("maps written")

    # run the real pipeline on the demo maps (same code path as the tool)
    fps = 12
    cfg["mode"] = "redraw"
    cfg["output"]["fps"] = fps
    cfg["timing"].update(first_marker_delay_sec=0.6, initial_delay_sec=0.4, track_duration_sec=5.0,
                         waypoint_pause_duration_sec=0.6, final_delay_sec=1.2)
    clean_r, track_r = cv2.imread(p_clean), cv2.imread(p_track)
    clean_r, track_r, _, _ = am.check_inputs(clean_r, track_r, cfg)
    mask = am.extract_track_mask(clean_r, track_r, cfg)
    pts, cum, skel = am.extract_path(mask, cfg)
    markers = am.detect_markers(mask, pts, cum, cfg, skel)
    print("markers found:", [m["label"] for m in markers])
    pts, cum = am.trim_tails(pts, cum, markers)
    am.autoscale_drawing(cfg, markers)
    sched = am.build_schedule(markers, float(cum[-1]), cum, cfg)
    r = am.RedrawRenderer(clean_r, pts, cum, markers, cfg)
    frames = [r.frame(d, n) for d, n in sched]
    out = os.path.join(ROOT, "docs", "assets", "hero.gif")
    build_hero(clean_r, track_r, frames, out, fps)
    print(f"hero.gif: {len(frames)} frames, {os.path.getsize(out) / 1e6:.1f} MB")
    cv2.imwrite("/tmp/demo_preview_track.png", track)


if __name__ == "__main__":
    main()
