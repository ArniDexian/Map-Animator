#!/usr/bin/env python3
"""Animate a route track over a map.

Takes a clean map and a map with a blue track + circular stop markers, extracts a
1D trajectory, detects/sorts markers (A, B, C...) and renders an MP4 animation.

Usage:  python animate_map.py --config config.json [--debug]
"""
import argparse
import copy
import json
import logging
import os
import string
import sys
import time

import cv2
import networkx as nx
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from scipy.ndimage import gaussian_filter1d
from scipy.spatial import cKDTree
from skimage.morphology import skeletonize
from tqdm import tqdm

log = logging.getLogger("animate_map")

DEFAULT_CONFIG = {
    "inputs": {"clean_map": "clean_map.jpg", "track_map": "track_map.jpg", "auto_swap": True},
    "output": {"video_path": "output/output_animation.mp4", "fps": 30, "codec": "mp4v",
               "resolution": None, "debug_dir": "debug",
               "aspect": [16, 9]},   # null = keep the map's own proportions
    "mode": "redraw",
    "timing": {
        "first_marker_delay_sec": 1.0,
        "initial_delay_sec": 1.0,
        "track_duration_sec": 4.0,
        "pause_on_waypoints": True,
        "waypoint_pause_duration_sec": 1.2,
        "final_delay_sec": 2.0,
    },
    "circle_detection": {"min_circle_radius_px": 8.5},
    "reveal_settings": {"line_brush_radius": 12, "waypoint_brush_radius": 22},
    "redraw_settings": {
        "line_color_bgr": [255, 122, 0],
        "line_thickness": 10,
        "marker_color_bgr": [255, 122, 0],
        "marker_text_color_bgr": [255, 255, 255],
        "marker_radius": 16,
        "marker_font_scale": 0.55,
        "marker_font_thickness": 2,
        # null = OpenCV's built-in font (marker_font_scale / marker_font_thickness).
        # A font file switches to anti-aliased text with a system font (size in px).
        "marker_font_path": None,
        "marker_font_index": 0,
        "marker_font_size_px": 16,
    },
    "hsv_blue_range": {"lower": [90, 50, 50], "upper": [130, 255, 255]},
    "path_settings": {"spur_length_px": 25, "smooth_sigma": 1.2, "diff_threshold": 15,
                      "max_mismatch_fraction": 0.01},
    "logging": {"file": "animate_map.log", "level": "INFO"},
}


class AnimateError(Exception):
    """User-facing error (bad input, no track found, ...). `code`/`args` let a UI translate it."""

    def __init__(self, message, code=None, **args):
        super().__init__(message)
        self.code, self.args_dict = code, args


# --------------------------------------------------------------------------- config

def deep_merge(base, override):
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path):
    if not os.path.isfile(path):
        raise AnimateError(f"Config file not found: {path}")
    try:
        with open(path, "r", encoding="utf-8") as f:
            user = json.load(f)
    except json.JSONDecodeError as e:
        raise AnimateError(f"Invalid JSON in {path}: {e}")
    cfg = deep_merge(DEFAULT_CONFIG, user)
    if cfg["mode"] not in ("redraw", "reveal"):
        raise AnimateError(f"Unknown mode '{cfg['mode']}' (expected 'redraw' or 'reveal')")
    if cfg["output"]["fps"] <= 0:
        raise AnimateError("output.fps must be > 0")
    res = cfg["output"]["resolution"]
    if res is not None and (len(res) != 2 or min(res) <= 0):
        raise AnimateError("output.resolution must be null or [width, height]")
    return cfg


