#!/bin/bash
# Rebuilds "Map Animator.app": compiles the Swift launcher, adds the icon, signs ad-hoc.
set -e
cd "$(dirname "$0")"
APP="Map Animator.app"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
swiftc -parse-as-library -O launcher/MapAnimatorApp.swift -o "$APP/Contents/MacOS/MapAnimator"
[ -f "$APP/Contents/Resources/AppIcon.icns" ] || .venv/bin/python make_icon.py "$APP/Contents/Resources/AppIcon.icns"
codesign --force --deep -s - "$APP"
echo "Built: $APP"
