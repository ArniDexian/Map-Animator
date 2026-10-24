#!/usr/bin/env python3
"""Smoke-check follow rendering and Scene scheduling on synthetic maps."""
import base64
import copy
import os
import sys
from unittest import mock

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import animate_map as am  # noqa: E402


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def config(mode, *, fps=12, aspect=None):
    cfg = copy.deepcopy(am.DEFAULT_CONFIG)
    cfg["mode"] = mode
    cfg["output"].update(fps=fps, aspect=aspect, resolution=None)
    cfg["timing"].update(first_marker_delay_sec=0.3, initial_delay_sec=0.3,
                         track_duration_sec=2.4, pause_on_waypoints=True,
                         waypoint_pause_duration_sec=0.15, final_delay_sec=0.5)
    return cfg


def verify_renderer():
    clean = np.full((150, 200, 3), (28, 30, 32), np.uint8)
    pts = np.array([[20, 90], [60, 90], [60, 45], [20, 45]], dtype=np.float64)
    cum = np.array([0.0, 40.0, 85.0, 125.0])
    cfg = config("follow")
    require(cfg["follow_settings"].get("end_always_visible") is False,
            "finish icon should remain arrival-only by default")
    cfg["redraw_settings"].update(line_color_bgr=[255, 0, 0], line_thickness=4,
                                   marker_font_path=None)
    cfg["follow_settings"].update(object_size_px=16, start_icon="green_flag", end_icon="checkered_flag",
                                   start_size_px=16, end_size_px=16)
    renderer = am.TravelerRenderer(clean, pts, cum, [], cfg)

    require(np.array_equal(renderer.frame(0.0, 0), clean), "n_vis=0 must leave the clean map hidden")
    start = renderer.frame(0.0, 1)
    require(not np.array_equal(start, clean), "the initial arrow and start flag should be visible")

    partial = renderer.frame(20.0, 1)
    line_color = np.array(cfg["redraw_settings"]["line_color_bgr"], np.uint8)
    require(np.array_equal(partial[90, 25], line_color), "the traveled segment should be drawn")
    require(np.array_equal(partial[90, 58], clean[90, 58]), "the route ahead of the arrow must remain hidden")

    angles = [renderer.heading(d) for d in (20.0, 62.5, 105.0)]
    require(abs(angles[0]) < 0.05, "rightward heading is incorrect")
    require(abs(angles[1] + np.pi / 2) < 0.05, "upward heading is incorrect")
    require(abs(abs(angles[2]) - np.pi) < 0.05, "leftward heading is incorrect")

    saved = renderer.frame(62.5, 1)
    renderer.frame(10.0, 1)
    require(np.array_equal(saved, renderer.frame(62.5, 1)), "random-access rendering must be deterministic")

    final = renderer.frame(float(cum[-1]), 1)
    before_final = renderer.frame(float(cum[-1] - 1), 1)
    checker = (30, 22)
    require(np.array_equal(before_final[checker], clean[checker]), "finish flag appeared before the endpoint")
    require(np.array_equal(final[checker], np.array([45, 45, 45], np.uint8)),
            "finish flag should appear at the endpoint")
    print("PASS renderer: visibility, no route ahead, right/up/left heading, final flag, random access")


def line_only_maps():
    clean = np.full((160, 240, 3), (205, 211, 218), np.uint8)
    track = clean.copy()
    route = np.array([[30, 125], [90, 125], [90, 70], [30, 70]], np.int32)
    cv2.polylines(track, [route], False, (255, 0, 0), 7, cv2.LINE_AA)
    return clean, track


def verify_line_only_scene():
    clean, track = line_only_maps()
    cfg = config("follow")
    cfg["output"]["aspect"] = None
    cfg["circle_detection"]["min_circle_radius_px"] = 12.0
    scene = am.Scene.prepare(cfg, clean, track)
    require(len(scene.markers) == 0, "the synthetic route should contain no waypoint markers")
    require(scene.n_frames > 0, "line-only traveler scene should have frames")
    require(np.array_equal(scene.frame(0), clean), "line-only scene should start on the clean map")
    first_visible = round(scene.cfg["timing"]["first_marker_delay_sec"] * scene.fps)
    require(not np.array_equal(scene.frame(first_visible), clean),
            "traveler should appear after the pre-delay even when no markers were detected")
    print("PASS Scene: line-only input renders and shows the follow arrow after the pre-delay")


