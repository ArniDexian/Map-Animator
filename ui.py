#!/usr/bin/env python3
"""Local web UI for animate_map.py.

Starts a small HTTP server on 127.0.0.1, opens the page in a compact Chrome app window and
shuts itself down when that window is closed (after a running generation has finished).

The UI never touches the algorithm: for every run it writes `config_ui.json` and starts
    animate_map.py --config config_ui.json
exactly like you would from the console. Videos go to ./output with a timestamp in the name,
logs to ./logs.
"""
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import cv2
from PIL import ImageFont

import animate_map

ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(ROOT, "output")      # finished videos only
DEBUG_DIR = os.path.join(ROOT, "debug")        # mask / path images (--debug)
LOGS_DIR = os.path.join(ROOT, "logs")
UPLOAD_DIR = os.path.join(ROOT, ".ui_uploads")
CONFIG_UI = os.path.join(ROOT, "config_ui.json")
SETTINGS_FILE = os.path.join(ROOT, "ui_settings.json")   # remembered UI state (settings, styles, language)
CONFIG_DEFAULT = os.path.join(ROOT, "config.json")
INDEX_HTML = os.path.join(ROOT, "ui_static", "index.html")
HOST, PORT = "127.0.0.1", int(os.environ.get("MAP_ANIMATOR_PORT", 8765))   # env var: lets tests run beside the app
TOKEN = secrets.token_urlsafe(16)
MAX_UPLOAD = 300 * 1024 * 1024
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}

# top-level config sections the page is allowed to change
EDITABLE = ("mode", "timing", "circle_detection", "reveal_settings", "redraw_settings",
            "hsv_blue_range", "path_settings")

STAGE_RE = re.compile(r"\[(\d)/5\] (.*)")
RENDER_RE = re.compile(r"(\d+)%\s+\((\d+)/(\d+)\) elapsed (\d+)s, ETA (\d+)s")


def load_defaults():
    with open(CONFIG_DEFAULT, "r", encoding="utf-8") as f:
        return animate_map.deep_merge(animate_map.DEFAULT_CONFIG, json.load(f))


class UiError(ValueError):
    """Error shown to the user. `code`/`args` let the page translate it; the message is the English fallback."""

    def __init__(self, message, code=None, **args):
        super().__init__(message)
        self.code, self.args_dict = code, args


class State:
    """Everything the server shares between request threads."""

    def __init__(self):
        self.lock = threading.Lock()
        self.uploads = {}          # id -> (width, height)
        self.started_at = time.time()
        self.alive = 0              # open page connections (/api/alive)
        self.ever_alive = False
        self.bye_deadline = None    # set when the last page connection drops
        self.proc = None
        self.reset_job()

    def reset_job(self):
        self.job = {"state": "idle", "stage": 0, "stage_text": "", "percent": 0.0, "render_pct": None,
                    "elapsed": 0, "eta": None, "lines": [], "video": None, "error": None,
                    "started": None, "debug": False}


S = State()
SERVER = None
CODECS = []


# ----------------------------------------------------------------------------- job runner

def run_job(cmd, video_path, cancel_flag):
    job = S.job
    try:
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, env=dict(os.environ, PYTHONUNBUFFERED="1"))
    except OSError as e:
        with S.lock:
            job.update(state="error", error=f"Не удалось запустить скрипт: {e}")
        return
    S.proc = proc
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        with S.lock:
            job["lines"].append(line)
            m = STAGE_RE.search(line)
            if m:
                job["stage"], job["stage_text"] = int(m.group(1)), m.group(2)
                job["percent"] = float(job["stage"])  # 1..4 %
            m = RENDER_RE.search(line)
            if m:
                pct = int(m.group(1))
                job["render_pct"], job["eta"] = pct, int(m.group(5))
                job["percent"] = 5 + 0.95 * pct
            job["elapsed"] = time.time() - job["started"]
    proc.wait()
    with S.lock:
        job["elapsed"] = time.time() - job["started"]
        if cancel_flag.is_set():
            job.update(state="cancelled")
            if os.path.exists(video_path):
                os.remove(video_path)
        elif proc.returncode == 0 and os.path.exists(video_path):
            job.update(state="done", percent=100.0, eta=0, video=os.path.relpath(video_path, ROOT))
        else:
            err = next((l for l in reversed(job["lines"]) if "[ERROR]" in l or "Error" in l), None)
            job.update(state="error", error=err or f"Скрипт завершился с кодом {proc.returncode}")
    S.proc = None
    if job["state"] == "done":
        subprocess.Popen(["open", "-R", video_path])  # reveal the result in Finder


