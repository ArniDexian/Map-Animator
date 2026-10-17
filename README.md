# Map Animator

**English** · [Русский](README.ru.md)

Turn two pictures of a map into a video: a blue track "travels" across the map from point A to the last stop, and round markers with the letters A, B, C… appear on the way.

![Clean map + the same map with a track → Map Animator → animated video](docs/assets/hero.gif)

* **Input:** a **clean map** and the **same map with a blue track and round stop markers** drawn on it (a screenshot with the route, for example).
* **Output:** an **MP4 video** (16:9 by default, near-lossless H.264/H.265, up to 4K).
* **Two ways to use it:** a macOS app with a window (drag & drop, settings, preview, built-in player), or a command-line tool for scripts.

> The demo maps in `examples/` are generated synthetically (`tools/make_demo.py`), so they are free of any third-party map data. Use your own maps at your own responsibility with respect to the map provider's terms.

---

## 1. Install

**Requirements:** macOS 13 or newer, internet access for the first install. Python 3.9+ is installed together with Apple's developer tools if you do not have it yet.

```bash
git clone <this repository>
cd <folder>
./install.command            # or just double-click install.command in Finder
```

The installer is a guided, step-by-step flow in Terminal:

1. checks Python 3.9+ (offers to install Apple's developer tools if it is missing),
2. creates the virtual environment `.venv`,
3. installs the Python packages from `requirements.txt` (OpenCV, SciPy, scikit-image, NetworkX, Pillow, ffmpeg, …),
4. runs a self-test (libraries and the video encoder),
5. builds `Map Animator.app` if Xcode / Command Line Tools are available.

**You can also just double-click `Map Animator.app`.** If Python or any package is missing, the app tells you what exactly is missing and asks *"Install now?"*. Press **Install**: Terminal opens with the same installer, and the app starts by itself when it is done.

Useful flags: `./install.command --check` (only check), `--yes` (no questions), `--no-open`.

## 2. Quick start (the app)

1. Open **`Map Animator.app`** (double-click).
2. Drag **both maps** onto the window — the clean one and the one with the track. You can drop both files at once (the file with `track` in its name is taken as the map with the track), or one at a time, or click a field to choose a file. *No maps at hand? Use `examples/demo_clean_map.jpg` and `examples/demo_track_map.jpg`.*
3. In a second the app checks the maps and tells you how many markers it found and how long the video will be.
4. Adjust the settings if you like (everything is remembered).
5. Press **Generate**. A progress bar with the remaining time runs at the bottom.
6. The finished video appears in the **Results** list: **▶ Watch** (built-in player) and **Show in Finder**. Files are stored in `output/` with a timestamp in the name, e.g. `animation_redraw_20261017_143332.mp4`; new files never overwrite old ones.

Closing the window quits the app. If a video is being generated at that moment, the app waits until it is finished and quits by itself (`⌘Q` asks for confirmation and cancels the generation).

### If the app does not start
* macOS blocks apps from reading *Downloads* until you allow it: press **Allow** in the prompt, or grant access in *System Settings → Privacy & Security → Full Disk Access*.
* Logs: `logs/ui_server.log` (server start-up) and `logs/animate_map_*.log` (every generation).
* The Finder icon did not refresh: run `killall Dock Finder` once.

---

## 3. The app: what is on the screen

### Maps
Two drop fields. After both maps are loaded they are **checked automatically**:
* **Swapped** (the track is drawn on the "clean" file) — the maps are **swapped back automatically** and you are told about it.
* **No track at all** (identical maps, or the track is not blue) — an error; *Generate* stays disabled.
* **The maps do not match** (different pictures, or one of them is shifted/zoomed) — an error with the percentage of differences. Away from the track the two maps must be identical.
* **Different sizes** — an error.

### General settings
| Field | Meaning |
|---|---|
| Mode | `redraw` (default) — the line and markers are drawn anew on the clean map; `reveal` — the original picture with the track is uncovered |
| FPS | 24, 25, 30, 48, 50, 60 |
| Resolution | "Same as the map" (the whole map, no cropping), "Original scale, 16:9 frame", or 4K / 1440p / 1080p / 720p / 480p (always 16:9) |
| Codec | H.264 (default, plays everywhere), H.265 (more compact, slower to encode), MPEG-4 (old) |
| Quality | Maximum (near lossless) / High / Balanced |
| Pauses | before the first marker; before the movement starts; at intermediate stops; at the end |
| Movement duration | the time the "point" needs to travel the whole track |

Below the fields the **total video length** is shown with its breakdown: `1 (before marker) + 1 (before start) + 4 (movement) + 3 × 1.2 (waypoint pauses) + 2 (end)`. Decimal numbers can be typed with a dot or a comma.

**16:9 frame.** The video is always 16:9: the biggest 16:9 area of the map, centred on the track, is cut out. If the track does not fit, the log says so and its edges are cut. To keep the whole map choose "Same as the map".

### `redraw` mode: line and markers
* A **preview** (a line with a marker and a letter) is shown at the top. If a map is loaded, its piece is used as the background, at true scale.
* **Styles** — saved sets of settings. The built-in **"Default"** (blue) cannot be deleted or overwritten. To save your own: set things up → *Save style* → name (a name is suggested). A duplicate style is not saved twice. Choosing a style applies all its values; *Delete style* removes your own styles.
* Fields: line colour and thickness (slider + number), marker colour and radius, **marker outline** (width, 0 = none; colour), letter colour, font (SF Pro — the macOS system font — and other system fonts), its weight and size, and the switch "Scale drawing to the image size".

> **Scale drawing to the image size.** The defaults are made for a picture whose markers have a radius of ~16 px. If your map has bigger markers, all sizes (line thickness, radius, outline, font) are multiplied by *real radius / given radius*. Switch it off to use the numbers literally.

### `reveal` mode: brushes
The original picture is uncovered by a brush moving along the path. Empty fields mean "automatic": the line brush equals the real line width plus a margin; a marker is uncovered as a whole, together with its white ring; the line comes right up to a marker and the marker appears exactly at that moment.

### Advanced: track and marker detection
The blue range (HSV), the minimal marker radius, pruning of short branches, path smoothing, the map-difference threshold. Change them only if your track is not blue.

### Results and the player
* The list of finished videos from `output/`. Each has **▶ Watch** and **Show in Finder**. **Clear** (visible only when there are files, with a confirmation) deletes all videos, debug images and logs.
* **Player:** play/pause (space or a click on the video), a smooth seek slider, speed 0.25×–2×, *Show in Finder*, *Open in external player*, and **Delete** (closes the player and deletes this video). Close with `Esc` or by clicking outside.

### Everything else
* **RU/EN** — interface language (follows the system language by default; your choice is remembered).
* **A− / A+** (or `⌘+`, `⌘−`, `⌘0`) — interface text size.
* **Documentation** — opens this description in a separate window (also in the menu: *Map Animator → Documentation*).
* **Reset settings** — back to the values from `config.json`.
* All settings, styles, the language and the text size are kept in `ui_settings.json` and restored on the next start.

---

## 4. The command-line tool

```bash
.venv/bin/python animate_map.py --config config.json            # normal run
.venv/bin/python animate_map.py --config config.json --debug    # + mask and path images into debug/
```

* Reads the maps from `inputs.clean_map` / `inputs.track_map`, writes the video to `output.video_path`, the log to `logging.file`. Relative paths are resolved from the folder of the config file.
* Shows a progress bar (in a terminal) or a "N%" line every 5% (when the output is redirected).
* Exit code: `0` success, `1` an expected error (missing file, maps do not match, no track…), `2` an unexpected one.

Try it on the demo maps: copy `config.json`, set `inputs.clean_map` / `track_map` to `examples/demo_clean_map.jpg` / `examples/demo_track_map.jpg` and run the command above.

### `config.json` reference

| Key | Default | Meaning |
|---|---|---|
| `inputs.clean_map`, `inputs.track_map` | `clean_map.jpg`, `track_map.jpg` | input files |
| `inputs.auto_swap` | `true` | swap the maps if the track is on the "clean" one (otherwise an error) |
| `output.video_path` | `output/output_animation.mp4` | where to write the video |
| `output.fps` | `30` | frames per second |
| `output.codec` | `avc1` | `avc1` (H.264), `hvc1` (H.265), `mp4v` (MPEG-4) |
| `output.quality` | `max` | `max` / `high` / `balanced` (CRF 8/13/19 for H.264; 10/15/21 for H.265) |
| `output.resolution` | `null` | `[width, height]` of the video; `null` — the native size of the frame |
| `output.aspect` | `[16, 9]` | aspect ratio of the frame; `null` — do not crop, take the whole map |
| `output.debug_dir` | `debug` | where to put intermediate images with `--debug` |
| `mode` | `redraw` | `redraw` or `reveal` |
| `timing.first_marker_delay_sec` | `1.0` | the clean map alone before marker A appears |
| `timing.initial_delay_sec` | `1.0` | marker A stands still before the movement starts |
| `timing.track_duration_sec` | `4.0` | time to travel the whole track |
| `timing.pause_on_waypoints` | `true` | stop at intermediate points |
| `timing.waypoint_pause_duration_sec` | `1.2` | length of a stop |
| `timing.final_delay_sec` | `2.0` | pause at the end |
| `circle_detection.min_circle_radius_px` | `8.5` | lower bound of the marker radius (the real threshold is also ≥ 1.7 × the half-width of the line) |
| `reveal_settings.line_brush_radius` | `null` | line brush radius; `null` — automatic |
| `reveal_settings.line_brush_margin_px` | `2` | brush margin over the real line width |
| `reveal_settings.waypoint_brush_radius` | `null` | marker disc radius; `null` — measured from the picture |
| `redraw_settings.line_color_bgr`, `.line_thickness` | `[255,122,0]`, `10` | colour (**BGR**!) and thickness of the line |
| `redraw_settings.marker_color_bgr`, `.marker_radius` | `[255,122,0]`, `16` | marker colour and radius |
| `redraw_settings.marker_outline_px`, `.marker_outline_color_bgr` | `2`, `[255,255,255]` | marker outline (0 — none) |
| `redraw_settings.marker_text_color_bgr` | `[255,255,255]` | letter colour |
| `redraw_settings.marker_font_path`, `.marker_font_index`, `.marker_font_variation`, `.marker_font_size_px` | SF Pro, `0`, `Bold`, `16` | system font for the letters; `marker_font_path: null` — OpenCV's built-in font (`marker_font_scale`, `marker_font_thickness`) |
| `redraw_settings.auto_scale_drawing` | `true` | scale drawing to the size of the markers on the map |
| `hsv_blue_range.lower/upper` | `[90,50,50]` / `[130,255,255]` | the "blue" range in HSV |
| `path_settings.spur_length_px` | `25` | shorter branches are pruned |
| `path_settings.smooth_sigma` | `1.2` | trajectory smoothing (Gaussian) |
| `path_settings.diff_threshold` | `15` | pixel-difference threshold of the two maps |
| `path_settings.max_mismatch_fraction` | `0.01` | allowed share of differences away from the track; more — "maps do not match" |
| `logging.file`, `logging.level` | `logs/animate_map.log`, `INFO` | log |

Colours in the config are **BGR** (OpenCV order); the app shows normal RGB and converts for you.

---

## 5. Project files

```
animate_map.py          command-line tool (the whole algorithm)
config.json             defaults (the app takes them from here)
install.command         guided installer (Python, venv, packages, self-test, app build)
ui.py                   local server of the interface (standard library + cv2/Pillow)
ui_static/index.html    the interface page (one file, no build step)
launcher/MapAnimatorApp.swift   native macOS shell (SwiftUI + WKWebView)
build_app.sh            rebuilds Map Animator.app
make_icon.py            draws the app icon
tools/make_demo.py      generates the demo maps and the animation at the top of this page
Map Animator.app        the ready app
examples/               demo maps (clean + with track)
docs/assets/            pictures used by this page
README.md, README.ru.md this file (also shown inside the app in the interface language)
requirements.txt        Python dependencies (ranges); requirements.lock.txt — the exact tested versions
-- created while working --
output/                 finished videos (only them)
debug/                  intermediate images (--debug)
logs/                   logs of generations and of the server
config_ui.json          config of the last generation from the interface
ui_settings.json        remembered settings, styles, language
.ui_uploads/            uploaded maps (older than 7 days are removed)
.venv/                  Python environment
```

---

# Technical details

## 6. Architecture

```
Map Animator.app (SwiftUI)
 ├─ preflight: .venv exists and has all packages? no → offers install.command in Terminal
 ├─ starts:   .venv/bin/python ui.py --no-window           (server on 127.0.0.1:8765)
 ├─ window = WKWebView → http://127.0.0.1:8765/            (ui_static/index.html)
 └─ "Documentation" window → http://127.0.0.1:8765/docs    (this README, in the UI language)

ui.py
 ├─ writes config_ui.json and starts:  animate_map.py --config config_ui.json   (subprocess)
 ├─ reads its output line by line → progress / ETA for the page
 └─ serves the page, the API, videos (with Range) and the documentation

animate_map.py  → output/*.mp4
```

The interface **knows nothing about the algorithm**: it only builds a config and starts the script, exactly as a person would in a terminal. The pre-check (`/api/analyze`) is the one place where `ui.py` imports `animate_map` and calls its functions directly (steps 0–3, ≈ 0.3 s). So the algorithm can be changed independently of the UI.

## 7. The algorithm (`animate_map.py`)

Steps are numbered as in the log (`[n/5]`).

0. **Input check** (`check_inputs`): counts "blue and different" pixels for both orders of the files — if the "clean" one has more, the maps are swapped. Then the differences away from the track (the track mask dilated by 41 px): more than `max_mismatch_fraction` is an error. Calibrated on real maps: a correct pair gives 0 %, even a 2-px shift gives ≈ 25 %.
1. **Track mask** (`extract_track_mask`): `absdiff` of the maps (> threshold) AND the HSV mask of blue; a morphological closing with a 15×15 ellipse fills the letters inside the markers; small components (< 20 % of the biggest) are dropped.
2. **1D path** (`extract_path`): skeletonisation → 8-connected graph → pruning of short branches → the largest component → the two points farthest from each other (double-sweep Dijkstra) → the shortest path; the start **A is the southernmost** end. The path is smoothed with a Gaussian; the cumulative length is computed. The ends are extended to the centres of the end markers.
3. **Markers** (`detect_markers`): distance transform of the mask; the radius threshold `max(min_circle_radius_px, 1.7 × median half-width of the line)` tells markers from bends of the line independently of the picture scale. Centres are projected onto the path and sorted by travelled distance → A, B, C… Then `trim_tails` cuts the skeleton "tails" beyond the end markers and puts the first and last path points exactly at their centres.
4. **Frame schedule** (`build_schedule`): a list `(distance along the path, number of visible markers)` per frame. A marker appears when the line reaches its **edge** (distance minus radius); a stop is made at the centre.
5. **Rendering** and encoding. Frames with the same state (pauses) are not recomputed.

### Render modes
* **`redraw`** — `polylines` along the smoothed path on a copy of the clean map (round ends) + markers: outline (`marker_outline_px`), disc, letter. The letter is drawn with Pillow in a system font (a mask of the real glyph box, so it is centred by its ink, not by font metrics), or with `cv2.putText` if `marker_font_path` is empty. SF Pro is the variable file `SFNS.ttf`; the weight is a named instance (`set_variation_by_name`).
* **`reveal`** — an accumulated brush mask; the result is an alpha blend of the clean and the marked maps. The line brush = the real line width + margin. Every marker is a disc of "blue disc + white ring" measured from the picture difference. Markers the line has not reached yet are **cut out of the mask**: the line comes up to a still hidden marker, then the marker appears whole.

### Framing and auto-scale
* `frame_crop` cuts the biggest area with `output.aspect`, centred on the bounding box of the track; then it is resized to `output.resolution`.
* `autoscale_drawing`: factor = median radius of the found markers / `marker_radius`; applied to line thickness, radius, outline and font (if it differs from 1 by more than 15 %).

### Encoding
The OpenCV writer in this build **ignores all quality settings** (and produces a low bitrate), so frames (raw BGR) are piped into **ffmpeg** (`imageio-ffmpeg`, a static build with libx264/libx265): `libx264 -crf 8…19 -preset slow`, `libx265 -crf 10…21`, `mpeg4 -q:v 1…5`; colours are tagged **BT.709**, `yuv420p`, `+faststart`. Measured: at 4K the difference from the ceiling of the format (4:2:0) is ≈ 0.4 dB. If ffmpeg is unavailable `cv2.VideoWriter` is used with a warning. Sizes are made even.

## 8. The interface server (`ui.py`)

A standard `ThreadingHTTPServer` on `127.0.0.1:8765` (`MAP_ANIMATOR_PORT` changes the port). A token in the HTML protects the API from foreign pages (`X-Token` or `?t=`).

| Route | Method | Purpose |
|---|---|---|
| `/`, `/docs`, `/docs/assets/*` | GET | the page; the documentation (README → HTML, language from `?lang=`, then the saved UI language) and its pictures |
| `/api/defaults` | GET | values from `config.json` + `DEFAULT_CONFIG` |
| `/api/settings` | GET/POST | remembered state (`ui_settings.json`) |
| `/api/fonts`, `/api/codecs` | GET | system fonts (incl. SF Pro), available codecs |
| `/api/upload?kind=` | POST | upload a map (body = the file, `X-Filename`) |
| `/api/analyze` | POST | pre-check: swapped? match? how many markers, marker radius |
| `/api/generate`, `/api/cancel` | POST | start / stop the subprocess |
| `/api/status?since=N` | GET | state, stage, %, ETA, new log lines |
| `/api/outputs`, `/api/video` | GET | list of videos, a video with Range support |
| `/api/reveal`, `/api/delete`, `/api/clear` | POST | Finder / open externally, delete one video, delete all generated files |
| `/api/busy` | GET | is a generation running (no token; for the native shell) |
| `/api/alive` | GET | a long-lived connection: when the window closes it breaks and the server exits in ≈ 5 s (after a running generation ends) |

Errors carry a machine-readable `code` + `args` (`sizes_differ`, `no_track`, `maps_mismatch`…); the page translates them into RU/EN, the English text is the fallback. On `SIGTERM` the server cancels the running generation and exits.

Environment variables for tests: `MAP_ANIMATOR_PORT`, `MAP_ANIMATOR_SETTINGS` (another settings file), so tests do not touch the running app or your settings.

## 9. The page (`ui_static/index.html`)

One file, plain JS, no build. State: `cfg` (a copy of `config.json` + service keys `__debug`, `__resPreset`), `styles`, `lang`, `uiScale`, saved to `/api/settings` with a 300 ms delay. Translations live in the `I18N` dictionary. Form fields are built from the tables `MAIN` / `REDRAW_*` / `REVEAL` / `DETECT`; adding a parameter = adding a row to a table + a key in `I18N`. The preview is a `<canvas>`; its scale depends only on the map (the detected marker radius) and shrinks when the marker or the line would not fit.

## 10. The macOS shell

`launcher/MapAnimatorApp.swift` is a SwiftUI app: it starts `ui.py`, shows a `WKWebView`, the documentation window and the menu. **Why not a shell script inside the `.app`:** macOS checks access to *Downloads* by the executable; for a script that is `/bin/bash`, so access cannot be granted. A native binary has its own identity. Build: `./build_app.sh` (`swiftc -parse-as-library`, icon `AppIcon.icns`, ad-hoc signature).

**First-run flow:** if `.venv/bin/python` is missing, or the server dies at start because packages are missing (checked with `importlib.util.find_spec`), the app shows "Setup is required", asks *Install now?* and opens `install.command` in Terminal; it re-checks every 2 s and starts by itself when the installer is done. Closing the window: if a generation is running the app stays and quits when it finishes; the `busy` state is cached in the background so that closing never hangs.

## 11. Testing and pitfalls

* Run the interface without the app: `MAP_ANIMATOR_PORT=8777 MAP_ANIMATOR_SETTINGS=/tmp/s.json .venv/bin/python ui.py --no-window` and open `http://127.0.0.1:8777/`.
* Regenerate the demo material: `.venv/bin/python tools/make_demo.py`.
* Quick quality check: compare a video frame with a rendered frame (PSNR); decode with `in_color_matrix=bt709`, otherwise OpenCV adds its own colour error.
* OpenCV `VideoWriter`: quality/bitrate parameters have no effect — use the ffmpeg path.
* Keep the contract "UI → `config_ui.json` → `animate_map.py`": every new parameter is added to `DEFAULT_CONFIG` and `config.json` first.
* Do not run destructive tests against the project folder: copy the project to a temporary folder (`ui.py` uses its own folder as the root).
* `output/`, `debug/`, `logs/`, `config_ui.json`, `ui_settings.json`, `.ui_uploads/` are not tracked by git.

## 12. Ideas not done yet

* A fast **preview before generation**: open the map, watch the animation without encoding a video (frames rendered on demand by the server from the same renderers), and press *Generate* only after that.
* A 4:4:4 mode for the crispest coloured lines (does not play in QuickTime/Safari).
* Choosing the 16:9 area by hand (now: centred on the track).
* Batch processing of several pairs of maps.

---

## 13. Prompt for an AI assistant

Paste the block below at the beginning of a conversation when you ask an AI to improve the project.

```text
You are helping to develop the project "Map Animator" (macOS). Read README.md in the project root: it describes
the functionality and the architecture. In short:

- animate_map.py — a command-line tool in Python (OpenCV, scikit-image, networkx, scipy, Pillow, ffmpeg via
  imageio-ffmpeg). From two maps (clean and with a blue track) it builds a 1D trajectory, finds the round markers
  A,B,C…, renders a video in two modes (redraw / reveal), crops it to 16:9 and encodes it through ffmpeg.
- ui.py + ui_static/index.html — a local web interface (standard http.server, one HTML file, no build step).
  The interface only builds config_ui.json and starts animate_map.py as a subprocess; it does not know the algorithm.
- launcher/MapAnimatorApp.swift — a native SwiftUI + WKWebView shell (build with ./build_app.sh).
- install.command — the guided installer (Python, .venv, packages, self-test, app build).

Rules:
1. A new parameter: add it to DEFAULT_CONFIG (animate_map.py) and config.json, then to the field tables and to I18N
   (ru and en) in index.html; ui.py must let the key through in the allowed sections. Colours in the config are BGR.
2. Do not change the contract UI -> config_ui.json -> animate_map.py. Script errors are AnimateError(message, code,
   **args); the page translates by code.
3. Video quality is set only through FfmpegWriter (OpenCV VideoWriter ignores the settings).
4. Verify changes by running: a console run on the demo maps in examples/ and a UI test on a separate port
   (MAP_ANIMATOR_PORT) with a separate settings file (MAP_ANIMATOR_SETTINGS) so that the running app and the
   user's settings are not touched. Never test destructive actions on the real project folder.
5. The app is started by double-clicking Map Animator.app (a native binary, for access to ~/Downloads).
   After Swift changes run ./build_app.sh.
6. Do not commit unless the user explicitly asks. Results (output/, debug/, logs/) are not in git.
7. The interface is bilingual (RU/EN): every visible string goes through the translation dictionary.
8. Documentation exists in two languages: README.md (English) and README.ru.md (Russian); keep them in sync.

Task: <describe what you need>
```