def load_demo_maps():
    clean = cv2.imread(os.path.join(ROOT, "examples", "demo_clean_map.jpg"), cv2.IMREAD_COLOR)
    track = cv2.imread(os.path.join(ROOT, "examples", "demo_track_map.jpg"), cv2.IMREAD_COLOR)
    require(clean is not None and track is not None, "could not read the synthetic demo maps")
    return clean, track


def verify_follow_waypoints():
    clean = np.full((160, 220, 3), (40, 50, 60), np.uint8)
    pts = np.array([[30, 80], [100, 80], [170, 80]], dtype=np.float64)
    cum = np.array([0.0, 70.0, 140.0])
    markers = [
        {"center": (30.0, 80.0), "dist": 0.0, "radius": 10.0, "label": "A"},
        {"center": (100.0, 80.0), "dist": 70.0, "radius": 10.0, "label": "B"},
        {"center": (170.0, 80.0), "dist": 140.0, "radius": 10.0, "label": "C"},
    ]
    cfg = config("follow")
    cfg["redraw_settings"].update(line_color_bgr=[255, 0, 0], line_thickness=4,
                                   marker_text_color_bgr=[0, 0, 255], marker_font_path=None)
    cfg["follow_settings"].update(waypoint_color_bgr=[20, 210, 40],
                                   object_size_px=16,
                                   waypoint_outline_color_bgr=[0, 0, 0],
                                   waypoint_diameter_px=24, waypoint_outline_px=2)
    renderer = am.TravelerRenderer(clean, pts, cum, markers, cfg)
    baseline = am.TravelerRenderer(clean, pts, cum, [], cfg).frame(110.0, 1)
    hidden_cfg = copy.deepcopy(cfg)
    hidden_cfg["follow_settings"]["hide_waypoints"] = True
    hidden = am.TravelerRenderer(clean, pts, cum, markers, hidden_cfg).frame(110.0, 1)
    require(np.array_equal(hidden, baseline), "hide_waypoints should remove only the reached circles")
    unreached = renderer.frame(69.0, 1)
    reached = renderer.frame(70.0, 1)
    require(np.array_equal(unreached[90, 100], clean[90, 100]), "waypoint appeared before its distance")
    require(np.array_equal(reached[90, 100], np.array([20, 210, 40], np.uint8)),
            "reached waypoint should use the configured fill color")
    require(np.array_equal(reached[80, 108], np.array([20, 210, 40], np.uint8)),
            "the reached waypoint should remain visible beside the moving object")

    settled = renderer.frame(110.0, 1)
    require(np.array_equal(settled[80, 114], np.array([0, 0, 0], np.uint8)),
            "configured waypoint outline color or diameter was not applied")
    require(np.array_equal(settled[80, 116], baseline[80, 116]),
            "waypoint diameter should include a 24 px disc and 2 px outline")
    require(not np.any(np.all(settled[68:93, 88:113] == [0, 0, 255], axis=2)),
            "follow waypoints must not contain letters")
    require(np.array_equal(settled[92, 30], clean[92, 30]), "start marker A should not become a circle")
    require(np.array_equal(settled[92, 170], clean[92, 170]), "finish marker C should not become a circle")

    no_outline_cfg = copy.deepcopy(cfg)
    no_outline_cfg["follow_settings"]["waypoint_outline_px"] = 0
    no_outline = am.TravelerRenderer(clean, pts, cum, markers, no_outline_cfg).frame(110.0, 1)
    require(np.array_equal(no_outline[80, 114], baseline[80, 114]),
            "zero outline width should leave the route visible outside the waypoint disc")
    # Detection can put the original circle center away from the smoothed line.
    offset_markers = copy.deepcopy(markers)
    offset_markers[1]["center"] = (100.0, 115.0)
    aligned = am.TravelerRenderer(clean, pts, cum, offset_markers, cfg).frame(110.0, 1)
    require(np.array_equal(aligned[80, 108], np.array([20, 210, 40], np.uint8)),
            "waypoint center must lie on the rendered trajectory")
    require(np.array_equal(aligned[115, 100], clean[115, 100]),
            "the off-path detected center must not be used for drawing")
    print("PASS follow waypoints: reach threshold, appearance, styling, no labels or endpoint circles")


