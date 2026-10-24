#!/bin/bash
set -euo pipefail

cd "$(dirname "$0")"

VERSION_FILE="VERSION"
PLIST_FILE="launcher/Info.plist"
PLIST_BUDDY="/usr/libexec/PlistBuddy"

if [[ ! -f "$VERSION_FILE" ]]; then
  echo "Error: VERSION file not found." >&2
  exit 1
fi
if [[ ! -f "$PLIST_FILE" ]]; then
  echo "Error: $PLIST_FILE not found." >&2
  exit 1
fi
if [[ ! -x "$PLIST_BUDDY" ]]; then
  echo "Error: PlistBuddy is not available. This script requires macOS." >&2
  exit 1
fi

current_version=$(<"$VERSION_FILE")
version_pattern='^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'
if [[ ! "$current_version" =~ $version_pattern ]]; then
  echo "Error: current version '$current_version' is not in MAJOR.MINOR.PATCH format." >&2
  exit 1
fi

major=${BASH_REMATCH[1]}
minor=${BASH_REMATCH[2]}
next_minor=$((minor + 1))
default_version="$major.$next_minor.0"

while true; do
  printf 'Current version: %s\nEnter new version (MAJOR.MINOR.PATCH) [%s]: ' "$current_version" "$default_version"
  IFS= read -r new_version || new_version=""
  if [[ -z "$new_version" ]]; then
    new_version="$default_version"
  fi
  if [[ "$new_version" =~ $version_pattern ]]; then
    break
  fi
  echo "Invalid version. Use three numeric parts, for example 1.2.3."
done

"$PLIST_BUDDY" -c "Set :CFBundleShortVersionString $new_version" "$PLIST_FILE"
"$PLIST_BUDDY" -c "Set :CFBundleVersion $new_version" "$PLIST_FILE"
printf '%s\n' "$new_version" > "$VERSION_FILE"

printf 'Version updated: %s -> %s\n' "$current_version" "$new_version"
