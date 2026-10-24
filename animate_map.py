#!/usr/bin/env python3
"""Animate a route track over a map.

Takes a clean map and a map with a blue track + circular stop markers, extracts a
1D trajectory, detects/sorts markers (A, B, C...) and renders an MP4 animation.

Usage:  python animate_map.py --config config.json [--debug]
"""
import argparse
import base64
import binascii
import io
from functools import lru_cache
import copy
import json
import logging
import os
import string
import subprocess
import sys
import tempfile
import threading
import time

import cv2
import networkx as nx
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from fontTools.ttLib import TTFont
import uharfbuzz as hb
from scipy.ndimage import gaussian_filter1d
from scipy.spatial import cKDTree
from skimage.morphology import skeletonize
from tqdm import tqdm

log = logging.getLogger("animate_map")

DEFAULT_CONFIG = {
    "inputs": {"clean_map": "clean_map.jpg", "track_map": "track_map.jpg", "auto_swap": True},
    "output": {"video_path": "output/output_animation.mp4", "fps": 30, "codec": "avc1", "quality": "max",
               "resolution": None, "debug_dir": "debug",
               "aspect": [16, 9]},   # null = keep the map's own proportions
    "mode": "redraw",
    "timing": {
        "first_marker_delay_sec": 1.0,
        "initial_delay_sec": 1.0,
        "track_duration_sec": 4.0,
        "pause_on_waypoints": True,
        # Own times per segment (the "Customize" window of the app). When enabled, `legs[i]` is the travel time from marker i
        # to marker i+1 (shorter = faster) and `pauses[i]` the stop at marker i+1; the first-marker, start and final delays
        # above are still used. An entry that is null follows the general value: a leg gets its share of
        # track_duration_sec (by length), a stop gets waypoint_pause_duration_sec. It applies only when the number of
        # entries matches the markers found on the map.
        "custom": {"enabled": False, "legs": [], "pauses": []},
        "waypoint_pause_duration_sec": 1.2,
        "final_delay_sec": 2.0,
    },
    "follow_settings": {"waypoint_color_bgr": [255, 255, 255], "waypoint_outline_color_bgr": [0, 0, 0],
                        "waypoint_diameter_px": 16, "waypoint_outline_px": 2, "hide_waypoints": False,
                        "object_icon": "sprite", "object_size_px": 128, "object_image": None,
                        "object_emoji": "🏍️", "object_rotation_deg": 0,
                        "object_flip_horizontal": False, "object_flip_vertical": False,
                        "object_offset": {"x": 0.0, "y": 0.0},
                        "start_icon": "emoji", "end_icon": "emoji",
                        "start_size_px": 32, "end_size_px": 32, "start_image": None, "end_image": None,
                        "start_emoji": "green_flag", "end_emoji": "🏁",
                        "start_offset_x_px": 0, "start_offset_y_px": 0,
                        "end_offset_x_px": 0, "end_offset_y_px": 0},
    "circle_detection": {"min_circle_radius_px": 8.5},
    "reveal_settings": {"line_brush_radius": 12, "waypoint_brush_radius": 22},
    "redraw_settings": {
        "line_color_bgr": [255, 122, 0],
        "line_thickness": 10,
        "marker_color_bgr": [255, 122, 0],
        "marker_text_color_bgr": [255, 255, 255],
        "marker_radius": 16,
        "marker_outline_px": 2,                       # ring around the disc, 0 = none
        "marker_outline_color_bgr": [255, 255, 255],
        "marker_font_scale": 0.55,
        "marker_font_thickness": 2,
        # null = OpenCV's built-in font (marker_font_scale / marker_font_thickness).
        # A font file switches to anti-aliased text with a system font (size in px).
        "marker_font_path": None,
        "marker_font_index": 0,
        "marker_font_variation": None,                # named instance of a variable font, e.g. "Bold" for SF Pro
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
    if cfg["mode"] == "traveler":
        cfg["mode"] = "follow"  # compatibility with earlier saved configurations
    if cfg["mode"] not in ("redraw", "reveal", "follow"):
        raise AnimateError(f"Unknown mode '{cfg['mode']}' (expected 'redraw', 'reveal' or 'follow')")
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
    for d, k in ((rs, "line_thickness"), (rs, "marker_radius"), (rs, "marker_font_thickness"), (rs, "marker_outline_px"),
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
    custom = t.get("custom") or {}
    if custom.get("enabled"):
        raw_legs, raw_pauses = list(custom.get("legs", [])), list(custom.get("pauses", []))
        n_mid = max(0, len(markers) - 2)
        if len(raw_legs) == len(markers) - 1 and len(raw_pauses) == n_mid:
            lens = [(markers[i + 1]["dist"] if i + 1 < len(markers) - 1 else total_len) - markers[i]["dist"] for i in range(len(markers) - 1)]
            tot = sum(lens) or 1.0
            legs = [t["track_duration_sec"] * lens[i] / tot if x is None else max(0.0, float(x)) for i, x in enumerate(raw_legs)]
            general_pause = t["waypoint_pause_duration_sec"] if t["pause_on_waypoints"] else 0.0
            pauses = [general_pause if x is None else max(0.0, float(x)) for x in raw_pauses]
            return _custom_schedule(markers, total_len, fps, frames, visible, legs, pauses, final_f, t)
        log.warning("Custom timing has %d legs / %d pauses but the map has %d markers: using the general timing",
                    len(raw_legs), len(raw_pauses), len(markers))
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


def _custom_schedule(markers, total_len, fps, frames, visible, legs, pauses, final_f, t):
    """Timeline with an own duration for every leg (marker i -> i+1) and an own stop at every intermediate marker."""
    for i, leg in enumerate(legs):
        d0, d1 = markers[i]["dist"], markers[i + 1]["dist"] if i + 1 < len(markers) - 1 else total_len
        n = max(1, int(round(leg * fps)))
        for k in range(1, n + 1):                      # constant speed inside a leg
            d = d0 + (d1 - d0) * k / n
            frames.append((d, visible(d)))
        if i < len(pauses):                            # stop at the marker we have just reached
            frames.extend([(d1, visible(d1))] * int(round(pauses[i] * fps)))
    frames.extend([(total_len, len(markers))] * final_f)
    total = t["first_marker_delay_sec"] + t["initial_delay_sec"] + sum(legs) + sum(pauses) + t["final_delay_sec"]
    log.info("Frames: %d (custom timing, expected ~%d), duration %.2fs, legs %s s, pauses %s s",
             len(frames), round(total * fps), len(frames) / fps, [round(x, 2) for x in legs], [round(x, 2) for x in pauses])
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


def label_mask_ttf(label, path, index, size, variation=None):
    """Anti-aliased mask of `label` in a system font, cropped to its real ink box."""
    key = (label, path, index, size, variation)
    if key not in _LABEL_CACHE:
        fkey = (path, index, size, variation)
        if fkey not in _FONT_CACHE:
            font = ImageFont.truetype(path, size, index=index)
            if variation:
                font.set_variation_by_name(variation)       # e.g. SF Pro "Bold"
            _FONT_CACHE[fkey] = font
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
                           max(1, int(round(rs["marker_font_size_px"]))), rs.get("marker_font_variation"))
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
    outline = int(rs.get("marker_outline_px", 2))
    if outline > 0:
        cv2.circle(img, c, r + outline, tuple(rs.get("marker_outline_color_bgr", [255, 255, 255])), -1, cv2.LINE_AA)
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


_EMOJI_RENDER_LOCK = threading.RLock()
EMOJI_ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "emoji")


@lru_cache(maxsize=1)
def emoji_catalog():
    with open(os.path.join(EMOJI_ASSET_DIR, "catalog.json"), encoding="utf-8") as source:
        catalog = json.load(source)
    # Unicode has no plain green flag; expose a local recolour among the emoji flags.
    flags = next(group for group in catalog["groups"] if group["name"] == "Flags")
    flags["emojis"].insert(0, {"emoji": "green_flag", "name": "green flag", "origin": "app",
                              "keywords_ru": ["зелёный флажок", "зеленый флаг", "старт"],
                              "keywords_en": ["green flag", "start"]})
    return catalog


@lru_cache(maxsize=1)
def endpoint_emoji_choices():
    return frozenset(item["emoji"] for group in emoji_catalog()["groups"] for item in group["emojis"])


@lru_cache(maxsize=1)
def endpoint_emoji_font():
    path = os.path.join(EMOJI_ASSET_DIR, "NotoColorEmoji.ttf")
    font = hb.Font(hb.Face(hb.Blob.from_file_path(path)))
    table = TTFont(path)
    return font, table.getGlyphOrder(), table["CBDT"].strikeData[0]


@lru_cache(maxsize=1)
def system_emoji_font():
    """Read Apple's existing local font; never copy it into the app bundle."""
    path = "/System/Library/Fonts/Apple Color Emoji.ttc"
    if sys.platform != "darwin" or not os.path.isfile(path):
        return None
    try:
        table = TTFont(path, fontNumber=0)
        strikes = table["sbix"].strikes
        strike = strikes[min(strikes, key=lambda size: abs(size - 160))]
        font = hb.Font(hb.Face(hb.Blob.from_file_path(path)))
        font.scale = (strike.ppem * 64, strike.ppem * 64)
        return font, table.getGlyphOrder(), strike.glyphs
    except (OSError, KeyError):
        return None


def emoji_bitmap(font_info, emoji):
    """Decode shaped emoji bitmaps, including overlapping Apple sbix layers."""
    if font_info is None:
        return None
    font, glyph_order, bitmaps = font_info
    buffer = hb.Buffer()
    # Apple AAT shaping may leave invisible VS16 placeholders beside the glyph.
    buffer.flags = hb.BufferFlags.REMOVE_DEFAULT_IGNORABLES
    buffer.add_str(emoji)
    buffer.guess_segment_properties()
    hb.shape(font, buffer)
    glyphs, positions = buffer.glyph_infos, buffer.glyph_positions
    if not glyphs or any(glyph.codepoint == 0 for glyph in glyphs):
        return None
    # Apple stores some joined emoji as overlapping left/right bitmap layers.
    # Reject ordinary adjacent glyphs, but preserve these intentional zero-advance layers.
    if len(glyphs) > 1 and not (positions[0].x_advance > 0 and
                               all(pos.x_advance == 0 and pos.y_advance == 0 for pos in positions[1:])):
        return None
    try:
        layers = []
        cursor_x = cursor_y = 0
        for info, position in zip(glyphs, positions):
            name = glyph_order[info.codepoint]
            glyph = bitmaps[name]
            origin_x, origin_y = getattr(glyph, "originOffsetX", 0), getattr(glyph, "originOffsetY", 0)
            seen = set()
            while getattr(glyph, "graphicType", None) == "dupe":
                if name in seen:
                    return None
                seen.add(name)
                name = glyph.referenceGlyphName
                glyph = bitmaps[name]
            if not glyph.imageData:
                return None
            with Image.open(io.BytesIO(glyph.imageData)) as source:
                image = source.convert("RGBA")
            if len(glyphs) == 1:
                return image
            x = round((cursor_x + position.x_offset) / 64 + origin_x)
            y = round(-(cursor_y + position.y_offset) / 64 - origin_y - image.height)
            layers.append((image, x, y))
            cursor_x += position.x_advance
            cursor_y += position.y_advance
        left, top = min(x for _, x, _ in layers), min(y for _, _, y in layers)
        right = max(x + image.width for image, x, _ in layers)
        bottom = max(y + image.height for image, _, y in layers)
        image = Image.new("RGBA", (right - left, bottom - top))
        for layer, x, y in layers:
            image.alpha_composite(layer, (x - left, y - top))
        return image
    except (KeyError, OSError, AttributeError):
        return None


@lru_cache(maxsize=128)
def render_endpoint_emoji(emoji):
    if emoji not in endpoint_emoji_choices():
        raise AnimateError("Choose an emoji from the endpoint catalog.", "endpoint_emoji_invalid")
    try:
        source_emoji = "🚩" if emoji == "green_flag" else emoji
        with _EMOJI_RENDER_LOCK:
            image = emoji_bitmap(system_emoji_font(), source_emoji)
            if image is None:
                image = emoji_bitmap(endpoint_emoji_font(), source_emoji)
        if image is None:
            raise OSError("unsupported emoji sequence")
        bounds = image.getbbox()
        if bounds is None:
            raise OSError("empty emoji glyph")
        rgba = np.array(image.crop(bounds))
        if emoji == "green_flag":
            hsv = cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_RGB2HSV)
            red = ((hsv[:, :, 0] < 12) | (hsv[:, :, 0] > 170)) & (hsv[:, :, 1] > 90) & (rgba[:, :, 3] > 0)
            hsv[:, :, 0][red] = 60
            rgb = cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)
            rgba[:, :, :3][red] = rgb[red]
        return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)
    except (OSError, KeyError) as e:
        raise AnimateError("Could not render the colour emoji.", "endpoint_emoji_font") from e


