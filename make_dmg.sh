#!/bin/bash
# Builds dist/Map-Animator-<version>.dmg : open it, drag "Map Animator" onto "Applications".
set -e
cd "$(dirname "$0")"
VERSION="$(cat VERSION)"
STAGE="$(mktemp -d)/Map Animator"
mkdir -p "$STAGE" dist
APP_OUT="$STAGE/Map Animator.app" NO_DEV_ENV=1 ./build_app.sh      # a clean copy for other people; the local app stays as it is
ln -s /Applications "$STAGE/Applications"
cat > "$STAGE/Read me first.txt" <<TXT
Map Animator $VERSION

1. Drag "Map Animator" onto "Applications".
2. The first time, right-click the app -> Open -> Open (it is not signed by Apple).
   If macOS still refuses, run this once in Terminal:
       xattr -dr com.apple.quarantine "/Applications/Map Animator.app"
3. On the first start the app installs the Python packages it needs (internet required, 1-3 minutes).
   It needs Python 3 from Apple's Command Line Tools; macOS offers to install them if they are missing.

Your videos, debug images and logs go to  ~/Documents/Map Animator
Requires macOS 13 or newer on an Apple Silicon Mac.

Map Animator $VERSION

1. Перетащите "Map Animator" на "Applications".
2. В первый раз откройте приложение правым кликом -> Открыть -> Открыть (оно не подписано Apple).
   Если macOS всё равно отказывает, один раз выполните в Terminal:
       xattr -dr com.apple.quarantine "/Applications/Map Animator.app"
3. При первом запуске приложение само установит нужные пакеты Python (нужен интернет, 1-3 минуты).
   Нужен Python 3 из «Инструментов командной строки» Apple; macOS предложит их поставить, если их нет.

Видео, debug-картинки и логи лежат в  ~/Documents/Map Animator
Нужна macOS 13 или новее на Mac с Apple Silicon.
TXT
OUT="dist/Map-Animator-$VERSION.dmg"
rm -f "$OUT"
hdiutil create -volname "Map Animator $VERSION" -srcfolder "$STAGE" -ov -format UDZO -fs HFS+ "$OUT" >/dev/null
rm -rf "$(dirname "$STAGE")"
echo "Built: $OUT ($(du -h "$OUT" | cut -f1))"
