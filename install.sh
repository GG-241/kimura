#!/usr/bin/env bash
# Installer for the unofficial Kimura v3.0 (MS-4300WG) driver.
# Linux and macOS. Installs the `kimura`/`kimura-gui` commands via pip, sets
# up desktop integration (Linux app-menu entry, macOS .app bundle), and on
# Linux also sets up the udev rule needed for USB device access.
set -euo pipefail

OS="$(uname -s)"

echo "Installing kimura-driver..."
if command -v pip3 >/dev/null 2>&1; then
    PIP=pip3
else
    PIP=pip
fi

check_tkinter() {
    if ! python3 -c "import tkinter" >/dev/null 2>&1; then
        echo ""
        echo "NOTE: kimura-gui needs tkinter, which isn't installed for your Python."
        if [ "$OS" = "Linux" ]; then
            echo "  Debian/Ubuntu: sudo apt install python3-tk"
            echo "  Fedora:        sudo dnf install python3-tkinter"
            echo "  Arch:          sudo pacman -S tk"
        else
            echo "  Install Python from python.org, or: brew install python-tk"
        fi
        echo "The kimura CLI will still work fine without it."
    fi
}

check_tray() {
    # The system tray icon (kimura_tray.py) degrades gracefully without
    # this — the GUI works fine either way — but on Ubuntu/GNOME the tray
    # icon needs these GObject bindings to actually appear.
    if [ "$OS" = "Linux" ] && command -v apt >/dev/null 2>&1; then
        if ! python3 -c "import gi; gi.require_version('AyatanaAppIndicator3', '0.1')" >/dev/null 2>&1; then
            echo ""
            echo "NOTE: for the system tray icon to appear (optional — the GUI works"
            echo "fine without it), install:"
            echo "  sudo apt install gir1.2-ayatanaappindicator3-0.1"
        fi
    fi
}

if [ "$OS" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then
        brew list hidapi >/dev/null 2>&1 || brew install hidapi
    else
        echo "Homebrew not found — install it from https://brew.sh, then run:"
        echo "  brew install hidapi"
        echo "before using this tool."
    fi
    "$PIP" install --user .
    check_tkinter

    APP_DIR="$HOME/Applications/Kimura GUI.app/Contents/MacOS"
    mkdir -p "$APP_DIR"
    KIMURA_GUI_BIN="$(python3 -c 'import sysconfig; print(sysconfig.get_path("scripts", "posix_user"))')/kimura-gui"
    cat > "$APP_DIR/../Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key><string>Kimura GUI</string>
    <key>CFBundleExecutable</key><string>kimura-gui-launcher</string>
    <key>CFBundleIdentifier</key><string>com.kimura-driver.gui</string>
    <key>CFBundlePackageType</key><string>APPL</string>
</dict>
</plist>
PLIST
    cat > "$APP_DIR/kimura-gui-launcher" <<LAUNCHER
#!/usr/bin/env bash
exec "$KIMURA_GUI_BIN"
LAUNCHER
    chmod +x "$APP_DIR/kimura-gui-launcher"
    echo "Installed \"Kimura GUI.app\" to ~/Applications — open it from Finder/Spotlight."

elif [ "$OS" = "Linux" ]; then
    "$PIP" install --user . --break-system-packages 2>/dev/null || "$PIP" install --user .
    check_tkinter
    check_tray

    DESKTOP_DIR="$HOME/.local/share/applications"
    mkdir -p "$DESKTOP_DIR"
    KIMURA_GUI_BIN="$(python3 -c 'import sysconfig; print(sysconfig.get_path("scripts", "posix_user"))')/kimura-gui"
    cat > "$DESKTOP_DIR/kimura-gui.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Kimura GUI
Comment=Configure the Zeroground Kimura v3.0 mouse
Exec=$KIMURA_GUI_BIN
Terminal=false
Categories=Utility;Settings;
DESKTOP
    echo "Installed a Kimura GUI entry to your application menu."

    RULES_FILE="/etc/udev/rules.d/71-kimura-usb.rules"
    if [ ! -f "$RULES_FILE" ]; then
        echo ""
        echo "One more step on Linux — a udev rule is needed for USB access."
        echo "Run the following (requires sudo):"
        echo ""
        echo "  sudo tee $RULES_FILE > /dev/null <<'EOF'"
        echo "SUBSYSTEM==\"usb\", ATTRS{idVendor}==\"248a\", ATTRS{idProduct}==\"5b49\", MODE=\"0660\", TAG+=\"uaccess\""
        echo "SUBSYSTEM==\"usb\", ATTRS{idVendor}==\"248a\", ATTRS{idProduct}==\"5b4a\", MODE=\"0660\", TAG+=\"uaccess\""
        echo "EOF"
        echo "  sudo udevadm control --reload && sudo udevadm trigger"
        echo ""
        echo "Then unplug and replug the mouse."
    fi
else
    echo "Unsupported OS: $OS. This tool supports Linux and macOS only."
    exit 1
fi

echo ""
echo "Done. Try:  kimura list"
echo "Or launch the GUI:  kimura-gui"