@lru_cache(maxsize=256)
def endpoint_emoji_png(emoji, size=48):
    if size not in (32, 48, 64, 128):
        raise AnimateError("Invalid emoji preview size.", "endpoint_size_invalid")
    image = render_endpoint_emoji(emoji)
    h, w = image.shape[:2]
    image = cv2.resize(image, (max(1, round(w * size / max(h, w))), max(1, round(h * size / max(h, w)))),
                       interpolation=cv2.INTER_AREA)
    ok, data = cv2.imencode(".png", image)
    if not ok:
        raise AnimateError("Could not render emoji.", "endpoint_emoji_invalid")
    return data.tobytes()


def render_endpoint_pin(size):
    """A single smooth map pin, antialiased before compositing onto the map."""
    scale = 4
    w, h = max(6, round(size * 0.78)), int(size)
    canvas = np.zeros((h * scale, w * scale, 4), np.uint8)
    # Same normalized cubic outline as the HTML style sample.
    curves = [((.5, .98), (.38, .84), (.04, .57), (.04, .36)),
              ((.04, .36), (.04, .16), (.24, .02), (.5, .02)),
              ((.5, .02), (.76, .02), (.96, .16), (.96, .36)),
              ((.96, .36), (.96, .57), (.62, .84), (.5, .98))]
    points = []
    for segment in curves:
        p0, p1, p2, p3 = np.array(segment, dtype=float)
        for t in np.linspace(0, 1, 24, endpoint=False):
            u = 1 - t
            points.append((u**3 * p0 + 3*u*u*t*p1 + 3*u*t*t*p2 + t**3*p3) * [w * scale - 1, h * scale - 1])
    polygon = np.round(points).astype(np.int32)
    cv2.fillPoly(canvas, [polygon], (50, 65, 230, 255))
    cv2.polylines(canvas, [polygon], True, (35, 40, 160, 255), max(1, scale // 2))
    cv2.circle(canvas, (round((w * scale - 1) * .5), round((h * scale - 1) * .36)),
               max(2, round(h * scale * .14)), (255, 255, 255, 255), -1)
    return cv2.resize(canvas, (w, h), interpolation=cv2.INTER_AREA)


def load_endpoint_image(data):
    """Decode a bounded, self-contained image shared by settings, styles and CLI."""
    try:
        if not isinstance(data, str) or len(data) > 1500000:
            raise ValueError("missing or oversized image")
        prefix, payload = data.split(",", 1)
        if prefix not in ("data:image/png;base64", "data:image/jpeg;base64", "data:image/webp;base64"):
            raise ValueError("unsupported image format")
        raw = base64.b64decode(payload, validate=True)
        with Image.open(io.BytesIO(raw)) as source:
            if source.format not in ("PNG", "JPEG", "WEBP") or max(source.size) > 512 or min(source.size) < 1:
                raise ValueError("image must be at most 512 x 512 pixels")
            rgba = np.array(source.convert("RGBA"))
        return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA)
    except (ValueError, TypeError, OSError, binascii.Error, Image.DecompressionBombError) as e:
        raise AnimateError("Invalid endpoint image: use a small PNG, JPEG or WebP (up to 512 px).",
                           "endpoint_image_invalid") from e


def load_follow_sprite(data=None):
    """Load a 4×4 directional sprite sheet, using the bundled Tiger Sport sheet by default."""
    try:
        if data is None:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "tiger-sport-16", "sprite_sheet_16x256.png")
            with Image.open(path) as source:
                rgba = np.array(source.convert("RGBA"))
        else:
            if not isinstance(data, str) or len(data) > 6000000:
                raise ValueError("missing or oversized sprite sheet")
            prefix, payload = data.split(",", 1)
            if prefix != "data:image/png;base64":
                raise ValueError("sprite sheet must be PNG")
            raw = base64.b64decode(payload, validate=True)
            with Image.open(io.BytesIO(raw)) as source:
                if source.format != "PNG" or max(source.size) > 4096 or source.width % 4 or source.height % 4:
                    raise ValueError("sprite sheet must be a 4×4 PNG grid")
                rgba = np.array(source.convert("RGBA"))
        h, w = rgba.shape[:2]
        if w % 4 or h % 4 or min(w, h) < 4:
            raise ValueError("sprite sheet must be a 4×4 grid")
        return [cv2.cvtColor(rgba[r*h//4:(r+1)*h//4, c*w//4:(c+1)*w//4], cv2.COLOR_RGBA2BGRA)
                for r in range(4) for c in range(4)]
    except (ValueError, TypeError, OSError, binascii.Error, Image.DecompressionBombError) as e:
        raise AnimateError("Invalid directional sprite sheet: use a PNG divided into a 4×4 grid.",
                           "follow_sprite_invalid") from e


def rotate_follow_image(image, angle):
    """Rotate an image around its center to follow a heading (image y points down)."""
    h, w = image.shape[:2]
    matrix = cv2.getRotationMatrix2D((w / 2, h / 2), -np.degrees(angle), 1.0)
    corners = np.array([[0, 0, 1], [w, 0, 1], [0, h, 1], [w, h, 1]], dtype=np.float64)
    rotated = corners @ matrix.T
    min_x, min_y = np.floor(rotated.min(axis=0)).astype(int)
    max_x, max_y = np.ceil(rotated.max(axis=0)).astype(int)
    out_w, out_h = max(1, max_x - min_x), max(1, max_y - min_y)
    matrix[0, 2] -= min_x
    matrix[1, 2] -= min_y
    return cv2.warpAffine(image, matrix, (out_w, out_h), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0, 0))


class TravelerRenderer(RedrawRenderer):
    """Draw the traveled line, endpoint flags and a direction-aware moving object.

    Intermediate markers appear as unlabelled circles when reached. Direction is sampled over a short distance
    rather than individual skeleton pixels, and depends on distance, not frame history.
    """

    def __init__(self, clean, pts, cum, markers, cfg):
        super().__init__(clean, pts, cum, [], cfg)
        self.waypoints = markers[1:-1]
        self.fs = deep_merge(DEFAULT_CONFIG["follow_settings"], cfg.get("follow_settings", {}))
        object_kind = self.fs["object_icon"]
        if object_kind not in ("sprite", "emoji", "custom"):
            raise AnimateError("Unknown moving object icon: " + str(object_kind), "follow_object_icon_invalid")
        object_size = self.fs["object_size_px"]
        if not isinstance(object_size, (int, float)) or not np.isfinite(object_size) or not 8 <= object_size <= 256:
            raise AnimateError("Moving object size must be between 8 and 256 pixels.", "follow_object_size_invalid")
        self.object_size = int(round(object_size))
        self.object_rotation_deg = self.fs.get("object_rotation_deg", 0)
        if not isinstance(self.object_rotation_deg, (int, float)) or not np.isfinite(self.object_rotation_deg) or not -180 <= self.object_rotation_deg <= 180:
            raise AnimateError("Object rotation must be between -180 and 180 degrees.", "follow_object_rotation_invalid")
        object_offset = self.fs.get("object_offset", {"x": 0.0, "y": 0.0})
        if not isinstance(object_offset, dict):
            raise AnimateError("Object offsets must be between -1 and 1.", "follow_object_offset_invalid")
        self.object_offset = {}
        for axis in ("x", "y"):
            value = object_offset.get(axis, 0.0)
            if not isinstance(value, (int, float)) or not np.isfinite(value) or not -1 <= value <= 1:
                raise AnimateError("Object offsets must be between -1 and 1.", "follow_object_offset_invalid")
            self.object_offset[axis] = float(value)
        self.object_sprites = load_follow_sprite(self.fs.get("object_sprite_image")) if object_kind == "sprite" else None
        if self.object_sprites is not None:
            self.object_sprites = [cv2.resize(frame,
                (max(1, round(frame.shape[1] * self.object_size / max(frame.shape[:2]))),
                 max(1, round(frame.shape[0] * self.object_size / max(frame.shape[:2])))),
                interpolation=cv2.INTER_AREA if self.object_size < max(frame.shape[:2]) else cv2.INTER_CUBIC)
                for frame in self.object_sprites]
        if object_kind == "custom":
            self.object_image = load_endpoint_image(self.fs["object_image"])
        elif object_kind == "emoji":
            self.object_image = render_endpoint_emoji(self.fs["object_emoji"])
        else:
            self.object_image = None
        if self.object_image is not None:
            ih, iw = self.object_image.shape[:2]
            scale = self.object_size / max(ih, iw)
            self.object_image = cv2.resize(self.object_image,
                (max(1, round(iw * scale)), max(1, round(ih * scale))),
                interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
        self.endpoint_images = {}
        for endpoint in ("start", "end"):
            kind = self.fs[endpoint + "_icon"]
            if kind not in ("green_flag", "red_flag", "checkered_flag", "pin", "none", "custom", "emoji"):
                raise AnimateError("Unknown endpoint icon: " + str(kind), "endpoint_icon_invalid")
            size = self.fs[endpoint + "_size_px"]
            if not isinstance(size, (int, float)) or not np.isfinite(size) or not 8 <= size <= 512:
                raise AnimateError("Endpoint icon size must be between 8 and 512 pixels.", "endpoint_size_invalid")
            for axis in ("x", "y"):
                offset = self.fs[endpoint + "_offset_" + axis + "_px"]
                if not isinstance(offset, (int, float)) or not np.isfinite(offset) or abs(offset) > 1024:
                    raise AnimateError("Endpoint offsets must be between -1024 and 1024 pixels.", "endpoint_offset_invalid")
            if kind == "pin":
                self.endpoint_images[endpoint] = render_endpoint_pin(round(size))
            if kind in ("custom", "emoji"):
                source = (load_endpoint_image(self.fs[endpoint + "_image"]) if kind == "custom"
                          else render_endpoint_emoji(self.fs[endpoint + "_emoji"]))
                h, w = source.shape[:2]
                # Emoji fonts expose a fixed-size bitmap. Scale that bitmap to the requested map size,
                # including upscaling beyond the font's native glyph dimensions.
                scale = size / max(h, w)
                self.endpoint_images[endpoint] = cv2.resize(source, (max(1, round(w * scale)), max(1, round(h * scale))),
                                                            interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
        # Independent of hidden circle settings; scale with the map's dimensions.
        self.size = max(12.0, min(clean.shape[:2]) * 0.025)

    @staticmethod
    def _blit_rgba(img, source, center, anchor=(0.5, 0.5)):
        h, w = source.shape[:2]
        x, y = np.round(center).astype(int)
        left, top = x - int(round(w * anchor[0])), y - int(round(h * anchor[1]))
        x0, y0 = max(0, left), max(0, top)
        x1, y1 = min(img.shape[1], left + w), min(img.shape[0], top + h)
        if x1 <= x0 or y1 <= y0:
            return
        source = source[y0-top:y1-top, x0-left:x1-left]
        alpha = source[..., 3:4].astype(np.float32) / 255
        img[y0:y1, x0:x1] = np.round(source[..., :3] * alpha + img[y0:y1, x0:x1] * (1-alpha)).astype(np.uint8)

    def heading(self, d):
        window = self.size * 0.6
        before = head_point(self.pts, self.cum, max(0.0, d - window))
        after = head_point(self.pts, self.cum, min(float(self.cum[-1]), d + window))
        delta = after - before
        if np.linalg.norm(delta) < 1e-6:
            delta = self.pts[-1] - self.pts[0]
        return float(np.arctan2(delta[1], delta[0]))

    def draw_flag(self, img, point, finish=False):
        endpoint = "end" if finish else "start"
        kind = self.fs[endpoint + "_icon"]
        if kind == "none":
            return
        x, y = np.round(np.asarray(point) + [self.fs[endpoint + "_offset_x_px"],
                                            self.fs[endpoint + "_offset_y_px"]]).astype(int)
        h = int(round(self.fs[endpoint + "_size_px"]))
        w = max(6, int(round(h * 0.7)))
        if kind in ("custom", "emoji", "pin"):
            icon = self.endpoint_images[endpoint]
            ih, iw = icon.shape[:2]
            left, top = x - iw // 2, y - ih
            x0, y0 = max(0, left), max(0, top)
            x1, y1 = min(img.shape[1], left + iw), min(img.shape[0], top + ih)
            if x1 > x0 and y1 > y0:
                source = icon[y0 - top:y1 - top, x0 - left:x1 - left]
                alpha = source[..., 3:4].astype(np.float32) / 255
                img[y0:y1, x0:x1] = np.round(source[..., :3] * alpha + img[y0:y1, x0:x1] * (1 - alpha)).astype(np.uint8)
            return
        top = y - h
        cv2.line(img, (x, y), (x, top), (255, 255, 255), 5, cv2.LINE_AA)
        cv2.line(img, (x, y), (x, top), (55, 55, 55), 2, cv2.LINE_AA)
        cv2.rectangle(img, (x + 1, top), (x + w, top + h // 2), (255, 255, 255), -1)
        if kind == "checkered_flag":
            for row in range(2):
                for col in range(4):
                    if (row + col) % 2 == 0:
                        cv2.rectangle(img, (x + 1 + col * w // 4, top + row * h // 4),
                                      (x + (col + 1) * w // 4, top + (row + 1) * h // 4), (45, 45, 45), -1)
        else:
            cv2.rectangle(img, (x + 2, top + 1), (x + w - 1, top + h // 2 - 1), (50, 65, 230) if kind == "red_flag" else (80, 165, 35), -1)
        cv2.rectangle(img, (x + 1, top), (x + w, top + h // 2), (255, 255, 255), 1)

    def frame(self, d, n_vis):
        if not n_vis:
            return self.clean.copy()
        img = super().frame(d, 0)
        radius = max(1, int(round(self.fs["waypoint_diameter_px"] / 2)))
        outline = max(0, int(round(self.fs["waypoint_outline_px"])))
        for marker in ([] if self.fs["hide_waypoints"] else self.waypoints):
            if d + 1e-6 < marker["dist"]:
                continue
            # Use the same smoothed trajectory as the line and moving object.
            center = tuple(np.round(head_point(self.pts, self.cum, marker["dist"])).astype(int))
            if outline:
                cv2.circle(img, center, radius + outline, tuple(self.fs["waypoint_outline_color_bgr"]), -1, cv2.LINE_AA)
            cv2.circle(img, center, radius, tuple(self.fs["waypoint_color_bgr"]), -1, cv2.LINE_AA)
        self.draw_flag(img, self.pts[0])
        if d >= self.cum[-1] - 1e-6:
            self.draw_flag(img, self.pts[-1], finish=True)
        center = head_point(self.pts, self.cum, d)
        angle = self.heading(d)
        if self.fs["object_icon"] == "sprite":
            # Sheet order follows the turn: up (frame 1), right (5), down (9), left (13).
            frame_index = int(round(((angle + np.pi / 2) % (2 * np.pi)) * 16 / (2 * np.pi))) % 16
            self._blit_rgba(img, self.object_sprites[frame_index], center)
        elif self.fs["object_icon"] in ("emoji", "custom"):
            # Uploaded art defaults to facing right. The standard motorcycle emoji faces left,
            # so face right by reflecting horizontally; rotating it 180° turns the wheels up.
            emoji = str(self.fs.get("object_emoji") or "").replace("\ufe0f", "")
            motorcycle = self.fs["object_icon"] == "emoji" and emoji == "🏍"
            direction = angle + np.radians(self.object_rotation_deg)
            auto_flip_horizontal = motorcycle and np.cos(direction) >= 0
            relative_angle = direction - (np.pi if motorcycle and not auto_flip_horizontal else 0.0)
            image = self.object_image
            flip_horizontal = bool(self.fs.get("object_flip_horizontal", False)) ^ auto_flip_horizontal
            flip_vertical = bool(self.fs.get("object_flip_vertical", False))
            if flip_horizontal or flip_vertical:
                flip_code = -1 if flip_horizontal and flip_vertical else (1 if flip_horizontal else 0)
                image = cv2.flip(image, flip_code)
            rotated = rotate_follow_image(image, relative_angle)
            offset = np.array([self.object_offset["x"], self.object_offset["y"]], dtype=np.float64) * self.object_size
            self._blit_rgba(img, rotated, np.asarray(center) + offset)
        else:
            rotation = np.array([[np.cos(angle), -np.sin(angle)],
                                 [np.sin(angle), np.cos(angle)]])
            shape = np.array([[1, 0], [-0.8, -0.65], [-0.4, 0], [-0.8, 0.65]]) * self.size
            poly = np.round(shape @ rotation.T + center).astype(np.int32)
            cv2.fillPoly(img, [poly], (255, 255, 255), cv2.LINE_AA)
            cv2.polylines(img, [poly], True, (255, 255, 255), 5, cv2.LINE_AA)
            cv2.fillPoly(img, [poly], (50, 65, 230), cv2.LINE_AA)
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

    def reset(self):
        """Forget what has been uncovered (needed when a frame earlier than the last one is requested)."""
        self.mask[:] = 0
        self.done = -1
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


# --------------------------------------------------------------------------- scene

class Scene:
    """A prepared animation: everything needed to render *any* frame of the video.

    `Scene.prepare()` runs the analysis once (steps 0-4: input check, mask, path, markers, timeline, framing);
    after that `scene.frame(i)` / `scene.render(i, size)` return frame number i. The video encoder
    (`run`) and the interactive preview of the UI use exactly the same object, so the preview shows what
    the video will contain.

    Frames may be requested in any order. `redraw` frames are independent; `reveal` frames accumulate a
    mask, so asking for an earlier frame than the previous one rebuilds the mask from scratch.
    """

    def __init__(self):
        self.cfg = None
        self.lock = threading.RLock()          # the renderer is stateful: one frame at a time

    @classmethod
    def prepare(cls, cfg, clean, track):
        sc = cls()
        sc.cfg = cfg = deep_merge(DEFAULT_CONFIG, cfg)       # autoscale_drawing() adjusts the copy, never the caller's config
        if cfg["mode"] == "traveler":
            cfg["mode"] = "follow"
        clean, track, sc.swapped, _ = check_inputs(clean, track, cfg)
        sc.clean, sc.track = clean, track
        h, w = clean.shape[:2]
        log.info("Maps loaded: %dx%d", w, h)
        log.info("[1/5] extracting track mask")
        sc.mask = extract_track_mask(clean, track, cfg)
        log.info("[2/5] extracting 1D trajectory")
        pts, cum, sc.skel = extract_path(sc.mask, cfg)
        log.info("[3/5] detecting markers")
        markers = detect_markers(sc.mask, pts, cum, cfg, sc.skel)
        sc.pts, sc.cum = trim_tails(pts, cum, markers)
        sc.markers = markers
        if cfg["mode"] != "follow":
            autoscale_drawing(cfg, markers)
        log.info("[4/5] building timeline")
        sc.schedule = build_schedule(markers, float(sc.cum[-1]), sc.cum, cfg)
        renderers = {"redraw": lambda: RedrawRenderer(clean, sc.pts, sc.cum, markers, cfg),
                     "reveal": lambda: RevealRenderer(clean, track, sc.pts, sc.cum, markers, cfg),
                     "follow": lambda: TravelerRenderer(clean, sc.pts, sc.cum, markers, cfg)}
        sc.renderer = renderers[cfg["mode"]]()
        sc.crop = frame_crop(w, h, sc.pts, markers, cfg["output"].get("aspect"))
        if cfg["mode"] == "follow":
            # Include the object and flag overhang when centering the output crop.
            fs = cfg["follow_settings"]
            object_pad = fs["object_size_px"] * (np.sqrt(2) if fs["object_icon"] in ("emoji", "custom") else 1)
            pad = max(sc.renderer.size * 1.6, object_pad,
                      0 if fs["hide_waypoints"] else fs["waypoint_diameter_px"] / 2 + fs["waypoint_outline_px"],
                      *(fs[end + "_size_px"] + max(abs(fs[end + "_offset_x_px"]), abs(fs[end + "_offset_y_px"]))
                        if fs[end + "_icon"] != "none" else 0 for end in ("start", "end")))
            sc.crop = frame_crop(w, h, sc.pts, [{"radius": pad}], cfg["output"].get("aspect"))
        sc._last_d = -1.0
        return sc

    # ---- timeline facts
    @property
    def fps(self):
        return self.cfg["output"]["fps"]

    @property
    def n_frames(self):
        return len(self.schedule)

    @property
    def native_size(self):
        """Size of the cropped frame (before any resize)."""
        return self.crop[2], self.crop[3]

    # ---- rendering
    def frame(self, i):
        """Frame i, cropped to the output frame, at native scale (BGR)."""
        i = min(max(i, 0), len(self.schedule) - 1)
        d, n_vis = self.schedule[i]
        if isinstance(self.renderer, TravelerRenderer):
            n_vis = int(i >= round(self.cfg["timing"]["first_marker_delay_sec"] * self.fps))
        x0, y0, cw, ch = self.crop
        with self.lock:
            if isinstance(self.renderer, RevealRenderer) and d < self._last_d:
                self.renderer.reset()
            self._last_d = d
            return self.renderer.frame(d, n_vis)[y0:y0 + ch, x0:x0 + cw]

    def render(self, i, size=None):
        """Frame i resized to `size` (width, height); same interpolation as the encoder."""
        img = self.frame(i)
        cw, ch = self.native_size
        if size and tuple(size) != (cw, ch):
            img = cv2.resize(img, tuple(size), interpolation=cv2.INTER_AREA if size[0] < cw else cv2.INTER_CUBIC)
        return img


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


class FfmpegWriter:
    """Video writer that pipes raw frames into ffmpeg (bundled via imageio-ffmpeg).

    OpenCV's own writer ignores quality settings and produces low-bitrate files; ffmpeg lets us
    pick a near-lossless CRF. Colours are tagged BT.709 so players show them as rendered.
    """
    # (x264/x265 CRF, mpeg4 qscale) per quality level; lower = better
    CRF = {"avc1": {"max": 8, "high": 13, "balanced": 19}, "hvc1": {"max": 10, "high": 15, "balanced": 21}}
    QSCALE = {"max": 1, "high": 3, "balanced": 5}

    def __init__(self, path, size, fps, codec, quality):
        import imageio_ffmpeg
        self.exe = imageio_ffmpeg.get_ffmpeg_exe()
        self.size, self.path = size, path
        q = quality if quality in self.QSCALE else "max"
        preset = "slow" if q != "balanced" else "medium"
        if codec == "avc1":
            venc = ["-c:v", "libx264", "-preset", preset, "-crf", str(self.CRF["avc1"][q]), "-profile:v", "high"]
        elif codec == "hvc1":
            venc = ["-c:v", "libx265", "-preset", preset, "-crf", str(self.CRF["hvc1"][q]), "-tag:v", "hvc1",
                    "-x265-params", "log-level=error"]
        else:  # mp4v: MPEG-4 part 2
            venc = ["-c:v", "mpeg4", "-q:v", str(self.QSCALE[q]), "-tag:v", "mp4v"]
        cmd = [self.exe, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24",
               "-s", f"{size[0]}x{size[1]}", "-r", str(fps), "-i", "-",
               "-vf", "scale=out_color_matrix=bt709:out_range=tv", *venc, "-pix_fmt", "yuv420p",
               "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv",
               "-movflags", "+faststart", path]
        self.err = tempfile.TemporaryFile()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=self.err)

    def isOpened(self):
        return self.proc.poll() is None

    def write(self, frame):
        try:
            self.proc.stdin.write(frame.tobytes())
        except (BrokenPipeError, OSError):
            raise AnimateError("ffmpeg stopped while encoding: " + self._error_text())

    def _error_text(self):
        self.err.seek(0)
        return self.err.read().decode("utf-8", "replace").strip()[-400:]

    def release(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        self.proc.wait()
        if self.proc.returncode not in (0, None):
            raise AnimateError("ffmpeg failed: " + self._error_text())
        self.err.close()


def open_writer(path, size, fps, codec, quality):
    """Best available video writer. Returns (writer, size, codec_used)."""
    even = (size[0] - size[0] % 2, size[1] - size[1] % 2)
    try:
        w = FfmpegWriter(path, even, fps, codec if codec in ("avc1", "hvc1", "mp4v") else "avc1", quality)
        if w.isOpened():
            return w, even, codec if codec in ("avc1", "hvc1", "mp4v") else "avc1"
    except Exception as e:  # noqa: BLE001 - ffmpeg missing: fall back to OpenCV
        log.warning("ffmpeg is not available (%s): using OpenCV's writer (quality cannot be controlled)", e)
    for cand in [codec] + [c for c in ("avc1", "hvc1", "mp4v") if c != codec]:
        vw = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*cand), fps, even)
        if vw.isOpened():
            if cand != codec:
                log.warning("Codec %s is not available here, using %s instead", codec, cand)
            return vw, even, cand
        vw.release()
    raise AnimateError(f"Cannot open video writer for {path}")


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
                           f"vs track {track.shape[1]}x{track.shape[0]}",
                           "sizes_differ", w1=clean.shape[1], h1=clean.shape[0], w2=track.shape[1], h2=track.shape[0])
    scene = Scene.prepare(cfg, clean, track)             # steps 0-4: analysis, markers, timeline, framing
    cfg = scene.cfg
    out_path = resolve(cfg["output"]["video_path"], base_dir)
    if debug:
        dbg_dir = resolve(cfg["output"].get("debug_dir") or os.path.dirname(out_path), base_dir)
        os.makedirs(dbg_dir, exist_ok=True)
        save_debug(os.path.join(dbg_dir, os.path.splitext(os.path.basename(out_path))[0]),
                   scene.mask, scene.skel, scene.pts, scene.markers, scene.track)

    log.info("[5/5] rendering (%s mode)", cfg["mode"])
    cw, ch = scene.native_size
    log.info("Frame: %dx%d region of the map at (%d,%d)", cw, ch, scene.crop[0], scene.crop[1])
    size = tuple(cfg["output"]["resolution"]) if cfg["output"]["resolution"] else (cw, ch)
    codec = cfg["output"]["codec"]
    if len(codec) != 4:
        raise AnimateError(f"Codec must be a 4-char fourcc, got '{codec}'")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    vw, size, codec = open_writer(out_path, size, cfg["output"]["fps"], codec, cfg["output"].get("quality", "max"))
    log.info("Encoding %s, quality=%s, %dx%d", codec, cfg["output"].get("quality", "max"), *size)
    try:
        cache_key, cache_img = None, None
        for i in progress(range(scene.n_frames), "Rendering + encoding"):
            d, n_vis = scene.schedule[i]
            key = (round(d, 3), n_vis, i >= round(cfg["timing"]["first_marker_delay_sec"] * scene.fps))
            if key != cache_key:  # holds/pauses reuse the previous frame
                cache_key, cache_img = key, scene.render(i, size)
            vw.write(cache_img)
    except BaseException:
        if hasattr(vw, "proc"):
            vw.proc.kill()
        raise
    vw.release()
    log.info("Done: %s (%d frames, %dx%d @ %s fps)", out_path, scene.n_frames, size[0], size[1],
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