def verify_follow_object_rotation_flips_and_offsets():
    clean = np.full((120, 140, 3), (30, 30, 30), np.uint8)
    pts = np.array([[30.0, 60.0], [100.0, 60.0]])
    cum = np.array([0.0, 70.0])
    image = np.zeros((12, 12, 4), np.uint8)
    image[2:5, 9:12] = (255, 0, 0, 255)  # Asymmetric marker on the upper-right of the image.
    ok, png = cv2.imencode(".png", cv2.cvtColor(image, cv2.COLOR_RGBA2BGRA))
    require(ok, "could not encode the synthetic moving object")
    data_uri = "data:image/png;base64," + base64.b64encode(png.tobytes()).decode("ascii")

    def render(rotation=0, flip_horizontal=False, flip_vertical=False, offset=None):
        cfg = config("follow")
        cfg["follow_settings"].update(object_icon="custom", object_size_px=24, object_image=data_uri,
                                       object_rotation_deg=rotation,
                                       object_flip_horizontal=flip_horizontal,
                                       object_flip_vertical=flip_vertical,
                                       object_offset=offset or {"x": 0.0, "y": 0.0},
                                       hide_waypoints=True, start_icon="none", end_icon="none")
        renderer = am.TravelerRenderer(clean, pts, cum, [], cfg)
        output = renderer.frame(35.0, 1)
        red = (output[:, :, 2] > 180) & (output[:, :, 0] < 40)
        ys, xs = np.where(red)
        require(len(xs) > 0, "the synthetic object marker was not rendered")
        return float(xs.mean()), float(ys.mean())

    base_x, base_y = render()
    require(base_x > 65 and base_y < 60,
            "a right-facing image should keep its upper-right marker on a rightward route")

    flipped_x, flipped_y = render(flip_horizontal=True)
    require(flipped_x < 65 and flipped_y < 60, "horizontal reflection should move the marker to the left")
    vertical_x, vertical_y = render(flip_vertical=True)
    require(vertical_x > 65 and vertical_y > 60, "vertical reflection should move the marker below the center")
    rotated_x, rotated_y = render(rotation=90)
    require(rotated_y > base_y + 5, "a 90-degree object rotation should rotate the marker downward")
    offset_x, offset_y = render(offset={"x": 0.5, "y": 0.5})
    require(offset_x > base_x + 10 and offset_y > base_y + 10,
            "positive X/Y offsets should move the image right and down by the configured fraction")

    def rendered_motorcycle(points, explicit_flip=False):
        motorcycle_cfg = config("follow")
        motorcycle_cfg["redraw_settings"]["line_color_bgr"] = [30, 30, 30]
        motorcycle_cfg["follow_settings"].update(object_icon="emoji", object_emoji="🏍️", object_size_px=24,
                                                   object_flip_horizontal=explicit_flip, hide_waypoints=True,
                                                   start_icon="none", end_icon="none")
        renderer = am.TravelerRenderer(clean, points, cum, [], motorcycle_cfg)
        output = renderer.frame(35.0, 1)
        source = renderer.object_image
        if points[1][0] > points[0][0]:
            source = cv2.flip(source, 1) if not explicit_flip else source
        else:
            source = cv2.flip(source, 1) if explicit_flip else source
        h, w = source.shape[:2]
        center_x, center_y = 65, 60
        left, top = center_x - w // 2, center_y - h // 2
        alpha = source[:, :, 3:4].astype(np.float32) / 255
        background = clean[top:top + h, left:left + w]
        expected = np.round(source[:, :, :3] * alpha + background * (1 - alpha)).astype(np.uint8)
        require(np.array_equal(output[top:top + h, left:left + w], expected),
                "motorcycle heading should preserve upright wheels and respect the manual horizontal flip")

    right_points = np.array([[30.0, 60.0], [100.0, 60.0]])
    left_points = right_points[::-1].copy()
    rendered_motorcycle(right_points)
    rendered_motorcycle(left_points)
    rendered_motorcycle(right_points, explicit_flip=True)
    print("PASS moving object: rotation, horizontal/vertical reflection and X/Y offsets")