def setup_logging(cfg, base_dir):
    level = getattr(logging, str(cfg["logging"]["level"]).upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    log.setLevel(level)
    log.handlers.clear()
    # tqdm-friendly console handler
    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(fmt)
    log.addHandler(ch)
    lf = cfg["logging"].get("file")
    if lf:
        fh = logging.FileHandler(resolve(lf, base_dir), encoding="utf-8")
        fh.setFormatter(fmt)
        log.addHandler(fh)


def resolve(path, base_dir):
    return path if os.path.isabs(path) else os.path.join(base_dir, path)


# --------------------------------------------------------------------------- input sanity

def blue_diff_pixels(base, other, cfg):
    """Pixels where `other` differs from `base` AND is blue in `other` (= where a track was drawn)."""
    diff = cv2.cvtColor(cv2.absdiff(base, other), cv2.COLOR_BGR2GRAY) > cfg["path_settings"]["diff_threshold"]
    hsv = cv2.cvtColor(other, cv2.COLOR_BGR2HSV)
    lo = np.array(cfg["hsv_blue_range"]["lower"], np.uint8)
    hi = np.array(cfg["hsv_blue_range"]["upper"], np.uint8)
    return int(((cv2.inRange(hsv, lo, hi) > 0) & diff).sum())


def check_inputs(clean, track, cfg):
    """Make sure the two files are a proper pair.

    Returns (clean, track, swapped, track_pixels). Raises AnimateError when
      - neither map contains a blue track,
      - the maps differ away from the track (different images or a shifted map),
      - the maps are swapped and inputs.auto_swap is false.
    Swapped maps (the track is drawn on the "clean" file) are swapped back automatically.
    """
    as_given = blue_diff_pixels(clean, track, cfg)
    swapped = blue_diff_pixels(track, clean, cfg)
    min_px = max(200, int(1e-4 * clean.shape[0] * clean.shape[1]))
    log.info("Blue track pixels: track map=%d, clean map=%d (minimum %d)", as_given, swapped, min_px)
    if as_given < min_px and swapped < min_px:
        raise AnimateError("No blue track found: the maps are identical or the track is not blue "
                           "(check the files and hsv_blue_range).", "no_track")
    did_swap = swapped > 2 * as_given and swapped >= min_px
    if did_swap:
        if not cfg["inputs"].get("auto_swap", True):
            raise AnimateError("The maps look swapped: the track is drawn on clean_map, not on track_map.", "swapped")
        log.warning("clean_map and track_map look swapped (the track is on clean_map): swapping them")
        clean, track = track, clean
        as_given = swapped

    # Away from the track the two maps must be identical. A shifted/zoomed map or a different
    # picture differs almost everywhere (>25 % of the pixels), a correct pair differs ~0 %.
    ps = cfg["path_settings"]
    diff = cv2.cvtColor(cv2.absdiff(clean, track), cv2.COLOR_BGR2GRAY) > ps["diff_threshold"]
    hsv = cv2.cvtColor(track, cv2.COLOR_BGR2HSV)
    blue = cv2.inRange(hsv, np.array(cfg["hsv_blue_range"]["lower"], np.uint8),
                       np.array(cfg["hsv_blue_range"]["upper"], np.uint8)) > 0
    near_track = cv2.dilate((diff & blue).astype(np.uint8) * 255,
                            cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (41, 41))) > 0
    outside = float((diff & ~near_track).sum()) / diff.size
    log.info("Differences away from the track: %.2f%% of the image", outside * 100)
    if outside > ps.get("max_mismatch_fraction", 0.01):
        raise AnimateError("The two maps do not match: %.0f%% of the image differs away from the track "
                           "(different pictures, or the map was shifted/zoomed)." % (outside * 100),
                           "maps_mismatch", percent=round(outside * 100))
    return clean, track, did_swap, as_given


# --------------------------------------------------------------------------- step 1

def extract_track_mask(clean, track, cfg):
    ps = cfg["path_settings"]
    diff = cv2.absdiff(clean, track)
    diff_gray = cv2.cvtColor(diff, cv2.COLOR_BGR2GRAY)
    diff_mask = (diff_gray > ps["diff_threshold"]).astype(np.uint8) * 255

    hsv = cv2.cvtColor(track, cv2.COLOR_BGR2HSV)
    lo = np.array(cfg["hsv_blue_range"]["lower"], np.uint8)
    hi = np.array(cfg["hsv_blue_range"]["upper"], np.uint8)
    blue = cv2.inRange(hsv, lo, hi)

    mask = cv2.bitwise_and(diff_mask, blue)
    # Close the gaps made by white letters inside the circular markers.
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    # Remove tiny specks.
    n, labels, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
    if n <= 1:
        raise AnimateError("No blue track found: check hsv_blue_range and that the two maps differ.")
    keep = np.zeros_like(closed)
    areas = stats[1:, cv2.CC_STAT_AREA]
    thr = max(30, 0.2 * areas.max())
    for i, a in enumerate(areas, start=1):
        if a >= thr:
            keep[labels == i] = 255
    log.info("Track mask: %d px (diff=%d, blue=%d)", int((keep > 0).sum()),
             int((diff_mask > 0).sum()), int((blue > 0).sum()))
    return keep


# --------------------------------------------------------------------------- step 2

_NB = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]


def skeleton_graph(skel):
    ys, xs = np.nonzero(skel)
    nodes = set(zip(ys.tolist(), xs.tolist()))
    g = nx.Graph()
    g.add_nodes_from(nodes)
    for (y, x) in nodes:
        for dy, dx in _NB:
            q = (y + dy, x + dx)
            if q in nodes and q > (y, x):
                g.add_edge((y, x), q, weight=float(np.hypot(dy, dx)))
    return g


