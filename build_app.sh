#!/bin/bash
# Rebuilds "Map Animator.app": compiles the Swift launcher, adds the icon, signs ad-hoc.
set -e
cd "$(dirname "$0")"
APP="Map Animator.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -parse-as-library -O launcher/MapAnimatorApp.swift -o "$APP/Contents/MacOS/MapAnimator"
[ -f "$APP/Contents/Resources/AppIcon.icns" ] || .venv/bin/python make_icon.py "$APP/Contents/Resources/AppIcon.icns"
# Ad-hoc signature with a *stable* designated requirement (by bundle identifier). A plain ad-hoc signature changes
# with every build, and macOS then asks again for access to ~/Downloads, Documents, ... after each rebuild.
codesign --force --deep -s - -r='designated => identifier "local.mapanimator.launcher"' "$APP"
echo "Built: $APP"