def verify_endpoint_icons():
    clean = np.full((160, 220, 3), (40, 50, 60), np.uint8)
    pts = np.array([[30.0, 80.0], [110.0, 80.0], [190.0, 80.0]])
    cum = np.array([0.0, 80.0, 160.0])
    markers = [{"center": tuple(p), "dist": float(d), "radius": 8.0, "label": label}
               for p, d, label in zip(pts, cum, ("A", "B", "C"))]

    def flag_image(settings, point=(110, 80), finish=False):
        cfg = config("follow")
        cfg["follow_settings"].update(settings)
        renderer = am.TravelerRenderer(clean, pts, cum, markers, cfg)
        image = clean.copy()
        renderer.draw_flag(image, point, finish)
        return image

    none = flag_image({"start_icon": "none"})
    require(np.array_equal(none, clean), "none should leave the canvas unchanged")
    for kind in ("green_flag", "red_flag", "checkered_flag", "pin"):
        start = flag_image({"start_icon": kind, "end_icon": "none"})
        end = flag_image({"start_icon": "none", "end_icon": kind}, finish=True)
        require(np.any(start != clean), f"start icon {kind} did not render")
        require(np.any(end != clean), f"end icon {kind} did not render independently")

    both = {"start_icon": "none", "end_icon": "red_flag"}
    require(np.array_equal(flag_image(both, finish=False), clean), "end icon setting changed the start icon")
    require(np.any(flag_image(both, finish=True) != clean), "end red flag did not render")

    visibility_cfg = config("follow")
    visibility_cfg["follow_settings"].update(start_icon="none", end_icon="red_flag", end_always_visible=False)
    hidden_renderer = am.TravelerRenderer(clean, pts, cum, markers, visibility_cfg)
    require(np.array_equal(hidden_renderer.frame(0, 0), clean),
            "finish icon should stay hidden during the initial pause by default")
    require(np.array_equal(hidden_renderer.frame(80, 1)[40:80, 170:210], clean[40:80, 170:210]),
            "finish icon should remain hidden before arrival by default")
    require(np.any(hidden_renderer.frame(cum[-1], 1)[40:80, 170:210] != clean[40:80, 170:210]),
            "finish icon should still appear on arrival by default")

    visibility_cfg["follow_settings"]["end_always_visible"] = True
    visible_renderer = am.TravelerRenderer(clean, pts, cum, markers, visibility_cfg)
    require(np.any(visible_renderer.frame(0, 0)[40:80, 170:210] != clean[40:80, 170:210]),
            "always-visible finish icon should appear during the initial pause")
    require(np.any(visible_renderer.frame(80, 1)[40:80, 170:210] != clean[40:80, 170:210]),
            "always-visible finish icon should remain visible before arrival")

    rgba = np.empty((8, 12, 4), np.uint8)
    rgba[:] = (220, 80, 30, 128)  # RGBA, semi-transparent orange
    ok, png = cv2.imencode(".png", cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGRA))
    require(ok, "could not encode the synthetic custom icon")
    data_uri = "data:image/png;base64," + base64.b64encode(png.tobytes()).decode("ascii")
    custom_settings = {"start_icon": "custom", "end_icon": "none", "start_size_px": 32,
                       "start_image": data_uri}
    custom = flag_image(custom_settings)
    changed = np.any(custom != clean, axis=2)
    ys, xs = np.where(changed)
    width, height = xs.max() - xs.min() + 1, ys.max() - ys.min() + 1
    require(abs(width / height - 1.5) < 0.06, "custom icon aspect ratio was not preserved")
    require(xs.min() + xs.max() == 219 and ys.max() == 79,
            "custom icon should be bottom-center anchored at the endpoint")
    require(np.array_equal(custom[70, 110], np.array([35, 65, 140], np.uint8)),
            "custom icon alpha was not composited over the map")

    renderer_cfg = config("follow")
    renderer_cfg["follow_settings"].update(custom_settings)
    edge_renderer = am.TravelerRenderer(clean, pts, cum, markers, renderer_cfg)
    clipped = clean.copy()
    edge_renderer.draw_flag(clipped, (2, 80))
    ys, xs = np.where(np.any(clipped != clean, axis=2))
    require(xs.min() == 0 and xs.max() == 17 and ys.max() == 79,
            "custom icon should clip cleanly at an image edge while keeping its anchor")

    bad_cfg = config("follow")
    bad_cfg["follow_settings"].update(start_icon="custom", start_image="data:image/png;base64,not-base64!")
    try:
        am.TravelerRenderer(clean, pts, cum, markers, bad_cfg)
    except am.AnimateError as error:
        require(error.code == "endpoint_image_invalid", "malformed custom image used the wrong error code")
    else:
        raise AssertionError("malformed custom image should raise AnimateError")
    print("PASS endpoint icons: independent built-ins, none, custom alpha/aspect/anchor/clipping, malformed data")