def prune_spurs(g, max_len):
    """Iteratively remove leaf branches shorter than max_len that end in a junction."""
    for _ in range(10):
        removed = False
        for leaf in [n for n in g.nodes if g.degree(n) == 1]:
            if leaf not in g or g.degree(leaf) != 1:
                continue
            branch = [leaf]
            prev, cur = None, leaf
            while True:
                nxt = [n for n in g.neighbors(cur) if n != prev]
                if len(nxt) != 1:
                    break
                prev, cur = cur, nxt[0]
                if g.degree(cur) != 2:
                    break
                branch.append(cur)
            # cur is a junction (deg>=3) if branch ended on one
            if g.degree(cur) >= 3 and len(branch) < max_len:
                g.remove_nodes_from(branch)
                removed = True
        if not removed:
            break
    return g


def snap_ends_to_markers(pts, mask, cfg):
    """The skeleton of a round marker stops short of its centre; extend path ends to it."""
    min_r = cfg["circle_detection"]["min_circle_radius_px"]
    dt = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    for end in (0, -1):
        x, y = np.round(pts[end]).astype(int)
        r = int(max(dt[y, x], min_r) * 2 + 4)
        y0, x0 = max(y - r, 0), max(x - r, 0)
        win = dt[y0:y + r + 1, x0:x + r + 1]
        if win.max() < min_r:
            continue
        top = np.argwhere(win >= win.max() - 0.5)
        cy, cx = top.mean(axis=0) + (y0, x0)
        if np.hypot(cx - pts[end][0], cy - pts[end][1]) > 1.0:
            c = np.array([[cx, cy]])
            pts = np.vstack([pts, c]) if end == -1 else np.vstack([c, pts])
    return pts


def extract_path(mask, cfg):
    ps = cfg["path_settings"]
    skel = skeletonize(mask > 0)
    g = skeleton_graph(skel)
    log.info("Skeleton: %d nodes, %d edges", g.number_of_nodes(), g.number_of_edges())
    if g.number_of_nodes() < 2:
        raise AnimateError("Skeleton is empty; cannot build a trajectory.")

    prune_spurs(g, ps["spur_length_px"])
    comp = max(nx.connected_components(g), key=len)
    if len(comp) < g.number_of_nodes():
        log.info("Dropping %d nodes outside the largest component", g.number_of_nodes() - len(comp))
    g = g.subgraph(comp).copy()

    leaves = [n for n in g.nodes if g.degree(n) == 1]
    log.info("Endpoints (degree 1): %d", len(leaves))
    # Round markers can leave their skeleton end non-degree-1, so take the two extremes
    # of the line as the farthest-apart pair (double sweep), then A = southernmost one.
    seed = leaves[0] if leaves else next(iter(g.nodes))
    d0 = nx.single_source_dijkstra_path_length(g, seed, weight="weight")
    p1 = max(d0, key=d0.get)
    d1 = nx.single_source_dijkstra_path_length(g, p1, weight="weight")
    p2 = max(d1, key=d1.get)
    start, end = sorted([p1, p2], key=lambda n: (-n[0], n[1]))  # larger y (south) first
    path = nx.dijkstra_path(g, start, end, weight="weight")
    log.info("Path: %d px from (y=%d,x=%d) to (y=%d,x=%d)", len(path), *start, *end)

    pts = np.array([(x, y) for (y, x) in path], dtype=np.float64)
    if len(pts) > 5:
        pts[:, 0] = gaussian_filter1d(pts[:, 0], ps["smooth_sigma"], mode="nearest")
        pts[:, 1] = gaussian_filter1d(pts[:, 1], ps["smooth_sigma"], mode="nearest")
    pts = snap_ends_to_markers(pts, mask, cfg)
    seg = np.hypot(*np.diff(pts, axis=0).T)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    log.info("Trajectory length: %.1f px", cum[-1])
    return pts, cum, skel


# --------------------------------------------------------------------------- step 3

