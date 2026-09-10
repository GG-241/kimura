#!/bin/bash
################################################################################
# Ubuntu Setup Script for Kimura Mouse HID Communication
#
# This script automates the entire setup process for empirical protocol
# discovery on Ubuntu. It:
#   1. Installs hidapi and dependencies
#   2. Creates udev rule for unprivileged HID access
#   3. Reloads udev
#   4. Verifies the mouse is visible
#
# Usage:
#   bash setup_ubuntu.sh
#
# No arguments needed. The script checks prerequisites and reports status.
################################################################################

set -e  # Exit on error

COLOR_GREEN='\033[0;32m'
COLOR_YELLOW='\033[1;33m'
COLOR_RED='\033[0;31m'
COLOR_BLUE='\033[0;34m'
COLOR_RESET='\033[0m'

log_info() {
    echo -e "${COLOR_BLUE}[INFO]${COLOR_RESET} $1"
}

log_success() {
    echo -e "${COLOR_GREEN}[OK]${COLOR_RESET} $1"
}

log_warn() {
    echo -e "${COLOR_YELLOW}[WARN]${COLOR_RESET} $1"
}

log_error() {
    echo -e "${COLOR_RED}[ERROR]${COLOR_RESET} $1"
}

################################################################################
# Check if running on Ubuntu/Debian
################################################################################

log_info "Checking system..."

if ! grep -qi "ubuntu\|debian" /etc/os-release 2>/dev/null; then
    log_warn "This script is designed for Ubuntu/Debian-based systems."
    log_warn "It may work on other Linux distros but is not tested."
    read -p "Continue anyway? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log_info "Aborted."
        exit 1
    fi
fi

log_success "Running on a compatible system"

################################################################################
# Step 1: Update package list
################################################################################

log_info "Updating package list..."
sudo apt-get update -qq
log_success "Package list updated"

################################################################################
# Step 2: Install hidapi and dependencies
################################################################################

log_info "Installing hidapi and Python dependencies..."

packages_to_install=""

# Check if libhidapi-libusb0 is installed
if ! dpkg -l | grep -q libhidapi-libusb0; then
    packages_to_install="$packages_to_install libhidapi-libusb0"
fi

# Check if libhidapi-dev is installed
if ! dpkg -l | grep -q libhidapi-dev; then
    packages_to_install="$packages_to_install libhidapi-dev"
fi

# Check if python3-pip is installed
if ! command -v pip3 &> /dev/null; then
    packages_to_install="$packages_to_install python3-pip"
fi

if [ -n "$packages_to_install" ]; then
    log_info "Installing:$packages_to_install"
    sudo apt-get install -y $packages_to_install
else
    log_success "All system packages already installed"
fi

log_success "System dependencies installed"

################################################################################
# Step 3: Install Python hidapi
################################################################################

log_info "Installing Python hidapi..."

if python3 -c "import hid" &> /dev/null; then
    log_success "Python hidapi already installed"
else
    # NOTE: the Debian/Ubuntu apt package "python3-hidapi" is a different
    # project (hidapi-cffi) that installs a module named "hidapi" with an
    # incompatible API (hidapi.Device/hidapi.enumerate). This script's tools
    # require the "hid" module (trezor/cython-hidapi, hid.device()/hid.enumerate()),
    # which is only available via pip. Modern Ubuntu/Debian (PEP 668) block
    # system-wide pip installs, so --break-system-packages is required.
    pip3 install hidapi --break-system-packages --quiet
    log_success "Python hidapi installed (pip)"
fi

################################################################################
# Step 4: Create udev rule
################################################################################

log_info "Setting up udev rule for unprivileged HID access..."

UDEV_RULE="/etc/udev/rules.d/70-kimura.rules"

# Check if rule already exists
if [ -f "$UDEV_RULE" ]; then
    log_warn "Udev rule already exists at $UDEV_RULE"
    read -p "Overwrite? (y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        log_info "Keeping existing rule"
    else
        sudo bash -c "cat > $UDEV_RULE" <<'EOF'
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="248a", ATTRS{idProduct}=="5b49", MODE="0660", TAG+="uaccess"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="248a", ATTRS{idProduct}=="5b4a", MODE="0660", TAG+="uaccess"
EOF
        log_success "Udev rule created"
    fi
else
    sudo bash -c "cat > $UDEV_RULE" <<'EOF'
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="248a", ATTRS{idProduct}=="5b49", MODE="0660", TAG+="uaccess"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="248a", ATTRS{idProduct}=="5b4a", MODE="0660", TAG+="uaccess"
EOF
    log_success "Udev rule created at $UDEV_RULE"
fi

################################################################################
# Step 5: Reload udev
################################################################################

log_info "Reloading udev..."
sudo udevadm control --reload
sudo udevadm trigger
log_success "Udev reloaded and triggered"

################################################################################
# Step 6: Verify mouse is visible (optional check)
################################################################################

log_info "Checking for Kimura mouse (0x248a)..."

if lsusb 2>/dev/null | grep -qi "248a"; then
    log_success "Mouse found in USB device list!"
    lsusb | grep -i 248a
else
    log_warn "Mouse not detected in lsusb output"
    log_warn "Make sure it's plugged in to the Ubuntu machine (not through a dock)"
    log_warn "You can plug it in now and the udev rule will apply automatically"
fi

################################################################################
# Step 7: Check for hidraw devices
################################################################################

log_info "Checking for hidraw devices..."

if ls /dev/hidraw* &>/dev/null; then
    hidraw_count=$(ls /dev/hidraw* 2>/dev/null | wc -l)
    log_success "Found $hidraw_count hidraw device(s)"
    ls -la /dev/hidraw* 2>/dev/null | tail -3
else
    log_warn "No hidraw devices found yet"
    log_warn "Plug in the mouse and they should appear automatically"
fi

################################################################################
# Summary
################################################################################

echo ""
echo "================================================================================"
log_success "Setup complete!"
echo "================================================================================"
echo ""
log_info "Next steps:"
echo "  1. Plug in your Kimura mouse to this Ubuntu machine (if not already connected)"
echo "  2. Run:  python3 phase-b/empirical_probe.py list"
echo "  3. If you see the device, test with:  python3 phase-b/empirical_probe.py send \"04 01\""
echo "  4. Watch the LED — if it changes, writes are working!"
echo ""
log_info "For detailed troubleshooting, see:  phase-b/UBUNTU_SETUP.txt"
echo ""
