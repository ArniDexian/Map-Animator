#!/bin/bash
# Rebuilds "Map Animator.app": compiles the Swift launcher, bundles the code and resources, adds the icon, signs ad-hoc.
# Everything the app needs is copied INTO the bundle (Contents/Resources/app): code, page, config.json, docs, installer.
set -e
cd "$(dirname "$0")"
APP="${APP_OUT:-Map Animator.app}"   # APP_OUT: build somewhere else (make_dmg.sh); NO_DEV_ENV=1: do not record this machine's environment
VERSION="$(cat VERSION)"
RES="$APP/Contents/Resources/app"

rm -rf "$APP"                                   # always build from scratch: the bundle is a generated artefact
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -target arm64-apple-macosx13.0 -parse-as-library -O launcher/MapAnimatorApp.swift -o "$APP/Contents/MacOS/MapAnimator"
cp launcher/Info.plist "$APP/Contents/Info.plist"
[ -f launcher/AppIcon.icns ] || .venv/bin/python make_icon.py launcher/AppIcon.icns     # the icon is generated once and kept
cp launcher/AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

# --- bundle the program itself (read-only inside the app; user data lives outside, see README)
rm -rf "$RES" && mkdir -p "$RES/docs"
cp animate_map.py ui.py config.json requirements.txt install.command VERSION README.md README.ru.md "$RES/"
cp -R ui_static examples "$RES/"
cp -R docs/assets "$RES/docs/"
chmod +x "$RES/install.command"

# --- a local build remembers the .venv next to the sources, so the app on THIS Mac needs no second environment.
#     (a disk image for other people is built with NO_DEV_ENV=1: no personal paths inside)
if [ -z "$NO_DEV_ENV" ] && [ -x "$PWD/.venv/bin/python" ]; then
  /usr/libexec/PlistBuddy -c "Add :MapAnimatorDevEnvironment string $PWD/.venv/bin/python" "$APP/Contents/Info.plist" 2>/dev/null || true
fi

# --- version shown by macOS (About, Finder info)
PB=/usr/libexec/PlistBuddy
$PB -c "Set :CFBundleShortVersionString $VERSION" "$APP/Contents/Info.plist"
$PB -c "Set :CFBundleVersion $VERSION" "$APP/Contents/Info.plist"

# Ad-hoc signature with a *stable* designated requirement (by bundle identifier). A plain ad-hoc signature changes
# with every build, and macOS then asks again for access to ~/Documents, ... after each rebuild.
codesign --force --deep -s - -r='designated => identifier "local.mapanimator.launcher"' "$APP"
echo "Built: $APP  (version $VERSION)"