def detect_markers(mask, pts, cum, cfg, skel):
    min_r = cfg["circle_detection"]["min_circle_radius_px"]
    dt = cv2.distanceTransform(mask, cv2.DIST_L2, 5)
    # Adapt to image scale: markers must be clearly thicker than the line itself.
    line_r = float(np.median(dt[skel]))
    thr = max(min_r, 1.7 * line_r)
    log.info("Line half-width ~%.1f px; marker threshold %.1f px (config min %.1f)", line_r, thr, min_r)
    cfg["_detected"] = {"line_r": line_r}
    thick = (dt >= thr).astype(np.uint8)
    n, labels, _, _ = cv2.connectedComponentsWithStats(thick, connectivity=8)
    tree = cKDTree(pts)
    markers = []
    for i in range(1, n):
        ys, xs = np.nonzero(labels == i)
        w = dt[ys, xs]
        # centre = peak of the distance transform (centroid of the top plateau)
        top = w >= w.max() - 0.5
        c = (xs[top].mean(), ys[top].mean())
        d, idx = tree.query(c)
        markers.append({"center": c, "radius": float(w.max()), "idx": int(idx),
                        "dist": float(cum[idx]), "off": float(d)})
    markers.sort(key=lambda m: m["dist"])
    for i, m in enumerate(markers):
        m["label"] = label_for(i)
        log.info("Marker %s: center=(%.0f,%.0f) r=%.1f at %.0f px along path (off-path %.1f)",
                 m["label"], *m["center"], m["radius"], m["dist"], m["off"])
    if len(markers) < 2:
        log.warning("Found %d markers (<2); try lowering circle_detection.min_circle_radius_px", len(markers))
    return markers


def trim_tails(pts, cum, markers):
    """Make the path start exactly at the first marker's centre and end exactly at the last one's.

    Skeleton stubs sticking out past the end discs are cut off, and the end points are
    replaced by the marker centres.
    """
    if len(markers) < 2:
        return pts, cum
    first, last = markers[0], markers[-1]
    lo, hi = first["idx"], last["idx"]
    if hi - lo < 2:
        return pts, cum
    new = pts[lo:hi + 1].copy()
    new[0] = first["center"]
    new[-1] = last["center"]
    seg = np.hypot(*np.diff(new, axis=0).T)
    new_cum = np.concatenate([[0.0], np.cumsum(seg)])
    # re-project the intermediate markers onto the new path
    tree = cKDTree(new)
    for m in markers:
        _, i = tree.query(m["center"])
        m["idx"], m["dist"] = int(i), float(new_cum[i])
    first["idx"], first["dist"] = 0, 0.0
    last["idx"], last["dist"] = len(new) - 1, float(new_cum[-1])
    log.info("Path now runs from the centre of %s to the centre of %s (cut %d px at start, %d px at end)",
             first["label"], last["label"], lo, len(pts) - 1 - hi)
    return new, new_cum


def autoscale_drawing(cfg, markers):
    if not markers or not cfg["redraw_settings"].get("auto_scale_drawing", True):
        return
    rs, rv = cfg["redraw_settings"], cfg["reveal_settings"]
    f = float(np.median([m["radius"] for m in markers])) / rs["marker_radius"]
    if abs(f - 1.0) < 0.15:
        return
    log.info("Auto-scaling drawing sizes x%.2f to match detected marker size", f)
    for d, k in ((rs, "line_thickness"), (rs, "marker_radius"), (rs, "marker_font_thickness"),
                 (rv, "line_brush_radius"), (rv, "waypoint_brush_radius")):
        if d.get(k):  # null = automatic, nothing to scale
            d[k] = max(1, int(round(d[k] * f)))
    rs["marker_font_scale"] = rs["marker_font_scale"] * f
    rs["marker_font_size_px"] = rs["marker_font_size_px"] * f


def label_for(i):
    letters = string.ascii_uppercase
    s = ""
    i += 1
    while i > 0:
        i, r = divmod(i - 1, 26)
        s = letters[r] + s
    return s


# --------------------------------------------------------------------------- step 4