def verify_emojis_and_pin():
    for size in (8, 9, 12, 16, 32):
        pin = am.render_endpoint_pin(size)
        alpha = pin[..., 3]
        require(pin.shape[0] == size and np.any(alpha > 0), f"pin did not render at size {size}")
        require(np.array_equal(alpha, alpha[:, ::-1]), f"pin silhouette is asymmetric at size {size}")
        red = (pin[..., 2] > 150) & (pin[..., 0] < 100) & (pin[..., 1] < 130) & (alpha > 0)
        require(np.any(red), f"pin lost its red fill at size {size}")
        cy, cx = round(size * 0.36), (pin.shape[1] - 1) // 2
        hole = pin[max(0, cy - 1):cy + 2, max(0, cx - 1):cx + 3]
        white = np.all(hole[..., :3] > 190, axis=2) & (hole[..., 3] > 200)
        require(np.any(white), f"pin lost its white center at size {size}")

    catalog = am.emoji_catalog()
    require(len(am.endpoint_emoji_choices()) > 3000 and len(catalog["groups"]) >= 9,
            "emoji catalog must contain the full categorized dataset")
    samples = {"📍", "🏁", "🏍️", "☕", "🌳", "👩🏽‍💻", "🇵🇱", "🦄"}
    samples.update(group["emojis"][0]["emoji"] for group in catalog["groups"])
    for emoji in samples:
        icon = am.render_endpoint_emoji(emoji)
        alpha = icon[..., 3]
        colors = np.unique(icon[..., :3][alpha > 0], axis=0)
        require(icon.ndim == 3 and icon.shape[2] == 4 and alpha.max() > 0,
                f"emoji {emoji} did not produce a transparent BGRA image")
        if emoji not in {item["emoji"] for group in catalog["groups"] if group["name"] == "Component" for item in group["emojis"]}:
            require(len(colors) > 1, f"emoji {emoji} did not render in color")

    clean = np.full((160, 220, 3), (40, 50, 60), np.uint8)
    pts = np.array([[30.0, 80.0], [110.0, 80.0], [190.0, 80.0]])
    cum = np.array([0.0, 80.0, 160.0])
    markers = [{"center": tuple(p), "dist": float(d), "radius": 8.0, "label": label}
               for p, d, label in zip(pts, cum, ("A", "B", "C"))]
    cfg = config("follow")
    cfg["follow_settings"].update(start_icon="emoji", end_icon="emoji", start_emoji="📍", end_emoji="🏁",
                                   start_size_px=32, end_size_px=48)
    renderer = am.TravelerRenderer(clean, pts, cum, markers, cfg)
    for endpoint, emoji, size in (("start", "📍", 32), ("end", "🏁", 48)):
        source = am.render_endpoint_emoji(emoji)
        h, w = source.shape[:2]
        scale = size / max(h, w)
        expected = cv2.resize(source, (max(1, round(w * scale)), max(1, round(h * scale))),
                              interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC)
        require(np.array_equal(renderer.endpoint_images[endpoint], expected),
                f"{endpoint} used the other endpoint's emoji or size")
        canvas = clean.copy()
        renderer.draw_flag(canvas, (30, 80) if endpoint == "start" else (190, 80), finish=endpoint == "end")
        require(np.any(canvas != clean), f"{endpoint} emoji did not composite onto the map")

    pin_cfg = config("follow")
    pin_cfg["follow_settings"].update(start_icon="pin", end_icon="none", start_size_px=8)
    tiny_pin = am.TravelerRenderer(clean, pts, cum, markers, pin_cfg)
    require(np.array_equal(tiny_pin.endpoint_images["start"], am.render_endpoint_pin(8)),
            "small pin was not used by the endpoint renderer")

    invalid = config("follow")
    invalid["follow_settings"].update(start_icon="emoji", start_emoji="not an emoji")
    try:
        am.TravelerRenderer(clean, pts, cum, markers, invalid)
    except am.AnimateError as error:
        require(error.code == "endpoint_emoji_invalid", "invalid emoji used the wrong error code")
    else:
        raise AssertionError("emoji outside the allowed list should be rejected")
    print("PASS emoji and pin: allowed glyphs, independent endpoints, invalid choice, symmetric pin and small sizes")


