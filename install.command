#!/bin/bash
# Map Animator installer (macOS): checks Python, creates the virtual environment, installs the packages,
# verifies them and (if possible) builds the app.  Double-click it, or run  ./install.command
#
#   --yes       answer "yes" to every question (no prompts)
#   --check     only check, change nothing; exit code 0 = everything is installed
#   --no-open   do not start the app at the end
#   --no-wait   do not wait for Enter at the end
#   --python=PATH   use this Python (or set MAP_ANIMATOR_PYTHON); by default the best one found is used
#
# Установщик Map Animator: проверяет Python, создаёт окружение, ставит пакеты, проверяет и собирает приложение.

cd "$(dirname "$0")" || exit 1
HERE="$(pwd)"

# Two layouts: a source checkout (venv in ./.venv) or inside Map Animator.app (venv in Application Support).
case "$HERE" in
  */Contents/Resources/app) APP_MODE=1; VENV_DIR="${MAP_ANIMATOR_DATA:-$HOME/Library/Application Support/Map Animator}/venv";;
  *)                        APP_MODE=0; VENV_DIR="$HERE/.venv";;
esac
VPY="$VENV_DIR/bin/python"
ASSUME_YES=0; CHECK_ONLY=0; OPEN_APP=1; WAIT=1
FORCE_PY="${MAP_ANIMATOR_PYTHON:-}"
for a in "$@"; do case "$a" in --yes) ASSUME_YES=1; WAIT=0;; --check) CHECK_ONLY=1; WAIT=0;; --no-open) OPEN_APP=0;; --no-wait) WAIT=0;; --python=*) FORCE_PY="${a#--python=}";; esac; done

# ---- language: Russian if the system language is Russian
LANG_PREF="$(defaults read -g AppleLanguages 2>/dev/null | tr -d '[:space:]"(),' | head -c 2)"
case "$LANG_PREF$LANG" in ru*|*ru_RU*) RU=1;; *) RU=0;; esac
t() { if [ "$RU" = 1 ]; then echo "$1"; else echo "$2"; fi; }

if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; C=$'\033[36m'; N=$'\033[0m'; else B=; G=; Y=; R=; C=; N=; fi
step() { echo; echo "${B}${C}[$1/5]${N} ${B}$2${N}"; }
ok()   { echo "  ${G}✓${N} $1"; }
warn() { echo "  ${Y}!${N} $1"; }
fail() { echo "  ${R}✗${N} $1"; }
ask()  { # ask "question"  -> 0 yes / 1 no
  [ "$ASSUME_YES" = 1 ] && return 0
  local ans; read -r -p "  $1 [Y/n] " ans; case "$ans" in ""|y|Y|yes|Yes|д|Д|да|Да) return 0;; *) return 1;; esac; }
finish() { # finish exit_code
  echo
  if [ "$WAIT" = 1 ]; then read -r -p "$(t 'Нажмите Enter, чтобы закрыть окно…' 'Press Enter to close this window…')" _; fi
  exit "$1"; }

REQS=(cv2 numpy scipy skimage networkx tqdm PIL imageio_ffmpeg markdown)
missing_modules() { # prints the names of the packages that cannot be found (empty = all present)
  "$VPY" - "${REQS[@]}" <<'PY' 2>/dev/null
import importlib.util, sys
print(" ".join(m for m in sys.argv[1:] if importlib.util.find_spec(m) is None))
PY
}

echo "${B}Map Animator — $(t 'установка' 'installation')${N}"

# ------------------------------------------------------------------ 1. Python
step 1 "$(t 'Ищу Python 3' 'Looking for Python 3')"
# The installer never installs a Python of its own: it uses one that is already on this Mac.
# It looks in the usual places (Homebrew, python.org, pyenv, the system one), keeps those that are 3.9+ and
# really work (venv + pip available) and takes the newest one the packages are known to support (3.9 – 3.13).
# Force a specific one:  MAP_ANIMATOR_PYTHON=/path/to/python3 ./install.command    or    --python=/path/to/python3
CANDS=()
[ -n "$FORCE_PY" ] && CANDS+=("$FORCE_PY")
for v in 3.13 3.12 3.11 3.10 3.9; do
  CANDS+=("$(command -v python$v 2>/dev/null)" "/opt/homebrew/bin/python$v" "/usr/local/bin/python$v"
          "/Library/Frameworks/Python.framework/Versions/$v/bin/python3" "$HOME/.pyenv/versions/$v"*/bin/python3)
done
CANDS+=("$(command -v python3 2>/dev/null)" "$(command -v python 2>/dev/null)" /opt/homebrew/bin/python3 /usr/local/bin/python3
        /Library/Frameworks/Python.framework/Versions/Current/bin/python3 /usr/bin/python3)
if command -v pyenv >/dev/null 2>&1; then CANDS+=("$(pyenv which python3 2>/dev/null)"); fi