def build_schedule(markers, total_len, cum, cfg):
    """Return list of (path_dist, n_markers_visible) per frame."""
    fps = cfg["output"]["fps"]
    t = cfg["timing"]
    pre_f = int(round(t["first_marker_delay_sec"] * fps))
    init_f = int(round(t["initial_delay_sec"] * fps))
    move_f = max(1, int(round(t["track_duration_sec"] * fps)))
    pause_f = int(round(t["waypoint_pause_duration_sec"] * fps)) if t["pause_on_waypoints"] else 0
    final_f = int(round(t["final_delay_sec"] * fps))
    mid = markers[1:-1] if len(markers) > 2 else []
    if not t["pause_on_waypoints"]:
        mid = []

    def visible(d):
        """Markers shown at path distance d. A marker appears as soon as the line reaches its
        edge (first one is always shown once it is allowed to appear)."""
        n = 0
        for k, m in enumerate(markers):
            if k == 0 or m["dist"] - m["radius"] <= d + 1e-6:
                n += 1
            else:
                break
        return n

    frames = [(0.0, 0)] * pre_f + [(0.0, visible(0.0))] * init_f
    mid_d = [m["dist"] for m in mid]
    paused = set()
    for k in range(1, move_f + 1):
        d = total_len * k / move_f
        # hold at every waypoint crossed since the previous frame
        for j, md in enumerate(mid_d):
            if j not in paused and md <= d:
                paused.add(j)
                frames.extend([(md, visible(md))] * pause_f)
        frames.append((d, visible(d)))
    frames.extend([(total_len, len(markers))] * final_f)

    expected = round((t["first_marker_delay_sec"] + t["initial_delay_sec"] + t["track_duration_sec"]
                      + len(mid) * t["waypoint_pause_duration_sec"] + t["final_delay_sec"]) * fps)
    log.info("Frames: %d (expected ~%d), duration %.2fs, waypoints with pause: %d",
             len(frames), expected, len(frames) / fps, len(mid))
    return frames


# --------------------------------------------------------------------------- step 5

_TEXT_OFFSETS = {}


def text_origin(label, center, fs, ft):
    """Baseline origin that puts the *visible ink* of the label at the circle centre.

    getTextSize only gives font metrics, which leaves capital letters visibly low, so the
    glyphs are rendered once and their real bounding box is measured.
    """
    key = (label, fs, ft)
    if key not in _TEXT_OFFSETS:
        (tw, th), base = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, fs, ft)
        pad = 10
        canvas = np.zeros((th + base + 2 * pad, tw + 2 * pad), np.uint8)
        cv2.putText(canvas, label, (pad, pad + th), cv2.FONT_HERSHEY_SIMPLEX, fs, 255, ft, cv2.LINE_AA)
        ys, xs = np.nonzero(canvas > 40)
        ink_cx, ink_cy = (xs.min() + xs.max() + 1) / 2.0, (ys.min() + ys.max() + 1) / 2.0
        # offset from the text origin (pad, pad+th) to the ink centre
        _TEXT_OFFSETS[key] = (ink_cx - pad, ink_cy - (pad + th))
    dx, dy = _TEXT_OFFSETS[key]
    return int(round(center[0] - dx)), int(round(center[1] - dy))


_FONT_CACHE = {}
_LABEL_CACHE = {}


def label_mask_ttf(label, path, index, size):
    """Anti-aliased mask of `label` in a system font, cropped to its real ink box."""
    key = (label, path, index, size)
    if key not in _LABEL_CACHE:
        fkey = (path, index, size)
        if fkey not in _FONT_CACHE:
            _FONT_CACHE[fkey] = ImageFont.truetype(path, size, index=index)
        font = _FONT_CACHE[fkey]
        l, t, r, b = font.getbbox(label, anchor="ls")
        w, h = int(np.ceil(r - l)) + 6, int(np.ceil(b - t)) + 6
        canvas = Image.new("L", (w, h), 0)
        ImageDraw.Draw(canvas).text((3 - l, 3 - t), label, font=font, fill=255, anchor="ls")
        _LABEL_CACHE[key] = np.asarray(canvas, dtype=np.float32) / 255.0
    return _LABEL_CACHE[key]


def draw_label_ttf(img, c, label, rs):
    try:
        m = label_mask_ttf(label, rs["marker_font_path"], int(rs.get("marker_font_index", 0)),
                           max(1, int(round(rs["marker_font_size_px"]))))
    except (OSError, ValueError) as e:
        if not _FONT_CACHE.get("warned"):
            log.warning("Cannot use font %s (%s): falling back to the built-in font", rs["marker_font_path"], e)
            _FONT_CACHE["warned"] = True
        return False
    h, w = m.shape
    x0, y0 = c[0] - w // 2, c[1] - h // 2          # ink box centred on the circle centre
    x1, y1 = x0 + w, y0 + h
    cx0, cy0, cx1, cy1 = max(x0, 0), max(y0, 0), min(x1, img.shape[1]), min(y1, img.shape[0])
    if cx1 <= cx0 or cy1 <= cy0:
        return True
    a = m[cy0 - y0:cy1 - y0, cx0 - x0:cx1 - x0, None]
    roi = img[cy0:cy1, cx0:cx1].astype(np.float32)
    col = np.array(rs["marker_text_color_bgr"], np.float32)
    img[cy0:cy1, cx0:cx1] = (roi * (1 - a) + col * a).astype(np.uint8)
    return True


