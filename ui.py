#!/usr/bin/env python3
"""Local web UI for animate_map.py.

Starts a small HTTP server on 127.0.0.1, opens the page in a compact Chrome app window and
shuts itself down when that window is closed (after a running generation has finished).

The UI never touches the algorithm: for every run it writes `config_ui.json` and starts
    animate_map.py --config config_ui.json
exactly like you would from the console. Videos go to ./output with a timestamp in the name,
logs to ./logs.
"""
import hashlib
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

# Three kinds of places:
#   APP_DIR   read-only resources (code, page, config.json, docs). The project folder, or Contents/Resources/app in the .app
#   DATA_DIR  internal data (settings, uploaded maps, config_ui.json). Project folder, or ~/Library/Application Support/Map Animator
#   HOME_DIR  what the user sees: videos, debug images, logs. ~/Documents/Map Animator in the .app
# The macOS app sets MAP_ANIMATOR_DATA / MAP_ANIMATOR_HOME; without them everything lives in the project folder (development).
APP_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = APP_DIR
DATA_DIR = os.environ.get("MAP_ANIMATOR_DATA") or APP_DIR
HOME_DIR = os.environ.get("MAP_ANIMATOR_HOME")
if HOME_DIR:
    OUTPUT_DIR = HOME_DIR                                  # finished videos (*.mp4) directly in the folder
    DEBUG_DIR = os.path.join(HOME_DIR, "debug")            # mask / path images (--debug)
    LOGS_DIR = os.path.join(HOME_DIR, "logs")
else:
    OUTPUT_DIR = os.path.join(APP_DIR, "output")
    DEBUG_DIR = os.path.join(APP_DIR, "debug")
    LOGS_DIR = os.path.join(APP_DIR, "logs")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads" if os.environ.get("MAP_ANIMATOR_DATA") else ".ui_uploads")
CONFIG_UI = os.path.join(DATA_DIR, "config_ui.json")
SETTINGS_FILE = os.environ.get("MAP_ANIMATOR_SETTINGS") or os.path.join(DATA_DIR, "ui_settings.json")
try:
    with open(os.path.join(APP_DIR, "VERSION"), "r", encoding="utf-8") as _f:
        VERSION = _f.read().strip() or "dev"