# The processor of THIS Mac (Apple Silicon or Intel); a Python of the other kind would run slowly under Rosetta
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ]; then NATIVE=arm64; else NATIVE=x86_64; fi
FOUND=""    # lines "major.minor|processor|path", one per distinct interpreter
for cand in "${CANDS[@]}"; do
  [ -n "$cand" ] && [ -x "$cand" ] || continue
  # a candidate counts only if it really runs (/usr/bin/python3 is a stub on a Mac without developer tools)
  ver="$("$cand" -c 'import sys, venv, ensurepip, platform
sys.exit(1) if sys.version_info[:2] < (3, 9) else print("%d.%d|%s" % (sys.version_info[0], sys.version_info[1], platform.machine()))' 2>/dev/null)" || continue
  real="$("$cand" -c 'import os, sys; print(os.path.realpath(sys.executable))' 2>/dev/null)" || continue
  case "$FOUND" in *"|$real"$'\n'*) continue;; esac
  FOUND="$FOUND$ver|$real"$'\n'
done

PY=""
if [ -n "$FORCE_PY" ] && ! printf '%s' "$FOUND" | grep -q "|$("$FORCE_PY" -c 'import os, sys; print(os.path.realpath(sys.executable))' 2>/dev/null)\$"; then
  fail "$(t 'Указанный Python не подходит (нужен 3.9+ с venv и pip):' 'The Python you specified is not usable (3.9+ with venv and pip is needed):') $FORCE_PY"
  finish 1
fi
if [ -n "$FOUND" ]; then
  # first the Pythons built for THIS Mac's processor; only if there is none, one for the other kind (slow, via Rosetta)
  POOL="$(printf '%s' "$FOUND" | awk -F'|' -v n="$NATIVE" '$2 == n')"
  WRONG_ARCH=0; [ -n "$POOL" ] || { POOL="$FOUND"; WRONG_ARCH=1; }
  # then the newest version up to 3.13 (newer ones may lack ready-made packages), else the newest there is
  PY="$(printf '%s' "$POOL" | awk -F'|' '$1 <= 3.13' | sort -t'|' -k1,1 -V -r | head -1 | cut -d'|' -f3)"
  [ -n "$PY" ] || PY="$(printf '%s' "$POOL" | sort -t'|' -k1,1 -V -r | head -1 | cut -d'|' -f3)"
fi
if [ -n "$PY" ]; then
  if [ "$(printf '%s' "$FOUND" | grep -c .)" -gt 1 ]; then
    echo "  $(t 'Найдено несколько Python:' 'Several Pythons were found:')"
    printf '%s' "$FOUND" | sort -t'|' -k1,1 -V -r | while IFS='|' read -r v arch pth; do echo "    $v  ($arch)  $pth"; done
  fi
  ok "$(t 'использую' 'using') $("$PY" --version 2>&1)  ($PY)   — $(t 'этот Mac:' 'this Mac:') $NATIVE"
  [ "$WRONG_ARCH" = 1 ] && warn "$(t 'Найден только Python для другого типа процессора: он будет работать через Rosetta, медленнее. Лучше поставить Python для вашего Mac (например, brew install python).' 'Only a Python for the other kind of processor was found: it runs through Rosetta, more slowly. Better install one for your Mac (e.g. brew install python).')"
  case "$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')" in
    3.9) warn "$(t 'Python 3.9 уже не получает обновлений: для библиотеки Pillow исправления безопасности есть только в версиях для Python 3.10+. Для работы приложения это не критично (оно не открывает чужие картинки через Pillow), но если поставите свежий Python (например, brew install python), установщик сам выберет его.' 'Python 3.9 no longer gets updates: the Pillow security fixes exist only for Python 3.10+. It does not matter much here (the app does not open untrusted images with Pillow), but if you install a newer Python (e.g. brew install python) the installer will pick it up.')";;
  esac
else
  fail "$(t 'Python 3.9 или новее не найден.' 'Python 3.9 or newer was not found.')"
  [ "$CHECK_ONLY" = 1 ] && exit 1
  echo "  $(t 'Нужно поставить инструменты разработчика Apple (в них есть Python 3) — это бесплатно, около 1–2 ГБ.' 'You need the Apple developer tools (they include Python 3) — free, about 1–2 GB.')"
  if ask "$(t 'Запустить установку сейчас?' 'Start the installation now?')"; then
    xcode-select --install 2>/dev/null
    echo "  $(t 'Откроется окно установки. Дождитесь его окончания, затем запустите этот установщик ещё раз.' 'An installer window will open. Wait until it finishes, then run this installer again.')"
    echo "  $(t 'Альтернатива: brew install python (если у вас есть Homebrew).' 'Alternative: brew install python (if you use Homebrew).')"
  else
    echo "  $(t 'Установите Python 3.9+ с python.org или через Homebrew и запустите установщик снова.' 'Install Python 3.9+ from python.org or Homebrew, then run this installer again.')"
  fi
  finish 1
fi