def draw_marker(img, center, label, rs):
    c = (int(round(center[0])), int(round(center[1])))
    r = rs["marker_radius"]
    cv2.circle(img, c, r + 2, (255, 255, 255), -1, cv2.LINE_AA)
    cv2.circle(img, c, r, tuple(rs["marker_color_bgr"]), -1, cv2.LINE_AA)
    if rs.get("marker_font_path") and draw_label_ttf(img, c, label, rs):
        return
    fs, ft = rs["marker_font_scale"], rs["marker_font_thickness"]
    org = text_origin(label, center, fs, ft)
    cv2.putText(img, label, org, cv2.FONT_HERSHEY_SIMPLEX, fs, tuple(rs["marker_text_color_bgr"]),
                ft, cv2.LINE_AA)


def head_index(cum, d):
    return int(np.searchsorted(cum, d, side="right")) - 1


def head_point(pts, cum, d):
    i = min(max(head_index(cum, d), 0), len(pts) - 1)
    if i >= len(pts) - 1:
        return pts[-1]
    f = (d - cum[i]) / max(cum[i + 1] - cum[i], 1e-9)
    return pts[i] + f * (pts[i + 1] - pts[i])


class RedrawRenderer:
    def __init__(self, clean, pts, cum, markers, cfg):
        self.clean, self.pts, self.cum, self.markers = clean, pts, cum, markers
        self.rs = cfg["redraw_settings"]

    def frame(self, d, n_vis):
        img = self.clean.copy()
        i = head_index(self.cum, d)
        if i >= 1 or d > 0:
            line = np.vstack([self.pts[:i + 1], head_point(self.pts, self.cum, d)[None]])
            poly = np.round(line).astype(np.int32).reshape(-1, 1, 2)
            col, th = tuple(self.rs["line_color_bgr"]), self.rs["line_thickness"]
            cv2.polylines(img, [poly], False, col, th, cv2.LINE_AA)
            cv2.circle(img, tuple(poly[0, 0]), th // 2, col, -1, cv2.LINE_AA)  # round caps
            cv2.circle(img, tuple(poly[-1, 0]), th // 2, col, -1, cv2.LINE_AA)
        for m in self.markers[:n_vis]:
            draw_marker(img, m["center"], m["label"], self.rs)
        return img


def marker_outer_radius(clean, track, center, r_blue, thr=15):
    """Radius of the whole drawn marker (blue disc + white ring), measured from the image diff."""
    gc = cv2.cvtColor(clean, cv2.COLOR_BGR2GRAY).astype(np.int16)
    gt = cv2.cvtColor(track, cv2.COLOR_BGR2GRAY).astype(np.int16)
    h, w = gc.shape
    extents = []
    for ang in np.linspace(0, 2 * np.pi, 72, endpoint=False):
        ext = r_blue
        for rr in np.arange(r_blue - 2, r_blue * 1.6, 0.5):
            x = int(round(center[0] + rr * np.cos(ang)))
            y = int(round(center[1] + rr * np.sin(ang)))
            if not (0 <= x < w and 0 <= y < h) or abs(gt[y, x] - gc[y, x]) <= thr:
                break
            ext = rr
        extents.append(ext)
    # the median ignores the few directions where the track line leaves the disc
    return max(float(np.median(extents)), r_blue)


class RevealRenderer:
    """Reveal the original track image with a brush that follows the path.

    - the path is revealed with a brush only ~2 px wider than the line itself;
    - markers are revealed as exactly their own disc (no stub of line sticking out);
    - a marker that has not appeared yet stays hidden, so the line runs right up to its
      edge and the disc then appears on its own.
    """

    def __init__(self, clean, track, pts, cum, markers, cfg):
        self.clean, self.track, self.pts, self.cum, self.markers = clean, track, pts, cum, markers
        rv = cfg["reveal_settings"]
        line_r = cfg.get("_detected", {}).get("line_r", 6.0)
        self.r_line = int(rv["line_brush_radius"]) if rv.get("line_brush_radius") else \
            int(round(line_r + rv.get("line_brush_margin_px", 2)))
        self.mask = np.zeros(clean.shape[:2], np.uint8)
        self.done = -1
        self.centers = [(int(round(m["center"][0])), int(round(m["center"][1]))) for m in markers]
        if rv.get("waypoint_brush_radius"):
            self.disc_r = [int(rv["waypoint_brush_radius"])] * len(markers)
        else:
            self.disc_r = [int(np.ceil(marker_outer_radius(clean, track, m["center"], m["radius"]) + 1))
                           for m in markers]
        log.info("Reveal brushes: line r=%d px, marker discs r=%s px", self.r_line, self.disc_r)
        self.revealed = 0

    def frame(self, d, n_vis):
        if n_vis == 0:  # marker A not shown yet: plain clean map
            return self.clean.copy()
        while self.revealed < n_vis:
            cv2.circle(self.mask, self.centers[self.revealed], self.disc_r[self.revealed], 255, -1)
            self.revealed += 1
        i = head_index(self.cum, d)
        for k in range(self.done + 1, i + 1):
            cv2.circle(self.mask, (int(round(self.pts[k, 0])), int(round(self.pts[k, 1]))),
                       self.r_line, 255, -1)
        self.done = max(self.done, i)
        hp = head_point(self.pts, self.cum, d)
        cv2.circle(self.mask, (int(round(hp[0])), int(round(hp[1]))), self.r_line, 255, -1)
        shown = self.mask.copy()
        for j in range(self.revealed, len(self.centers)):  # hide markers that have not appeared
            cv2.circle(shown, self.centers[j], self.disc_r[j], 0, -1)
        a = cv2.GaussianBlur(shown, (5, 5), 0).astype(np.float32)[..., None] / 255.0
        out = self.clean.astype(np.float32) * (1 - a) + self.track.astype(np.float32) * a
        return out.astype(np.uint8)


# --------------------------------------------------------------------------- framing

def frame_crop(w, h, pts, markers, aspect):
    """Largest region of the map with the given aspect ratio (e.g. 16:9), centred on the track.

    Returns (x0, y0, cw, ch); cw/ch are even (video encoders need it).
    """
    if not aspect:
        return 0, 0, w - w % 2, h - h % 2
    ar = aspect[0] / aspect[1]
    cw, ch = (int(round(h * ar)), h) if w / h > ar else (w, int(round(w / ar)))
    cw, ch = cw - cw % 2, ch - ch % 2
    pad = max([m["radius"] for m in markers] or [0]) + 4
    bx0, bx1 = pts[:, 0].min() - pad, pts[:, 0].max() + pad
    by0, by1 = pts[:, 1].min() - pad, pts[:, 1].max() + pad
    x0 = int(min(max(round((bx0 + bx1) / 2 - cw / 2), 0), w - cw))
    y0 = int(min(max(round((by0 + by1) / 2 - ch / 2), 0), h - ch))
    if bx0 < x0 or bx1 > x0 + cw or by0 < y0 or by1 > y0 + ch:
        log.warning("The track does not fit into a %d:%d frame of this map; its edges will be cut off",
                    aspect[0], aspect[1])
    return x0, y0, cw, ch


# --------------------------------------------------------------------------- main

def progress(items, desc):
    """Progress bar with percentage, elapsed time, ETA and speed.

    On a real terminal it is a live tqdm bar; when output is redirected (no TTY) it falls
    back to a plain log line every 5% so progress is still visible.
    """
    total = len(items)
    if sys.stderr.isatty():
        yield from tqdm(items, desc=desc, unit="frame", total=total, dynamic_ncols=True,
                        mininterval=0.2, smoothing=0.1,
                        bar_format="{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} "
                                   "[elapsed {elapsed} | ETA {remaining} | {rate_fmt}]")
        return
    start, step, next_pct = time.time(), max(1, total // 20), 0
    for i, it in enumerate(items, 1):
        yield it
        if i % step == 0 or i == total:
            el = time.time() - start
            eta = el / i * (total - i)
            log.info("%s: %3d%% (%d/%d) elapsed %.0fs, ETA %.0fs", desc, 100 * i // total, i, total, el, eta)


def read_image(path, what):
    if not os.path.isfile(path):
        raise AnimateError(f"{what} not found: {path}")
    img = cv2.imread(path, cv2.IMREAD_COLOR)
    if img is None:
        raise AnimateError(f"{what} could not be decoded as an image: {path}")
    return img


def save_debug(base, mask, skel, pts, markers, track):
    dbg = track.copy()
    dbg[skel] = (0, 0, 255)
    cv2.polylines(dbg, [np.round(pts).astype(np.int32).reshape(-1, 1, 2)], False, (0, 200, 0), 2)
    for m in markers:
        c = (int(m["center"][0]), int(m["center"][1]))
        cv2.circle(dbg, c, int(m["radius"]), (0, 0, 255), 2)
        cv2.circle(dbg, c, 4, (0, 0, 255), -1)
        cv2.putText(dbg, m["label"], (c[0] + 18, c[1] - 18), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 0, 255), 2)
    cv2.imwrite(base + "_debug_mask.png", mask)
    cv2.imwrite(base + "_debug_path.png", dbg)
    log.info("Debug images written: %s_debug_*.png", base)


def run(cfg, base_dir, debug=False):
    clean = read_image(resolve(cfg["inputs"]["clean_map"], base_dir), "clean_map")
    track = read_image(resolve(cfg["inputs"]["track_map"], base_dir), "track_map")
    if clean.shape != track.shape:
        raise AnimateError(f"Map sizes differ: clean {clean.shape[1]}x{clean.shape[0]} "
                           f"vs track {track.shape[1]}x{track.shape[0]}")
    clean, track, _, _ = check_inputs(clean, track, cfg)
    h, w = clean.shape[:2]
    log.info("Maps loaded: %dx%d", w, h)

    log.info("[1/5] extracting track mask")
    mask = extract_track_mask(clean, track, cfg)
    log.info("[2/5] extracting 1D trajectory")
    pts, cum, skel = extract_path(mask, cfg)
    log.info("[3/5] detecting markers")
    markers = detect_markers(mask, pts, cum, cfg, skel)
    pts, cum = trim_tails(pts, cum, markers)
    autoscale_drawing(cfg, markers)
    out_path = resolve(cfg["output"]["video_path"], base_dir)
    if debug:
        dbg_dir = resolve(cfg["output"].get("debug_dir") or os.path.dirname(out_path), base_dir)
        os.makedirs(dbg_dir, exist_ok=True)
        save_debug(os.path.join(dbg_dir, os.path.splitext(os.path.basename(out_path))[0]),
                   mask, skel, pts, markers, track)
    log.info("[4/5] building timeline")
    schedule = build_schedule(markers, float(cum[-1]), cum, cfg)

    log.info("[5/5] rendering (%s mode)", cfg["mode"])
    renderer = (RedrawRenderer(clean, pts, cum, markers, cfg) if cfg["mode"] == "redraw"
                else RevealRenderer(clean, track, pts, cum, markers, cfg))
    x0, y0, cw, ch = frame_crop(w, h, pts, markers, cfg["output"].get("aspect"))
    log.info("Frame: %dx%d region of the map at (%d,%d)", cw, ch, x0, y0)
    size = tuple(cfg["output"]["resolution"]) if cfg["output"]["resolution"] else (cw, ch)
    codec = cfg["output"]["codec"]
    if len(codec) != 4:
        raise AnimateError(f"Codec must be a 4-char fourcc, got '{codec}'")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    vw = None
    for cand in [codec] + [c for c in ("hvc1", "avc1", "mp4v") if c != codec]:   # fall back if unsupported
        sz = size
        if cand.lower() in ("avc1", "h264", "hvc1", "hev1"):                    # these need even dimensions
            sz = (size[0] - size[0] % 2, size[1] - size[1] % 2)
        vw = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*cand), cfg["output"]["fps"], sz)
        if vw.isOpened():
            if cand != codec:
                log.warning("Codec %s is not available here, using %s instead", codec, cand)
            size, codec = sz, cand
            break
        vw.release()
    else:
        raise AnimateError(f"Cannot open video writer for {out_path}")
    try:
        cache_key, cache_img = None, None
        for d, n_vis in progress(schedule, "Rendering + encoding"):
            key = (round(d, 3), n_vis)
            if key != cache_key:  # holds/pauses reuse the previous frame
                img = renderer.frame(d, n_vis)[y0:y0 + ch, x0:x0 + cw]
                if size != (cw, ch):
                    img = cv2.resize(img, size, interpolation=cv2.INTER_AREA if size[0] < cw else cv2.INTER_CUBIC)
                cache_key, cache_img = key, img
            vw.write(cache_img)
    finally:
        vw.release()
    log.info("Done: %s (%d frames, %dx%d @ %s fps)", out_path, len(schedule), size[0], size[1],
             cfg["output"]["fps"])


def main(argv=None):
    ap = argparse.ArgumentParser(description="Animate a route track over a map.")
    ap.add_argument("--config", default="config.json", help="path to JSON config (default: config.json)")
    ap.add_argument("--debug", action="store_true", help="save mask / path debug images next to the video")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
        base_dir = os.path.dirname(os.path.abspath(args.config))
        setup_logging(cfg, base_dir)
        run(cfg, base_dir, args.debug)
    except AnimateError as e:
        if log.handlers:
            log.error("%s", e)
        else:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except Exception:
        log.exception("Unexpected failure")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