def verify_complete_emoji_catalog():
    catalog = am.emoji_catalog()
    count = 0
    for group in catalog["groups"]:
        for item in group["emojis"]:
            image = am.render_endpoint_emoji(item["emoji"])
            require(image.shape[2] == 4 and image[:, :, 3].max() > 0, f"empty catalog emoji: {item['name']}")
            require(max(image.shape[:2]) <= 160, f"unjoined emoji sequence: {item['name']}")
            count += 1
    require(count == len(am.endpoint_emoji_choices()), "catalog must not contain duplicate entries")
    print(f"PASS full catalog: {count} single-glyph colour emoji, including modifiers, ZWJ and flags")


def _bitmap_to_bgra(image):
    require(image is not None, "expected emoji bitmap is missing")
    bounds = image.getbbox()
    require(bounds is not None, "expected emoji bitmap is empty")
    return cv2.cvtColor(np.array(image.crop(bounds)), cv2.COLOR_RGBA2BGRA)


def verify_platform_emoji_sources_and_defaults():
    settings = am.DEFAULT_CONFIG["follow_settings"]
    require(settings["start_icon"] == settings["end_icon"] == "emoji",
            "both endpoint icon defaults should use the emoji type")
    require(settings["start_emoji"] == "green_flag" and settings["end_emoji"] == "🏁",
            "default endpoints should use green_flag and checkered flag emoji")
    catalog = am.emoji_catalog()
    green_entries = [item for group in catalog["groups"] for item in group["emojis"]
                     if item["emoji"] == "green_flag"]
    require(len(green_entries) == 1 and green_entries[0].get("origin") == "app",
            "green_flag should be one app-local catalog entry")
    require("green_flag" in am.endpoint_emoji_choices(), "green_flag should be selectable")

    native = am.system_emoji_font()
    if am.sys.platform == "darwin":
        require(native is not None, "macOS should expose its Apple Color Emoji sbix font")
        for emoji in ("❤️", "🏍️", "🫱🏻‍🫲🏼", "🧑🏻‍❤️‍🧑🏿"):
            expected = _bitmap_to_bgra(am.emoji_bitmap(native, emoji))
            actual = am.render_endpoint_emoji(emoji)
            require(np.array_equal(actual, expected),
                    f"{emoji} should render from the native Apple sbix bitmap on macOS")
        print("PASS native emoji: Apple sbix output matches ❤️ and 🏍️ rendering")
    else:
        print("SKIP native Apple emoji comparison outside macOS")

    # On a non-MacOS host the renderer must use the bundled Noto bitmap.
    emoji = "❤️"
    expected = _bitmap_to_bgra(am.emoji_bitmap(am.endpoint_emoji_font(), emoji))
    am.render_endpoint_emoji.cache_clear()
    try:
        with mock.patch.object(am.sys, "platform", "linux"):
            am.system_emoji_font.cache_clear()
            require(am.system_emoji_font() is None, "non-macOS must not load Apple's system font")
            actual = am.render_endpoint_emoji(emoji)
            require(np.array_equal(actual, expected), "non-macOS rendering should use bundled Noto")
    finally:
        am.system_emoji_font.cache_clear()
        am.render_endpoint_emoji.cache_clear()

    # A current Mac may gain support later; force a missing native glyph for a Unicode 18 entry
    # to keep verifying the Noto fallback path independently of the installed OS font version.
    new_unicode_emoji = "🫹"  # leftwards thumb sign, Emoji 18.0
    require(new_unicode_emoji in am.endpoint_emoji_choices(), "Unicode 18.0 fallback fixture is missing")
    noto = am.endpoint_emoji_font()
    expected = _bitmap_to_bgra(am.emoji_bitmap(noto, new_unicode_emoji))
    native_missing = object()
    original_bitmap = am.emoji_bitmap

    def miss_only_native(font_info, value):
        if font_info is native_missing:
            return None
        return original_bitmap(font_info, value)

    am.render_endpoint_emoji.cache_clear()
    try:
        with mock.patch.object(am, "system_emoji_font", return_value=native_missing), \
                mock.patch.object(am, "emoji_bitmap", side_effect=miss_only_native):
            actual = am.render_endpoint_emoji(new_unicode_emoji)
            require(np.array_equal(actual, expected),
                    "an unsupported Unicode 18 native glyph should fall back to Noto")
    finally:
        am.system_emoji_font.cache_clear()
        am.render_endpoint_emoji.cache_clear()

    # The app-local green flag changes only red fabric to green: bounds, alpha, and pole stay intact.
    red = am.render_endpoint_emoji("🚩")
    green = am.render_endpoint_emoji("green_flag")
    require(red.shape == green.shape, "green flag recoloring changed the source glyph dimensions")
    require(np.array_equal(red[:, :, 3], green[:, :, 3]), "green flag recoloring changed the alpha silhouette")
    source_rgb = cv2.cvtColor(red[:, :, :3], cv2.COLOR_BGR2RGB)
    source_hsv = cv2.cvtColor(source_rgb, cv2.COLOR_RGB2HSV)
    red_mask = (((source_hsv[:, :, 0] < 12) | (source_hsv[:, :, 0] > 170)) &
                (source_hsv[:, :, 1] > 90) & (red[:, :, 3] > 0))
    require(np.any(red_mask), "source 🚩 should contain red fabric to recolor")
    expected_hsv = source_hsv.copy()
    expected_hsv[:, :, 0][red_mask] = 60
    expected_rgb = cv2.cvtColor(expected_hsv, cv2.COLOR_HSV2RGB)
    expected_green = red.copy()
    expected_green[red_mask, :3] = cv2.cvtColor(expected_rgb, cv2.COLOR_RGB2BGR)[red_mask]
    require(np.array_equal(green, expected_green),
            "green_flag should preserve every source pixel except red-to-green hue changes")
    require(np.any(green[:, :, :3][red_mask] != red[:, :, :3][red_mask]),
            "red flag fabric should visibly become green")
    require(np.array_equal(green[~red_mask], red[~red_mask]),
            "green flag recoloring should preserve the pole and other non-red pixels")
    print("PASS endpoints: defaults, app-local green flag recolor, native Apple glyphs, and Noto fallbacks")