# ------------------------------------------------------------------ 2. virtual environment
step 2 "$(t 'Окружение Python' 'Python environment')"
echo "  $VENV_DIR"
if [ -x "$VPY" ] && "$VPY" -c 'import sys' >/dev/null 2>&1; then
  ok "$(t 'уже есть' 'already exists')"
else
  [ "$CHECK_ONLY" = 1 ] && { fail "$(t 'окружение не найдено' 'the environment was not found')"; exit 1; }
  [ -e "$VENV_DIR" ] && { warn "$(t 'старое окружение повреждено, создаю заново' 'the old environment is broken, recreating it')"; rm -rf "$VENV_DIR"; }
  mkdir -p "$(dirname "$VENV_DIR")"
  if "$PY" -m venv "$VENV_DIR"; then ok "$(t 'создано' 'created')"; else fail "$(t 'не удалось создать окружение' 'could not create the environment')"; finish 1; fi
fi

# ------------------------------------------------------------------ 3. packages
step 3 "$(t 'Пакеты Python' 'Python packages')"
MISSING="$(missing_modules)"
if [ -z "$MISSING" ]; then
  ok "$(t 'все пакеты на месте' 'all packages are present')"
else
  warn "$(t 'не хватает:' 'missing:') $MISSING"
  [ "$CHECK_ONLY" = 1 ] && exit 1
  echo "  $(t 'Скачиваю и ставлю пакеты (нужен интернет, 1–3 минуты)…' 'Downloading and installing the packages (internet needed, 1–3 minutes)…')"
  "$VPY" -m pip install --upgrade pip --quiet --disable-pip-version-check 2>&1 | sed 's/^/    /'
  if "$VPY" -m pip install -r requirements.txt --only-binary=:all: --disable-pip-version-check 2>&1 | grep -E "^(Collecting|Downloading|Successfully|ERROR)" | sed 's/^/    /'; then :; fi
  MISSING="$(missing_modules)"
  if [ -n "$MISSING" ]; then
    fail "$(t 'не удалось установить:' 'could not install:') $MISSING"
    echo "  $(t 'Проверьте интернет и запустите установщик ещё раз.' 'Check your internet connection and run the installer again.')"
    finish 1
  fi
  ok "$(t 'пакеты установлены' 'packages installed')"
fi

# ------------------------------------------------------------------ 4. self-test
step 4 "$(t 'Проверка' 'Self-test')"
if PYTHONPATH="$HERE" "$VPY" - <<'PY' 2>/dev/null
import cv2, numpy, scipy, skimage, networkx, PIL, imageio_ffmpeg
import subprocess
ff = imageio_ffmpeg.get_ffmpeg_exe()
assert subprocess.run([ff, "-version"], capture_output=True).returncode == 0
import animate_map
PY
then ok "$(t 'библиотеки и видеокодер работают' 'libraries and the video encoder work')"; else fail "$(t 'самопроверка не прошла (см. лог выше)' 'the self-test failed (see the log above)')"; finish 1; fi
[ "$CHECK_ONLY" = 1 ] && { echo; ok "$(t 'всё готово' 'everything is ready')"; exit 0; }

# ------------------------------------------------------------------ 5. app
step 5 "$(t 'Приложение Map Animator.app' 'The Map Animator.app application')"
if [ "$APP_MODE" = 1 ]; then
  ok "$(t 'вы запустили установку из приложения: вернитесь в Map Animator, оно продолжит само' 'this installer was started by the app: go back to Map Animator, it continues by itself')"
elif [ -x "Map Animator.app/Contents/MacOS/MapAnimator" ]; then
  ok "$(t 'приложение готово' 'the app is ready')"
elif command -v swiftc >/dev/null 2>&1; then
  echo "  $(t 'Собираю приложение (swiftc)…' 'Building the app (swiftc)…')"
  if ./build_app.sh >/dev/null 2>&1; then ok "$(t 'приложение собрано' 'the app was built')"; else warn "$(t 'не удалось собрать приложение; можно работать через ./"$VPY" ui.py' 'could not build the app; you can use ./"$VPY" ui.py')"; fi
else
  warn "$(t 'Для сборки приложения нужен Xcode / Command Line Tools. Без них работает консольная утилита и интерфейс: ./"$VPY" ui.py' 'Building the app needs Xcode / Command Line Tools. Without them the console tool and the UI still work: ./"$VPY" ui.py')"
fi

echo
if [ "$APP_MODE" = 1 ]; then
  echo "${B}${G}$(t 'Готово!' 'Done!')${N}  $(t 'Это окно можно закрыть: Map Animator запустится сам.' 'You can close this window: Map Animator starts by itself.')"
else
  echo "${B}${G}$(t 'Готово!' 'Done!')${N}  $(t 'Запускайте' 'Start') ${B}Map Animator.app${N}  $(t 'двойным кликом.' 'with a double click.')"
fi
if [ "$APP_MODE" = 0 ] && [ "$OPEN_APP" = 1 ] && [ -x "Map Animator.app/Contents/MacOS/MapAnimator" ] && [ "$ASSUME_YES" = 0 ]; then open "Map Animator.app"; fi
finish 0
