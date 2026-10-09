#!/bin/bash
################################################################################
# USB Traffic Capture and Analysis for Kimura Mouse
#
# This script captures raw USB traffic to/from the mouse and helps reverse-
# engineer the actual protocol by showing what bytes are being sent.
#
# Usage:
#   sudo bash phase-b/usb_capture.sh capture          # Start capturing
#   sudo bash phase-b/usb_capture.sh analyze <pcap>   # Analyze captured file
#   sudo bash phase-b/usb_capture.sh report-desc      # Dump report descriptor
################################################################################

set -e

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
# Get device bus and device numbers
################################################################################

get_device_info() {
    local lsusb_output=$(lsusb | grep 248a)
    if [ -z "$lsusb_output" ]; then
        log_error "Mouse (248a) not found in lsusb output"
        return 1
    fi

    # lsusb output format: Bus 001 Device 003: ID 248a:5b49 Telink ...
    BUS=$(echo "$lsusb_output" | awk '{print $2}')
    DEVICE=$(echo "$lsusb_output" | awk '{print $4}' | tr -d ':')
    BUSNUM=$(printf "%03d" "$BUS")

    log_info "Found device: Bus $BUS, Device $DEVICE"
    echo "$lsusb_output"
}

################################################################################
# Capture USB traffic
################################################################################

capture_traffic() {
    if ! get_device_info; then
        return 1
    fi

    # Check if tcpdump is available
    if ! command -v tcpdump &> /dev/null; then
        log_error "tcpdump not found. Install with: sudo apt-get install tcpdump"
        return 1
    fi

    # Check if usbmon is available
    if [ ! -d /sys/kernel/debug/usb/usbmon ]; then
        log_warn "usbmon not loaded. Attempting to load..."
        sudo modprobe usbmon
    fi

    PCAP_FILE="kimura_usb_capture_$(date +%s).pcap"

    log_info "Starting USB traffic capture on bus $BUS"
    log_info "Output file: $PCAP_FILE"
    echo ""
    log_warn "Instructions:"
    echo "  1. Keep this capture running in the background"
    echo "  2. In another terminal, run commands like:"
    echo "     python3 phase-b/empirical_probe.py send \"04 01\""
    echo "  3. Press Ctrl+C here to stop capturing"
    echo ""

    # Capture USB traffic for this bus
    sudo tcpdump -i "usbmon${BUS}" -w "$PCAP_FILE" 2>/dev/null &
    TCPDUMP_PID=$!

    # Give tcpdump time to start
    sleep 1

    log_success "Capture started (PID: $TCPDUMP_PID)"
    log_info "Waiting for capture... (press Ctrl+C to stop)"

    # Wait for interrupt
    trap "sudo kill $TCPDUMP_PID 2>/dev/null; log_success 'Capture stopped'; log_info 'Analyze with: sudo bash phase-b/usb_capture.sh analyze $PCAP_FILE'" EXIT

    wait $TCPDUMP_PID 2>/dev/null || true
}

################################################################################
# Analyze captured traffic
################################################################################

analyze_traffic() {
    local pcap_file="$1"

    if [ ! -f "$pcap_file" ]; then
        log_error "PCAP file not found: $pcap_file"
        return 1
    fi

    if ! command -v tcpdump &> /dev/null; then
        log_error "tcpdump not found"
        return 1
    fi

    log_info "Analyzing captured USB traffic..."
    echo ""

    # Read the PCAP file and extract USB packets
    # Focus on packets to/from the device
    sudo tcpdump -r "$pcap_file" -X -q 2>/dev/null | head -200
}

################################################################################
# Dump report descriptor
################################################################################

dump_report_descriptor() {
    if ! get_device_info; then
        return 1
    fi

    log_info "Dumping report descriptor for bus $BUS device $DEVICE..."
    echo ""

    # Try usbhid-dump if available
    if command -v usbhid-dump &> /dev/null; then
        sudo usbhid-dump -d "$BUS:$DEVICE" -e descriptor 2>/dev/null || true
    else
        log_warn "usbhid-dump not available, trying lsusb..."
        sudo lsusb -d 248a: -v 2>/dev/null | grep -A 100 "HID Device Descriptor" | head -50 || true
    fi
}

################################################################################
# Alternative: Use tshark/Wireshark for easier analysis
################################################################################

capture_wireshark() {
    if ! get_device_info; then
        return 1
    fi

    if ! command -v tshark &> /dev/null; then
        log_error "tshark not found. Install with: sudo apt-get install tshark"
        log_info "Or use tcpdump capture instead"
        return 1
    fi

    PCAP_FILE="kimura_wireshark_$(date +%s).pcap"

    log_info "Starting capture with tshark (Wireshark CLI)..."
    log_info "Output file: $PCAP_FILE"
    echo ""
    log_warn "Run commands in another terminal while this captures"
    echo ""

    # Capture with tshark
    sudo tshark -i "usbmon${BUS}" -w "$PCAP_FILE" -f "usb.bus_id == \"$BUS\"" 2>/dev/null &
    TSHARK_PID=$!

    sleep 1
    log_success "Capture started (PID: $TSHARK_PID)"

    trap "sudo kill $TSHARK_PID 2>/dev/null; log_info 'Open in Wireshark: wireshark $PCAP_FILE'" EXIT
    wait $TSHARK_PID 2>/dev/null || true
}

################################################################################
# Main
################################################################################

case "${1:-help}" in
    capture)
        if [ "$EUID" -ne 0 ]; then
            log_error "This command requires root (use: sudo bash $0 capture)"
            exit 1
        fi
        capture_traffic
        ;;

    wireshark)
        if [ "$EUID" -ne 0 ]; then
            log_error "This command requires root (use: sudo bash $0 wireshark)"
            exit 1
        fi
        capture_wireshark
        ;;

    analyze)
        if [ "$EUID" -ne 0 ]; then
            log_error "This command requires root (use: sudo bash $0 analyze <file>)"
            exit 1
        fi
        if [ -z "$2" ]; then
            log_error "Usage: $0 analyze <pcap_file>"
            exit 1
        fi
        analyze_traffic "$2"
        ;;

    report-desc)
        if [ "$EUID" -ne 0 ]; then
            log_error "This command requires root (use: sudo bash $0 report-desc)"
            exit 1
        fi
        dump_report_descriptor
        ;;

    *)
        cat <<'EOF'
USB Traffic Capture for Kimura Mouse Protocol Reverse-Engineering

Usage:
  sudo bash usb_capture.sh capture              Capture USB traffic (tcpdump)
  sudo bash usb_capture.sh wireshark            Capture with tshark (Wireshark CLI)
  sudo bash usb_capture.sh analyze <pcap_file>  Analyze captured PCAP file
  sudo bash usb_capture.sh report-desc          Dump HID report descriptor

Examples:

  1. Start capturing:
     $ sudo bash phase-b/usb_capture.sh capture

  2. In another terminal, send test commands:
     $ python3 phase-b/empirical_probe.py send "04 01"
     $ python3 phase-b/empirical_probe.py send "02 FF 00"

  3. Stop the capture (Ctrl+C in capture terminal)

  4. Analyze the results:
     $ sudo bash phase-b/usb_capture.sh analyze kimura_usb_capture_*.pcap

  5. Or open in Wireshark:
     $ wireshark kimura_usb_capture_*.pcap

Notes:
  - tcpdump and Wireshark need to be installed
  - sudo/root access required for USB capture
  - Look for patterns in the data being sent/received
  - Document command bytes that correspond to LED/DPI changes
EOF
        exit 0
        ;;
esac
