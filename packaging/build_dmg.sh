#!/usr/bin/env bash
# Build a "Kimura GUI.app" and package it into a DMG (macOS only).
#
# Usage (from the repo root, ON A MAC):
#   bash packaging/build_dmg.sh
#
# Output: dist/Kimura-GUI.dmg
#
# NOTE: built and verified on a real macOS machine (Apple Silicon, brew
# Python 3.13 + Tcl/Tk 9). Two platform gotchas are baked in below: the
# tray icon must be created on the main thread (handled in kimura_tray.py),
# and a Python >= 3.12 is required — python.org 3.11.0-3.11.3 have a
# tkinter/GC thread-state bug that intermittently SIGABRTs the GUI.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [ "$(uname -s)" != "Darwin" ]; then
    echo "This script must be run on macOS." >&2
    exit 1
fi

if ! command -v brew >/dev/null 2>&1; then
    echo "Homebrew not found — install it from https://brew.sh first." >&2
    exit 1
fi
brew list hidapi >/dev/null 2>&1 || brew install hidapi

# Pick a Python >= 3.12 that has tkinter. Python 3.11.0-3.11.3 (python.org
# builds) ship a tkinter/GC thread-state bug (gh-84256 family) that makes
# this GUI intermittently SIGABRT during hid.enumerate() — don't use them.
PYTHON=""
for c in python3.13 python3.12 python3.14 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c "import tkinter" >/dev/null 2>&1; then
        PYTHON="$c"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    # brew Pythons lack tkinter until python-tk is installed.
    echo "No python3 with tkinter found — installing python-tk@3.13 via brew..."
    brew install python-tk@3.13 || true
    for c in python3.13 python3.12 python3.14 python3; do
        if command -v "$c" >/dev/null 2>&1 && "$c" -c "import tkinter" >/dev/null 2>&1; then
            PYTHON="$c"
            break
        fi
    done
fi
if [ -z "$PYTHON" ]; then
    echo "No python3 with tkinter found — try: brew install python-tk@3.13" >&2
    exit 1
fi
echo "Using $PYTHON ($( "$PYTHON" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' ))"

VENV=.dmg-build-venv
if [ -d "$VENV" ] && ! [ "$("$VENV/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null)" \
        = "$("$PYTHON" -c 'import sys; print("%d.%d" % sys.version_info[:2])')" ]; then
    echo "$VENV was built with a different Python — recreating it."
    rm -rf "$VENV"
fi
if [ ! -d "$VENV" ]; then
    "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet pyinstaller hidapi customtkinter Pillow pystray pyusb
# pystray's macOS tray backend needs PyObjC, which isn't always present on a
# fresh Homebrew Python — install it explicitly. If this fails or the tray
# icon doesn't show up at runtime, the app still runs fine without it
# (kimura_tray.py degrades gracefully).
"$VENV/bin/pip" install --quiet pyobjc || true

# App icon — derived from the real product photo (kimura_assets/mouse_top.webp),
# converted to .icns via macOS's native iconutil.
ICONSET=$(mktemp -d)/kimura-gui.iconset
mkdir -p "$ICONSET"
"$VENV/bin/python" - "$ICONSET" <<'PYEOF'
import sys
from PIL import Image
iconset = sys.argv[1]
im = Image.open("kimura_assets/mouse_top.webp").convert("RGBA")
for size in (16, 32, 128, 256, 512):
    im.resize((size, size), Image.LANCZOS).save(f"{iconset}/icon_{size}x{size}.png")
    im.resize((size * 2, size * 2), Image.LANCZOS).save(f"{iconset}/icon_{size}x{size}@2x.png")
PYEOF
iconutil -c icns "$ICONSET" -o packaging/kimura-gui.icns

rm -rf build dist "Kimura GUI.spec"
"$VENV/bin/python3" -m PyInstaller --windowed --name "Kimura GUI" \
    --collect-all hidapi \
    --collect-all customtkinter \
    --collect-all pystray \
    --collect-all usb \
    --collect-data kimura_assets --hidden-import kimura_assets --hidden-import PIL._tkinter_finder \
    --icon packaging/kimura-gui.icns \
    --osx-bundle-identifier com.kimura-driver.gui \
    kimura_gui.py

APP="dist/Kimura GUI.app"
if [ ! -d "$APP" ]; then
    echo "Build failed — no .app produced." >&2
    exit 1
fi

echo "Smoke-testing the bundled app..."
"$APP/Contents/MacOS/Kimura GUI" &
APP_PID=$!
sleep 2
if kill -0 "$APP_PID" 2>/dev/null; then
    if [ -t 0 ]; then
        echo "App is running — looks OK. Close its window, then press Enter to continue."
        read -r
    else
        echo "App is running — looks OK (non-interactive, killing it)."
        sleep 1
    fi
    kill "$APP_PID" 2>/dev/null || true
else
    echo "App exited immediately — something's wrong, most likely a missing or" >&2
    echo "unlinked hidapi dylib. Diagnose with:" >&2
    echo "  otool -L \"$APP/Contents/MacOS/Kimura GUI\"" >&2
    echo "and confirm it resolves libhidapi correctly (reinstall with" >&2
    echo "'brew reinstall hidapi' and rebuild if not)." >&2
    exit 1
fi

DMG_DIR=$(mktemp -d)
cp -R "$APP" "$DMG_DIR/"
ln -s /Applications "$DMG_DIR/Applications"

hdiutil create -volname "Kimura GUI" -srcfolder "$DMG_DIR" -ov -format UDZO dist/Kimura-GUI.dmg
rm -rf "$DMG_DIR"

echo ""
echo "Built: dist/Kimura-GUI.dmg"
echo "Note: this app is unsigned/unnotarized — first launch will need"
echo "right-click > Open (or System Settings > Privacy & Security > Open Anyway)"
echo "to bypass Gatekeeper, since there's no Apple Developer certificate involved."
