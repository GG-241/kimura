#!/usr/bin/env bash
# Build a portable AppImage for kimura-gui (Linux, x86_64).
#
# Usage (from the repo root):
#   bash packaging/build_appimage.sh
#
# Output: dist/Kimura-GUI-x86_64.AppImage
#
# Uses a throwaway venv so this doesn't touch your normal Python environment.
# Downloads appimagetool on first run (cached in packaging/ afterward).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

VENV=.appimage-build-venv
if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet pyinstaller hidapi customtkinter Pillow pystray pyusb

rm -rf build dist kimura-gui.spec packaging/AppDir
"$VENV/bin/python3" -m PyInstaller --onefile --windowed --name kimura-gui \
    --collect-all hidapi \
    --collect-all customtkinter \
    --collect-all pystray \
    --collect-all usb \
    --collect-data kimura_assets --hidden-import kimura_assets --hidden-import PIL._tkinter_finder \
    kimura_gui.py

mkdir -p packaging/AppDir/usr/bin
cp dist/kimura-gui packaging/AppDir/usr/bin/kimura-gui
chmod +x packaging/AppDir/usr/bin/kimura-gui

cat > packaging/AppDir/kimura-gui.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=Kimura GUI
Comment=Configure the Zeroground Kimura v3.0 mouse
Exec=kimura-gui
Icon=kimura-gui
Categories=Utility;Settings;
EOF

# App icon — derived from the real product photo (kimura_assets/mouse_top.webp).
python3 - <<'PYEOF'
from PIL import Image
im = Image.open("kimura_assets/mouse_top.webp").convert("RGBA")
im = im.resize((256, 256), Image.LANCZOS)
im.save("packaging/AppDir/kimura-gui.png")
PYEOF

cat > packaging/AppDir/AppRun <<'EOF'
#!/usr/bin/env bash
HERE="$(dirname "$(readlink -f "${0}")")"
exec "$HERE/usr/bin/kimura-gui" "$@"
EOF
chmod +x packaging/AppDir/AppRun

APPIMAGETOOL=packaging/appimagetool.AppImage
if [ ! -f "$APPIMAGETOOL" ]; then
    curl -sL -o "$APPIMAGETOOL" \
        https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x "$APPIMAGETOOL"
fi

ARCH=x86_64 "$APPIMAGETOOL" --appimage-extract-and-run packaging/AppDir dist/Kimura-GUI-x86_64.AppImage

echo ""
echo "Built: dist/Kimura-GUI-x86_64.AppImage"
