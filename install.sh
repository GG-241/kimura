#!/usr/bin/env bash
# Installer for the unofficial Kimura v3.0 (MS-4300WG) driver.
# Linux and macOS. Installs the `kimura` command via pip, and on Linux also
# sets up the udev rule needed for USB device access.
set -euo pipefail

OS="$(uname -s)"

echo "Installing kimura-driver..."
if command -v pip3 >/dev/null 2>&1; then
    PIP=pip3
else
    PIP=pip
fi

if [ "$OS" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then
        brew list hidapi >/dev/null 2>&1 || brew install hidapi
    else
        echo "Homebrew not found — install it from https://brew.sh, then run:"
        echo "  brew install hidapi"
        echo "before using this tool."
    fi
    "$PIP" install --user .
elif [ "$OS" = "Linux" ]; then
    "$PIP" install --user . --break-system-packages 2>/dev/null || "$PIP" install --user .

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