except OSError:
    VERSION = "dev"
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
        proc = subprocess.Popen(cmd, cwd=DATA_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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
            job.update(state="done", percent=100.0, eta=0, video=os.path.relpath(video_path, os.path.dirname(OUTPUT_DIR)))
        else:
            err = next((l for l in reversed(job["lines"]) if "[ERROR]" in l or "Error" in l), None)
            job.update(state="error", error=err or f"Скрипт завершился с кодом {proc.returncode}")
    S.proc = None


def checked_ids(payload):
    """The two uploaded maps named in a request; both must exist and have the same size."""
    clean_id, track_id = payload.get("clean"), payload.get("track")
    for uid in (clean_id, track_id):
        if uid not in S.uploads:
            raise UiError("Upload both maps first", "need_both")
    if S.uploads[clean_id] != S.uploads[track_id]:
        raise UiError("The maps have different sizes: %dx%d and %dx%d" % (*S.uploads[clean_id], *S.uploads[track_id]),
                      "sizes_differ", w1=S.uploads[clean_id][0], h1=S.uploads[clean_id][1],
                      w2=S.uploads[track_id][0], h2=S.uploads[track_id][1])
    return clean_id, track_id


def merged_config(payload):
    """config.json defaults + the settings the page is allowed to change (same rules for video and preview)."""
    cfg = load_defaults()
    user = payload.get("config", {})
    for key in EDITABLE:
        if key in user:
            cfg[key] = animate_map.deep_merge(cfg[key], user[key]) if isinstance(cfg[key], dict) else user[key]
    out = user.get("output", {})
    for key in ("fps", "codec", "resolution", "aspect", "quality"):
        if key in out:
            cfg["output"][key] = out[key]
    return cfg


def start_generation(payload):
    with S.lock:
        if S.job["state"] == "running":
            raise UiError("A generation is already running", "running")
    clean_id, track_id = checked_ids(payload)
    cfg = merged_config(payload)

    for d in (OUTPUT_DIR, DEBUG_DIR, LOGS_DIR, DATA_DIR):
        os.makedirs(d, exist_ok=True)
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


# ----------------------------------------------------------------------------- documentation

DOCS_CSS = """
:root { --bg:#fff; --text:#1c1e21; --muted:#6b7280; --line:#dfe2e7; --code:#f3f4f6; --accent:#007aff; }
@media (prefers-color-scheme: dark) { :root { --bg:#1b1c1e; --text:#ececec; --muted:#9aa0a6; --line:#3a3d41; --code:#26282b; --accent:#4da3ff; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:15px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
.wrap { display:flex; gap:32px; max-width:1200px; margin:0 auto; padding:24px 20px 80px; }
nav { flex:0 0 250px; position:sticky; top:16px; align-self:flex-start; max-height:calc(100vh - 32px); overflow:auto; font-size:13px; }
nav ul { list-style:none; margin:0; padding-left:14px; } nav > .toc > ul { padding-left:0; }
nav a { color:var(--muted); text-decoration:none; display:block; padding:2px 0; }
.langs { margin-bottom:12px; font-size:13px; } .langs a { display:inline; color:var(--accent); }
img { max-width:100%; height:auto; border-radius:10px; } p > img:only-child { display:block; margin:0 auto; } nav a:hover { color:var(--accent); }
main { min-width:0; flex:1; }
h1 { font-size:28px; margin:.2em 0 .5em; } h2 { font-size:22px; margin:1.8em 0 .5em; padding-top:.4em; border-top:1px solid var(--line); }
h3 { font-size:17px; margin:1.4em 0 .4em; }
a { color:var(--accent); }
code { background:var(--code); padding:.1em .35em; border-radius:5px; font:13px ui-monospace, Menlo, monospace; }
pre { background:var(--code); padding:12px 14px; border-radius:10px; overflow:auto; } pre code { background:none; padding:0; }
table { border-collapse:collapse; width:100%; margin:1em 0; font-size:14px; display:block; overflow-x:auto; }
th, td { border:1px solid var(--line); padding:6px 10px; text-align:left; vertical-align:top; } th { background:var(--code); }
blockquote { margin:1em 0; padding:.2em 1em; border-left:4px solid var(--accent); background:var(--code); border-radius:0 8px 8px 0; }
hr { border:0; border-top:1px solid var(--line); margin:2em 0; }
@media (max-width: 800px) { .wrap { display:block; } nav { position:static; max-height:none; margin-bottom:20px; } }
"""


DOCS_FILES = {"ru": "README.ru.md", "en": "README.md"}
DOCS_TITLES = {"ru": "Map Animator — документация", "en": "Map Animator — documentation"}
ASSET_TYPES = {".gif": "image/gif", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".mp4": "video/mp4"}


def docs_language(query, accept_language=""):
    """?lang= wins, then the language chosen in the UI (saved settings), then the browser/system language."""
    lang = parse_qs(query).get("lang", [None])[0]
    if lang not in DOCS_FILES:
        lang = read_settings().get("lang")
    if lang not in DOCS_FILES:
        lang = "ru" if accept_language.lower().startswith("ru") else "en"
    return lang


def render_docs(lang="en"):
    """README (in the UI language) rendered as a page; one source of truth for GitHub and the app."""
    try:
        import markdown
        with open(os.path.join(ROOT, DOCS_FILES[lang]), "r", encoding="utf-8") as f:
            text = f.read()
        md = markdown.Markdown(extensions=["tables", "fenced_code", "toc", "sane_lists", "md_in_html"],
                               extension_configs={"toc": {"toc_depth": "2-3", "permalink": False}})
        body = md.convert(text)
        body = body.replace('href="README.ru.md"', 'href="/docs?lang=ru"').replace('href="README.md"', 'href="/docs?lang=en"')
        switch = " · ".join(f'<b>{l.upper()}</b>' if l == lang else f'<a href="/docs?lang={l}">{l.upper()}</a>' for l in DOCS_FILES)
        return (f'<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><title>{DOCS_TITLES[lang]}</title>'
                f'<meta name="viewport" content="width=device-width, initial-scale=1"><style>{DOCS_CSS}</style></head>'
                f'<body><div class="wrap"><nav><div class="langs">{switch}</div>{md.toc}</nav><main>{body}</main></div></body></html>')
    except Exception as e:  # noqa: BLE001 - never break the app because of the docs
        return f"<!doctype html><meta charset='utf-8'><body style='font:15px sans-serif;padding:24px'>Documentation is unavailable: {e}</body>"


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


# ----------------------------------------------------------------------------- preview (frames on demand)

PREVIEWS = {}                 # scene id -> {"scene": Scene, "used": timestamp}; the 2 most recent are kept
PREVIEW_LOCK = threading.Lock()
PREVIEW_KEEP = 2


def preview_open(payload):
    """Prepare (or reuse) a scene for the current maps + settings; the page then asks for frames by number."""
    clean_id, track_id = checked_ids(payload)
    cfg = merged_config(payload)
    cfg["inputs"] = {"clean_map": os.path.join(UPLOAD_DIR, clean_id), "track_map": os.path.join(UPLOAD_DIR, track_id)}
    key = hashlib.md5(json.dumps([cfg, clean_id, track_id], sort_keys=True).encode()).hexdigest()[:16]
    with PREVIEW_LOCK:
        entry = PREVIEWS.get(key)
    if entry is None:
        clean = cv2.imread(cfg["inputs"]["clean_map"], cv2.IMREAD_COLOR)
        track = cv2.imread(cfg["inputs"]["track_map"], cv2.IMREAD_COLOR)
        try:
            scene = animate_map.Scene.prepare(cfg, clean, track)
        except animate_map.AnimateError as e:
            raise UiError(str(e), e.code, **e.args_dict)
        entry = {"scene": scene}
        with PREVIEW_LOCK:
            PREVIEWS[key] = entry
            while len(PREVIEWS) > PREVIEW_KEEP:             # drop the least recently used scene
                oldest = min((k for k in PREVIEWS if k != key), key=lambda k: PREVIEWS[k].get("used", 0))
                del PREVIEWS[oldest]
    entry["used"] = time.time()
    sc = entry["scene"]
    cw, ch = sc.native_size
    return {"id": key, "frames": sc.n_frames, "fps": sc.fps, "duration": sc.n_frames / sc.fps,
            "native": [cw, ch], "markers": len(sc.markers), "mode": sc.cfg["mode"]}


def preview_frame_jpeg(key, index, width):
    with PREVIEW_LOCK:
        entry = PREVIEWS.get(key)
    if entry is None:
        raise UiError("The preview expired: open it again", "preview_gone")
    entry["used"] = time.time()
    sc = entry["scene"]
    cw, ch = sc.native_size
    width = min(max(int(width), 160), cw)
    img = sc.render(index, (width, max(2, int(round(width * ch / cw)))))
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return buf.tobytes()


# ----------------------------------------------------------------------------- results list

_fps_cache = {}


def video_fps(path, mtime):
    """Frame rate of a video (cached), used by the player for frame stepping."""
    key = (path, mtime)
    if key not in _fps_cache:
        cap = cv2.VideoCapture(path)
        fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
        cap.release()
        _fps_cache[key] = round(float(fps), 3)
    return _fps_cache[key]


def list_outputs():
    if not os.path.isdir(OUTPUT_DIR):
        return []
    items = []
    for name in os.listdir(OUTPUT_DIR):
        if name.lower().endswith(".mp4"):
            p = os.path.join(OUTPUT_DIR, name)
            st = os.stat(p)
            items.append({"name": name, "size": st.st_size, "mtime": st.st_mtime, "fps": video_fps(p, st.st_mtime)})
    return sorted(items, key=lambda i: -i["mtime"])[:30]


def clear_outputs():
    """Delete every generated file: videos (output/), debug images (debug/) and logs (logs/)."""
    if S.job["state"] == "running":
        raise UiError("A generation is running", "running")
    count = freed = 0
    for folder in (OUTPUT_DIR, DEBUG_DIR, LOGS_DIR):
        if not os.path.isdir(folder):
            continue
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if not os.path.isfile(path):
                continue
            if folder == OUTPUT_DIR and not name.lower().endswith(".mp4"):   # never touch other files of the user
                continue
            if name == "ui_server.log":          # held open by this very server: empty it instead
                open(path, "w").close()
                continue
            freed += os.path.getsize(path)
            os.remove(path)
            count += 1
    return {"files": count, "bytes": freed}


def delete_video(name):
    """Delete one finished video from output/ (used by the player's Delete button)."""
    name = os.path.basename(name)
    path = os.path.join(OUTPUT_DIR, name)
    if not name.lower().endswith(".mp4") or not os.path.isfile(path):
        raise UiError("File not found", "file_missing")
    os.remove(path)


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
        if u.path.startswith("/docs/assets/"):      # pictures used by the README
            name = os.path.basename(u.path)
            path = os.path.join(ROOT, "docs", "assets", name)
            ctype = ASSET_TYPES.get(os.path.splitext(name)[1].lower())
            if not ctype or not os.path.isfile(path):
                return self.send_json({"error": "not found"}, 404)
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if u.path == "/docs":                       # documentation = README rendered as a page, in the UI language
            body = render_docs(docs_language(u.query, self.headers.get("Accept-Language", ""))).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/":
            with open(INDEX_HTML, "r", encoding="utf-8") as f:
                html = f.read().replace("__TOKEN__", TOKEN).replace("__VERSION__", VERSION)
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
        if u.path == "/api/video":
            return self.send_video(u)
        if u.path == "/api/preview/frame":
            q = parse_qs(u.query)
            try:
                data = preview_frame_jpeg(q.get("id", [""])[0], int(q.get("i", ["0"])[0]), int(q.get("w", ["960"])[0]))
            except UiError as e:
                return self.send_json({"error": str(e), "code": e.code, "args": e.args_dict}, 410)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "max-age=600")
            self.end_headers()
            self.wfile.write(data)
            return
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

    def send_video(self, u):
        """Serve a finished video with HTTP Range support (needed for seeking in <video>)."""
        name = os.path.basename(parse_qs(u.query).get("name", [""])[0])
        path = os.path.join(OUTPUT_DIR, name)
        if not name.lower().endswith(".mp4") or not os.path.isfile(path):
            return self.send_json({"error": "not found"}, 404)
        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            a, _, b = rng[6:].partition("-")
            try:
                if a == "":
                    start = max(0, size - int(b))
                else:
                    start, end = int(a), (int(b) if b else size - 1)
                end, code = min(end, size - 1), 206
            except ValueError:
                start, end, code = 0, size - 1, 200
        length = max(0, end - start + 1)
        self.send_response(code)
        self.send_header("Content-Type", "video/mp4")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(1 << 20, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except OSError:  # the player closed the connection (seeking): nothing to do
            pass

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
            elif u.path == "/api/preview":
                return self.send_json(preview_open(data))
            elif u.path == "/api/generate":
                start_generation(data)
            elif u.path == "/api/delete":
                delete_video(data.get("name", ""))
            elif u.path == "/api/clear":
                return self.send_json(clear_outputs())
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