def verify_endpoint_offsets():
    clean = np.full((240, 320, 3), (40, 50, 60), np.uint8)
    pts, cum = np.array([[100., 160.], [220., 160.]]), np.array([0., 120.])
    for kind in ("green_flag", "red_flag", "checkered_flag", "pin", "emoji", "custom"):
        cfg = config("follow")
        ok, png = cv2.imencode(".png", np.full((8, 12, 4), (20, 180, 240, 200), np.uint8))
        require(ok, "fixture image must encode")
        image = "data:image/png;base64," + base64.b64encode(png.tobytes()).decode()
        cfg["follow_settings"].update(start_icon=kind, end_icon=kind, start_image=image, end_image=image)
        original = am.TravelerRenderer(clean, pts, cum, [], cfg)
        shifted_cfg = copy.deepcopy(cfg)
        shifted_cfg["follow_settings"].update(start_offset_x_px=-11, start_offset_y_px=9,
                                               end_offset_x_px=13, end_offset_y_px=-7)
        shifted = am.TravelerRenderer(clean, pts, cum, [], shifted_cfg)
        for finish, dx, dy in ((False, -11, 9), (True, 13, -7)):
            before, after = clean.copy(), clean.copy()
            original.draw_flag(before, (160, 160), finish)
            shifted.draw_flag(after, (160, 160), finish)
            expected = np.roll(before, (dy, dx), axis=(0, 1))
            require(np.array_equal(after, expected), f"{kind} did not apply the independent X/Y endpoint offset")
    print("PASS offsets: every icon type translates independently at start and finish")


