#!/bin/bash
# Map Animator installer (macOS): checks Python, creates the virtual environment, installs the packages,
# verifies them and (if possible) builds the app.  Double-click it, or run  ./install.command
#
#   --yes       answer "yes" to every question (no prompts)
#   --check     only check, change nothing; exit code 0 = everything is installed
#   --no-open   do not start the app at the end
#   --no-wait   do not wait for Enter at the end
#
# Установщик Map Animator: проверяет Python, создаёт окружение, ставит пакеты, проверяет и собирает приложение.

cd "$(dirname "$0")" || exit 1
ASSUME_YES=0; CHECK_ONLY=0; OPEN_APP=1; WAIT=1
for a in "$@"; do case "$a" in --yes) ASSUME_YES=1; WAIT=0;; --check) CHECK_ONLY=1; WAIT=0;; --no-open) OPEN_APP=0;; --no-wait) WAIT=0;; esac; done

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
  .venv/bin/python - "${REQS[@]}" <<'PY' 2>/dev/null
import importlib.util, sys
print(" ".join(m for m in sys.argv[1:] if importlib.util.find_spec(m) is None))
PY
}

echo "${B}Map Animator — $(t 'установка' 'installation')${N}"

# ------------------------------------------------------------------ 1. Python
step 1 "$(t 'Проверяю Python 3' 'Checking Python 3')"
PY=""
for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 2>/dev/null)" /usr/bin/python3; do
  [ -n "$cand" ] && [ -x "$cand" ] || continue
  # /usr/bin/python3 is a stub on a Mac without developer tools: it must really run
  if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1; then PY="$cand"; break; fi
done
if [ -n "$PY" ]; then
  ok "$("$PY" --version 2>&1)  ($PY)"
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
step 2 "$(t 'Окружение .venv' 'Virtual environment .venv')"
if [ -x .venv/bin/python ] && .venv/bin/python -c 'import sys' >/dev/null 2>&1; then
  ok "$(t 'уже есть' 'already exists')"
else
  [ "$CHECK_ONLY" = 1 ] && { fail "$(t 'окружение .venv не найдено' '.venv not found')"; exit 1; }
  [ -e .venv ] && { warn "$(t 'старое окружение повреждено, создаю заново' 'the old environment is broken, recreating it')"; rm -rf .venv; }
  if "$PY" -m venv .venv; then ok "$(t 'создано' 'created')"; else fail "$(t 'не удалось создать окружение' 'could not create the environment')"; finish 1; fi
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
  .venv/bin/python -m pip install --upgrade pip --quiet --disable-pip-version-check 2>&1 | sed 's/^/    /'
  if .venv/bin/python -m pip install -r requirements.txt --disable-pip-version-check 2>&1 | grep -E "^(Collecting|Downloading|Successfully|ERROR)" | sed 's/^/    /'; then :; fi
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
if .venv/bin/python - <<'PY' 2>/dev/null
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
if [ -x "Map Animator.app/Contents/MacOS/MapAnimator" ]; then
  ok "$(t 'приложение готово' 'the app is ready')"
elif command -v swiftc >/dev/null 2>&1; then
  echo "  $(t 'Собираю приложение (swiftc)…' 'Building the app (swiftc)…')"
  if ./build_app.sh >/dev/null 2>&1; then ok "$(t 'приложение собрано' 'the app was built')"; else warn "$(t 'не удалось собрать приложение; можно работать через ./.venv/bin/python ui.py' 'could not build the app; you can use ./.venv/bin/python ui.py')"; fi
else
  warn "$(t 'Для сборки приложения нужен Xcode / Command Line Tools. Без них работает консольная утилита и интерфейс: ./.venv/bin/python ui.py' 'Building the app needs Xcode / Command Line Tools. Without them the console tool and the UI still work: ./.venv/bin/python ui.py')"
fi

echo
echo "${B}${G}$(t 'Готово!' 'Done!')${N}  $(t 'Запускайте' 'Start') ${B}Map Animator.app${N}  $(t 'двойным кликом.' 'with a double click.')"
if [ "$OPEN_APP" = 1 ] && [ -x "Map Animator.app/Contents/MacOS/MapAnimator" ] && [ "$ASSUME_YES" = 0 ]; then open "Map Animator.app"; fi
finish 0
