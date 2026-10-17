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

import cv2
import networkx as nx
import numpy as np
from scipy.ndimage import gaussian_filter1d
from scipy.spatial import cKDTree
from skimage.morphology import skeletonize
from tqdm import tqdm

log = logging.getLogger("animate_map")

DEFAULT_CONFIG = {
    "inputs": {"clean_map": "clean_map.jpg", "track_map": "track_map.jpg"},
    "output": {"video_path": "output_animation.mp4", "fps": 30, "codec": "mp4v", "resolution": None},
    "mode": "redraw",
    "timing": {
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
    },
    "hsv_blue_range": {"lower": [90, 50, 50], "upper": [130, 255, 255]},
    "path_settings": {"spur_length_px": 25, "smooth_sigma": 1.2, "diff_threshold": 15},
    "logging": {"file": "animate_map.log", "level": "INFO"},
}


class AnimateError(Exception):
    """User-facing error (bad input, no track found, ...)."""


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
    """Cut skeleton stubs that stick out past the first/last marker disc."""
    lo, hi = 0, len(pts) - 1
    if markers:
        a, b = markers[0], markers[-1]
        if a["dist"] < 1.5 * a["radius"]:
            lo = a["idx"]
        if cum[-1] - b["dist"] < 1.5 * b["radius"]:
            hi = b["idx"]
    if hi <= lo:
        return pts, cum
    off = cum[lo]
    for m in markers:
        m["idx"] = min(max(m["idx"] - lo, 0), hi - lo)
        m["dist"] = min(max(m["dist"] - off, 0.0), cum[hi] - off)
    if lo or hi != len(pts) - 1:
        log.info("Trimmed path tails: %d px at start, %d px at end", lo, len(pts) - 1 - hi)
    return pts[lo:hi + 1], cum[lo:hi + 1] - off


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
        d[k] = max(1, int(round(d[k] * f)))
    rs["marker_font_scale"] = rs["marker_font_scale"] * f


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
    init_f = int(round(t["initial_delay_sec"] * fps))
    move_f = max(1, int(round(t["track_duration_sec"] * fps)))
    pause_f = int(round(t["waypoint_pause_duration_sec"] * fps)) if t["pause_on_waypoints"] else 0
    final_f = int(round(t["final_delay_sec"] * fps))
    mid = markers[1:-1] if len(markers) > 2 else []
    if not t["pause_on_waypoints"]:
        mid = []

    def visible(d):  # markers reached at distance d (start marker is always on)
        return sum(1 for m in markers if m["dist"] <= d + 1e-6 or m is markers[0])

    frames = [(0.0, visible(0.0))] * init_f
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

    expected = round((t["initial_delay_sec"] + t["track_duration_sec"]
                      + len(mid) * t["waypoint_pause_duration_sec"] + t["final_delay_sec"]) * fps)
    log.info("Frames: %d (expected ~%d), duration %.2fs, waypoints with pause: %d",
             len(frames), expected, len(frames) / fps, len(mid))
    return frames


# --------------------------------------------------------------------------- step 5

def draw_marker(img, center, label, rs):
    c = (int(round(center[0])), int(round(center[1])))
    r = rs["marker_radius"]
    cv2.circle(img, c, r + 2, (255, 255, 255), -1, cv2.LINE_AA)
    cv2.circle(img, c, r, tuple(rs["marker_color_bgr"]), -1, cv2.LINE_AA)
    fs, ft = rs["marker_font_scale"], rs["marker_font_thickness"]
    (tw, th), base = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, fs, ft)
    org = (c[0] - tw // 2, c[1] + th // 2)
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


class RevealRenderer:
    def __init__(self, clean, track, pts, cum, markers, cfg):
        self.clean, self.track, self.pts, self.cum, self.markers = clean, track, pts, cum, markers
        rv = cfg["reveal_settings"]
        self.r_line, self.r_wp = int(rv["line_brush_radius"]), int(rv["waypoint_brush_radius"])
        self.mask = np.zeros(clean.shape[:2], np.uint8)
        self.done = -1
        # path points close to a marker get the big brush
        tree = cKDTree(np.array([m["center"] for m in markers])) if markers else None
        if tree is not None:
            dd, _ = tree.query(pts)
            reach = np.array([m["radius"] for m in markers]).max() + self.r_wp
            self.big = dd <= reach
        else:
            self.big = np.zeros(len(pts), bool)
        self.centers = [(int(round(m["center"][0])), int(round(m["center"][1]))) for m in markers]
        self.drawn_markers = 0

    def frame(self, d, n_vis):
        i = head_index(self.cum, d)
        for k in range(self.done + 1, i + 1):
            r = self.r_wp if self.big[k] else self.r_line
            cv2.circle(self.mask, (int(round(self.pts[k, 0])), int(round(self.pts[k, 1]))), r, 255, -1)
        self.done = max(self.done, i)
        hp = head_point(self.pts, self.cum, d)
        cv2.circle(self.mask, (int(round(hp[0])), int(round(hp[1]))), self.r_line, 255, -1)
        while self.drawn_markers < n_vis:  # fully uncover each marker once it is reached
            cv2.circle(self.mask, self.centers[self.drawn_markers], self.r_wp, 255, -1)
            self.drawn_markers += 1
        a = cv2.GaussianBlur(self.mask, (5, 5), 0).astype(np.float32)[..., None] / 255.0
        out = self.clean.astype(np.float32) * (1 - a) + self.track.astype(np.float32) * a
        return out.astype(np.uint8)


# --------------------------------------------------------------------------- main

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
    h, w = clean.shape[:2]
    log.info("Maps loaded: %dx%d", w, h)

    log.info("Step 1: extracting track mask")
    mask = extract_track_mask(clean, track, cfg)
    log.info("Step 2: extracting 1D trajectory")
    pts, cum, skel = extract_path(mask, cfg)
    log.info("Step 3: detecting markers")
    markers = detect_markers(mask, pts, cum, cfg, skel)
    pts, cum = trim_tails(pts, cum, markers)
    autoscale_drawing(cfg, markers)
    out_path = resolve(cfg["output"]["video_path"], base_dir)
    if debug:
        save_debug(os.path.splitext(out_path)[0], mask, skel, pts, markers, track)
    log.info("Step 4: building timeline")
    schedule = build_schedule(markers, float(cum[-1]), cum, cfg)

    log.info("Step 5: rendering (%s mode)", cfg["mode"])
    renderer = (RedrawRenderer(clean, pts, cum, markers, cfg) if cfg["mode"] == "redraw"
                else RevealRenderer(clean, track, pts, cum, markers, cfg))
    size = tuple(cfg["output"]["resolution"]) if cfg["output"]["resolution"] else (w, h)
    codec = cfg["output"]["codec"]
    if len(codec) != 4:
        raise AnimateError(f"Codec must be a 4-char fourcc, got '{codec}'")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    vw = cv2.VideoWriter(out_path, cv2.VideoWriter_fourcc(*codec), cfg["output"]["fps"], size)
    if not vw.isOpened():
        raise AnimateError(f"Cannot open video writer for {out_path} (codec {codec})")
    try:
        cache_key, cache_img = None, None
        for d, n_vis in tqdm(schedule, desc="Rendering", unit="frame"):
            key = (round(d, 3), n_vis)
            if key != cache_key:  # holds/pauses reuse the previous frame
                img = renderer.frame(d, n_vis)
                if size != (w, h):
                    img = cv2.resize(img, size, interpolation=cv2.INTER_AREA if size[0] < w else cv2.INTER_CUBIC)
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