def verify_scene_schedules_and_old_modes():
    clean, track = load_demo_maps()
    redraw_cfg = config("redraw")
    redraw = am.Scene.prepare(redraw_cfg, clean, track)
    traveler = am.Scene.prepare(config("follow"), clean, track)
    reveal = am.Scene.prepare(config("reveal"), clean, track)
    hidden_cfg = config("follow")
    hidden_cfg["follow_settings"]["hide_waypoints"] = True
    hidden = am.Scene.prepare(hidden_cfg, clean, track)
    require(len(traveler.markers) >= 2, "demo map should have detected waypoint markers")
    require(traveler.schedule == redraw.schedule == reveal.schedule,
            "redraw, reveal, and traveler schedules should match for default timing")
    require(hidden.schedule == traveler.schedule, "hiding follow circles must not change the schedule")
    require(type(redraw.renderer) is am.RedrawRenderer, "redraw renderer changed unexpectedly")
    require(type(reveal.renderer) is am.RevealRenderer, "reveal renderer changed unexpectedly")
    for scene in (redraw, reveal):
        x, y, w, h = scene.crop
        expected = clean[y:y + h, x:x + w]
        require(np.array_equal(scene.frame(0), expected), "an old mode should keep its clean pre-delay frame")
        require(not np.array_equal(scene.frame(scene.n_frames - 1), expected),
                "an old mode should still render the route by the final frame")

    count = len(traveler.markers)
    custom = config("follow")
    custom["timing"]["custom"] = {
        "enabled": True,
        "legs": [0.2 + 0.05 * (i % 3) for i in range(count - 1)],
        "pauses": [0.1 + 0.03 * i for i in range(count - 2)],
    }
    custom_traveler = am.Scene.prepare(custom, clean, track)
    custom_redraw = config("redraw")
    custom_redraw["timing"]["custom"] = copy.deepcopy(custom["timing"]["custom"])
    custom_redraw_scene = am.Scene.prepare(custom_redraw, clean, track)
    require(custom_traveler.schedule == custom_redraw_scene.schedule,
            "custom traveler timings must match redraw timings")
    alias = am.Scene.prepare(config("traveler"), clean, track)
    require(alias.cfg["mode"] == "follow" and alias.schedule == traveler.schedule,
            "the legacy traveler mode should remain an alias for follow")
    print("PASS Scene: redraw/reveal behavior and follow/redraw schedule parity, including custom timing and alias")


def main():
    verify_renderer()
    verify_line_only_scene()
    verify_follow_waypoints()
    verify_follow_object_rotation_flips_and_offsets()
    verify_endpoint_icons()
    verify_emojis_and_pin()
    verify_platform_emoji_sources_and_defaults()
    verify_endpoint_offsets()
    verify_complete_emoji_catalog()
    verify_scene_schedules_and_old_modes()
    print("All follow checks passed.")


if __name__ == "__main__":
    main()