def start_generation(payload):
    with S.lock:
        if S.job["state"] == "running":
            raise UiError("A generation is already running", "running")
    clean_id, track_id = payload.get("clean"), payload.get("track")
    for uid in (clean_id, track_id):
        if uid not in S.uploads:
            raise UiError("Upload both maps first", "need_both")
    if S.uploads[clean_id] != S.uploads[track_id]:
        raise UiError("The maps have different sizes: %dx%d and %dx%d" % (*S.uploads[clean_id], *S.uploads[track_id]),
                      "sizes_differ", w1=S.uploads[clean_id][0], h1=S.uploads[clean_id][1],
                      w2=S.uploads[track_id][0], h2=S.uploads[track_id][1])

    cfg = load_defaults()
    user = payload.get("config", {})
    for key in EDITABLE:
        if key in user:
            cfg[key] = animate_map.deep_merge(cfg[key], user[key]) if isinstance(cfg[key], dict) else user[key]
    out = user.get("output", {})
    for key in ("fps", "codec", "resolution", "aspect", "quality"):
        if key in out:
            cfg["output"][key] = out[key]

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(DEBUG_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    video_path = os.path.join(OUTPUT_DIR, f"animation_{cfg['mode']}_{stamp}.mp4")
    cfg["inputs"] = {"clean_map": os.path.join(UPLOAD_DIR, clean_id),
                     "track_map": os.path.join(UPLOAD_DIR, track_id)}
    cfg["output"]["video_path"] = video_path
    cfg["output"]["debug_dir"] = DEBUG_DIR
    cfg["logging"]["file"] = os.path.join(LOGS_DIR, f"animate_map_{stamp}.log")
    with open(CONFIG_UI, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)

    cmd = [sys.executable, os.path.join(ROOT, "animate_map.py"), "--config", CONFIG_UI]
    if payload.get("debug"):
        cmd.append("--debug")
    cancel_flag = threading.Event()
    with S.lock:
        S.reset_job()
        S.job.update(state="running", started=time.time(), debug=bool(payload.get("debug")))
        S.cancel_flag = cancel_flag
    threading.Thread(target=run_job, args=(cmd, video_path, cancel_flag), daemon=True).start()


def cancel_generation():
    if S.proc is not None and S.job["state"] == "running":
        S.cancel_flag.set()
        S.proc.terminate()


# ----------------------------------------------------------------------------- analysis / fonts

def analyze(payload):
    """Quick pre-check of the two maps: swapped? no track? how many markers (for the duration)?"""
    ids = [payload.get("clean"), payload.get("track")]
    for uid in ids:
        if uid not in S.uploads:
            raise UiError("Upload both maps first", "need_both")
    clean = cv2.imread(os.path.join(UPLOAD_DIR, ids[0]), cv2.IMREAD_COLOR)
    track = cv2.imread(os.path.join(UPLOAD_DIR, ids[1]), cv2.IMREAD_COLOR)
    if clean.shape != track.shape:
        raise UiError("The maps have different sizes", "sizes_differ", w1=clean.shape[1], h1=clean.shape[0],
                      w2=track.shape[1], h2=track.shape[0])
    cfg = load_defaults()
    for key in ("hsv_blue_range", "circle_detection", "path_settings"):
        if key in payload.get("config", {}):
            cfg[key] = animate_map.deep_merge(cfg[key], payload["config"][key])
    try:
        clean, track, swapped, _ = animate_map.check_inputs(clean, track, cfg)
        mask = animate_map.extract_track_mask(clean, track, cfg)
        pts, cum, skel = animate_map.extract_path(mask, cfg)
        markers = animate_map.detect_markers(mask, pts, cum, cfg, skel)
    except animate_map.AnimateError as e:
        return {"error": str(e), "code": e.code, "args": e.args_dict}
    radii = sorted(m["radius"] for m in markers)
    return {"swapped": swapped, "markers": len(markers), "length_px": float(cum[-1]),
            "marker_radius": float(radii[len(radii) // 2]) if radii else None,
            "width": clean.shape[1], "height": clean.shape[0]}


WEIGHT_RANK = {"thin": 0, "ultralight": 0, "ultra light": 0, "extralight": 1, "light": 2, "book": 3, "roman": 3,
               "regular": 3, "normal": 3, "medium": 4, "semibold": 5, "demi bold": 5, "demibold": 5, "bold": 6,
               "extrabold": 7, "ultrabold": 7, "heavy": 8, "black": 9, "extrablack": 9}
FONT_FAMILIES = ["Helvetica Neue", "Helvetica", "Arial", "Avenir Next", "Avenir", "Futura", "Gill Sans",
                 "Optima", "Verdana", "Tahoma", "Trebuchet MS", "Georgia", "Palatino", "Times New Roman",
                 "Baskerville", "Didot", "American Typewriter", "Menlo", "Courier New", "Impact"]
_fonts_cache = []


def font_catalog():
    """Curated system fonts that exist on this Mac, with their upright weights (no italics)."""
    if _fonts_cache:
        return _fonts_cache
    found = {}
    for d in ("/System/Library/Fonts", "/System/Library/Fonts/Supplemental", "/Library/Fonts",
              os.path.expanduser("~/Library/Fonts")):
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.lower().endswith((".ttf", ".otf", ".ttc")):
                continue
            path = os.path.join(d, name)
            for idx in range(40):
                try:
                    fam, style = ImageFont.truetype(path, 20, index=idx).getname()
                except Exception:  # noqa: BLE001 - end of collection / unreadable face
                    break
                low = style.lower()
                if "italic" in low or "oblique" in low or "condensed" in low or "narrow" in low:
                    continue
                found.setdefault(fam, {}).setdefault(style, (path, idx))
    sf = "/System/Library/Fonts/SFNS.ttf"           # San Francisco (the macOS system font), a variable font
    if os.path.exists(sf):
        try:
            names = [n.decode() for n in ImageFont.truetype(sf, 20).get_variation_names()]
            weights = [n for n in ("Ultralight", "Thin", "Light", "Regular", "Medium", "Semibold", "Bold", "Heavy", "Black") if n in names]
            _fonts_cache.append({"family": "SF Pro", "styles": [{"name": n, "path": sf, "index": 0, "variation": n} for n in weights]})
        except Exception:  # noqa: BLE001 - FreeType without variable-font support: skip it
            pass
    for fam in FONT_FAMILIES:
        if fam in found:
            styles = sorted(found[fam].items(), key=lambda kv: (WEIGHT_RANK.get(kv[0].lower(), 3), kv[0]))
            _fonts_cache.append({"family": fam, "styles": [{"name": n, "path": p, "index": i}
                                                           for n, (p, i) in styles]})
    return _fonts_cache


def probe_codecs():
    """Codecs offered in the UI, default first. With the bundled ffmpeg all three are available."""
    try:
        import imageio_ffmpeg
        imageio_ffmpeg.get_ffmpeg_exe()
        return [{"value": "avc1"}, {"value": "hvc1"}, {"value": "mp4v"}]
    except Exception:  # noqa: BLE001 - no ffmpeg: only what OpenCV's own writer can do
        import tempfile
        ok, tmp = [], tempfile.mkdtemp()
        for code in ("avc1", "hvc1", "mp4v"):
            w = cv2.VideoWriter(os.path.join(tmp, code + ".mp4"), cv2.VideoWriter_fourcc(*code), 30, (64, 64))
            if w.isOpened():
                ok.append({"value": code})
            w.release()
        shutil.rmtree(tmp, ignore_errors=True)
        return ok or [{"value": "mp4v"}]


# ----------------------------------------------------------------------------- saved settings

def read_settings():
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_settings(data):
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, SETTINGS_FILE)


# ----------------------------------------------------------------------------- results list

def list_outputs():
    if not os.path.isdir(OUTPUT_DIR):
        return []
    items = []
    for name in os.listdir(OUTPUT_DIR):
        if name.lower().endswith(".mp4"):
            p = os.path.join(OUTPUT_DIR, name)
            st = os.stat(p)
            items.append({"name": name, "size": st.st_size, "mtime": st.st_mtime})
    return sorted(items, key=lambda i: -i["mtime"])[:30]


def reveal(name, open_file=False):
    p = os.path.join(OUTPUT_DIR, os.path.basename(name))  # basename: no path traversal
    if not os.path.exists(p):
        raise UiError("File not found", "file_missing")
    subprocess.Popen(["open", p] if open_file else ["open", "-R", p])


# ----------------------------------------------------------------------------- http handler

class Handler(BaseHTTPRequestHandler):
    server_version = "MapAnimatorUI"

    def log_message(self, *args):  # keep the console quiet
        pass

    # helpers
    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        q = parse_qs(urlparse(self.path).query)
        return self.headers.get("X-Token") == TOKEN or q.get("t", [None])[0] == TOKEN

    def read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            raise UiError("File is too large", "too_large")
        return self.rfile.read(n)

    # routes
    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            with open(INDEX_HTML, "r", encoding="utf-8") as f:
                html = f.read().replace("__TOKEN__", TOKEN)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/api/busy":
            return self.send_json({"busy": S.job["state"] == "running"})
        if not self.authorized():
            return self.send_json({"error": "forbidden"}, 403)
        if u.path == "/api/alive":
            return self.stream_alive()
        if u.path == "/api/defaults":
            return self.send_json(load_defaults())
        if u.path == "/api/status":
            since = int(parse_qs(u.query).get("since", ["0"])[0])
            with S.lock:
                j = dict(S.job)
                j["lines"] = j["lines"][since:]
                j["next"] = since + len(j["lines"])
                if j["state"] == "running":
                    j["elapsed"] = time.time() - j["started"]
            return self.send_json(j)
        if u.path == "/api/settings":
            return self.send_json(read_settings())
        if u.path == "/api/fonts":
            return self.send_json(font_catalog())
        if u.path == "/api/codecs":
            return self.send_json(CODECS)
        if u.path == "/api/outputs":
            return self.send_json(list_outputs())
        self.send_json({"error": "not found"}, 404)

    def stream_alive(self):
        """Held open by the page for as long as the window exists; its drop means 'window closed'."""
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with S.lock:
            S.alive += 1
            S.ever_alive = True
            S.bye_deadline = None
        try:
            while True:
                self.wfile.write(b".")
                self.wfile.flush()
                time.sleep(1)
        except OSError:  # BrokenPipe / ConnectionReset: the window is gone
            pass
        finally:
            with S.lock:
                S.alive -= 1
                if S.alive == 0:
                    S.bye_deadline = time.time() + 5  # grace period so a page reload does not stop us

    def do_POST(self):
        if not self.authorized():
            return self.send_json({"error": "forbidden"}, 403)
        u = urlparse(self.path)
        try:
            if u.path == "/api/upload":
                return self.send_json(self.handle_upload(u))
            body = self.read_body()
            data = json.loads(body) if body else {}
            if u.path == "/api/settings":
                write_settings(data)
            elif u.path == "/api/analyze":
                return self.send_json(analyze(data))
            elif u.path == "/api/generate":
                start_generation(data)
            elif u.path == "/api/cancel":
                cancel_generation()
            elif u.path == "/api/reveal":
                reveal(data.get("name", ""), data.get("open", False))
            else:
                return self.send_json({"error": "not found"}, 404)
            self.send_json({"ok": True})
        except UiError as e:
            self.send_json({"error": str(e), "code": e.code, "args": e.args_dict}, 400)
        except ValueError as e:
            self.send_json({"error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001 - report anything to the page
            self.send_json({"error": f"{type(e).__name__}: {e}"}, 500)

    def handle_upload(self, u):
        kind = parse_qs(u.query).get("kind", ["img"])[0]
        ext = os.path.splitext(self.headers.get("X-Filename", ""))[1].lower()
        if ext not in IMAGE_EXT:
            raise UiError("An image file is required (jpg, png, ...)", "not_image")
        data = self.read_body()
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        uid = f"{kind}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{ext}"
        path = os.path.join(UPLOAD_DIR, uid)
        with open(path, "wb") as f:
            f.write(data)
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            os.remove(path)
            raise UiError("The image could not be read", "read_fail")
        h, w = img.shape[:2]
        S.uploads[uid] = (w, h)
        return {"id": uid, "width": w, "height": h}


# ----------------------------------------------------------------------------- lifecycle

def watchdog():
    """Stop the server once the window is closed (and no generation is still running)."""
    while True:
        time.sleep(1)
        if S.job["state"] == "running":
            continue
        now = time.time()
        closed = S.alive == 0 and S.bye_deadline is not None and now > S.bye_deadline
        never_opened = not S.ever_alive and now - S.started_at > 90
        if closed or never_opened:
            SERVER.shutdown()
            return


def cleanup_old_uploads(days=7):
    if os.path.isdir(UPLOAD_DIR):
        for n in os.listdir(UPLOAD_DIR):
            p = os.path.join(UPLOAD_DIR, n)
            if time.time() - os.path.getmtime(p) > days * 86400:
                os.remove(p)


def open_window(url):
    """Only used when ui.py is started by hand; the app embeds the page itself (--no-window)."""
    webbrowser.open(url)


def on_terminate(signum, frame):
    """Quit requested (e.g. the app was closed): stop a running generation, then exit."""
    cancel_generation()
    proc = S.proc
    if proc is not None:
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
    threading.Thread(target=SERVER.shutdown, daemon=True).start()


def main():
    global SERVER
    url = f"http://{HOST}:{PORT}/"
    try:
        SERVER = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError:  # already running: just show its window again
        if "--no-window" not in sys.argv:
            open_window(url)
        return
    cleanup_old_uploads()
    global CODECS
    CODECS = probe_codecs()
    threading.Thread(target=font_catalog, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    if "--no-window" not in sys.argv:
        open_window(url)
    signal.signal(signal.SIGTERM, on_terminate)
    try:
        SERVER.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
