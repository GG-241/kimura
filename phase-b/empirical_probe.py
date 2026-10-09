#!/usr/bin/env python3
"""
Empirical protocol discovery for Kimura v3.0 via observation.
Send patterns, observe device behavior (LED, DPI, etc.), reverse-engineer protocol.

Usage:
    python3 empirical_probe.py list              # Show devices
    python3 empirical_probe.py send <hex_bytes>  # Send raw command to interface 0
    python3 empirical_probe.py send <hex_bytes> --iface 1  # Send to specific interface
    python3 empirical_probe.py led               # Try LED commands
    python3 empirical_probe.py dpi               # Try DPI commands
    python3 empirical_probe.py test-write        # Test if writes work at all
"""

import argparse
import sys
import time
import hid

VID = 0x248A
PID = 0x5B49

# Settle delay between command and readback
SETTLE = 0.010


def find_all_devices():
    """Find all 0x248A:0x5B49 interfaces."""
    devices = []
    for d in hid.enumerate(VID, PID):
        devices.append(d)
    return devices


def find_device_by_iface(iface_num):
    """Find device by interface number."""
    for d in hid.enumerate(VID, PID):
        if d.get("interface_number") == iface_num:
            return d
    return None


def open_device(d):
    """Open and configure device."""
    dev = hid.device()
    dev.open_path(d["path"])
    dev.set_nonblocking(0)
    return dev


def send_command(dev, payload_hex, report_id=0, feature_len=65, verbose=True):
    """
    Send a raw command as hex bytes and read response.

    Args:
        payload_hex: hex string, e.g. "80" or "81 02 03"
        report_id: HID report ID
        feature_len: feature report length
    """
    # Parse hex input
    try:
        bytes_list = [int(x, 16) for x in payload_hex.replace(',', ' ').split()]
    except ValueError:
        print(f"Invalid hex: {payload_hex}")
        return None

    # Build command
    buf = bytearray(feature_len)
    buf[0] = report_id
    buf[1:1+len(bytes_list)] = bytes_list

    if verbose:
        print(f"\n  TX (report_id={report_id}, len={feature_len}): {' '.join('%02X' % b for b in buf[:min(16, len(buf))])}")

    try:
        dev.send_feature_report(bytes(buf))
        time.sleep(SETTLE)

        rx = dev.get_feature_report(report_id, feature_len)
        if verbose:
            print(f"  RX: {' '.join('%02X' % b for b in rx[:min(16, len(rx))])}")
            if len(rx) > 16:
                print(f"       {' '.join('%02X' % b for b in rx[16:min(32, len(rx))])}")

        return rx
    except Exception as e:
        print(f"  Error: {e}")
        return None


def cmd_list(args):
    """List all 0x248A devices."""
    found = find_all_devices()

    if not found:
        print("No 0x248A devices found.")
        return 1

    print(f"Found {len(found)} interface(s):\n")
    for i, d in enumerate(found):
        up = d.get("usage_page") or 0
        usage = d.get("usage") or 0
        iface = d.get("interface_number", "?")
        mfg = (d.get("manufacturer_string") or "").strip()
        prod = (d.get("product_string") or "").strip()
        path = d["path"].decode() if isinstance(d["path"], bytes) else d["path"]

        print(f"  [iface={iface}] usage_page=0x{up:04X} usage=0x{usage:04X}")
        if mfg or prod:
            print(f"          {mfg} {prod}")
        print(f"          Path: {path}\n")

    return 0


def cmd_send(args):
    """Send a raw hex command."""
    devices = find_all_devices()
    if not devices:
        print("No compatible device found.")
        return 1

    # If interface specified, use that; otherwise use first
    iface = args.iface if hasattr(args, 'iface') and args.iface is not None else 0

    d = None
    for device in devices:
        if device.get("interface_number") == iface:
            d = device
            break

    if not d:
        print(f"Interface {iface} not found. Available interfaces: {[dev.get('interface_number') for dev in devices]}")
        return 1

    print(f"Using interface {iface}")
    dev = open_device(d)
    send_command(dev, args.hex, report_id=args.report_id, feature_len=args.length)
    dev.close()
    return 0


def cmd_led(args):
    """Explore LED control commands.

    LED modes from the Windows GUI: Breathing, Neon, Wave, Customize, LED off.
    We'll try common opcode patterns.
    """
    devices = find_all_devices()
    if not devices:
        print("No compatible device found.")
        return 1

    d = devices[0]
    dev = open_device(d)

    print("Exploring LED control...")
    print("  (Watch the LED color/mode changes)\n")

    # Try some plausible LED opcodes based on the protocol doc
    # Opcode ranges: 0x0x (write), 0x8x (read), 0xFx (special)

    # Try small write opcodes with color/mode bytes
    test_cases = [
        ("01 00", "opcode 0x01, payload 0x00"),
        ("01 01", "opcode 0x01, payload 0x01"),
        ("01 02", "opcode 0x01, payload 0x02"),
        ("01 03", "opcode 0x01, payload 0x03"),
        ("02 FF 00", "opcode 0x02, payload 0xFF 0x00 (red?)"),
        ("02 00 FF", "opcode 0x02, payload 0x00 0xFF (green?)"),
        ("02 00 00", "opcode 0x02, payload 0x00 0x00 (off?)"),
        ("03 FF 00 00", "opcode 0x03, color RGB"),
        ("04 01", "opcode 0x04, mode 0x01"),
        ("04 02", "opcode 0x04, mode 0x02"),
        ("06 01", "opcode 0x06, LED mode 0x01"),
        ("F5", "opcode 0xF5, apply/commit"),
    ]

    for hex_cmd, desc in test_cases:
        print(f"  {desc:40} → {hex_cmd}")
        send_command(dev, hex_cmd, verbose=False)
        time.sleep(0.5)  # Give you time to observe

    dev.close()
    print("\nDone. Did you see any LED changes?")
    return 0


def cmd_test_write(args):
    """Test if writes actually work by observing response changes."""
    devices = find_all_devices()
    if not devices:
        print("No compatible device found.")
        return 1

    print("Testing write capability on all interfaces...")
    print("Sending different patterns and watching for response changes.\n")

    for d in devices:
        iface = d.get("interface_number", "?")
        print(f"Interface {iface}:")

        try:
            dev = open_device(d)

            # Read baseline
            rx1 = dev.get_feature_report(0, 65)
            baseline = bytes(rx1[:16])
            print(f"  Baseline: {' '.join('%02X' % b for b in baseline)}")

            # Send test pattern 1: all 0xFF
            print(f"  Sending 0xFF pattern...", end=" ")
            buf = bytearray(65)
            buf[:] = [0xFF] * 65
            dev.send_feature_report(bytes(buf))
            time.sleep(0.050)
            rx2 = dev.get_feature_report(0, 65)
            after1 = bytes(rx2[:16])

            if baseline != after1:
                print(f"✓ CHANGED: {' '.join('%02X' % b for b in after1)}")
            else:
                print("(no change)")

            # Send test pattern 2: 0x00
            print(f"  Sending 0x00 pattern...", end=" ")
            buf = bytearray(65)
            buf[:] = [0x00] * 65
            dev.send_feature_report(bytes(buf))
            time.sleep(0.050)
            rx3 = dev.get_feature_report(0, 65)
            after2 = bytes(rx3[:16])

            if baseline != after2 or after1 != after2:
                print(f"✓ CHANGED: {' '.join('%02X' % b for b in after2)}")
            else:
                print("(no change)")

            # Send test pattern 3: alternating 0xAA 0x55
            print(f"  Sending alternating pattern...", end=" ")
            buf = bytearray(65)
            for i in range(65):
                buf[i] = 0xAA if i % 2 == 0 else 0x55
            dev.send_feature_report(bytes(buf))
            time.sleep(0.050)
            rx4 = dev.get_feature_report(0, 65)
            after3 = bytes(rx4[:16])

            if baseline != after3:
                print(f"✓ CHANGED: {' '.join('%02X' % b for b in after3)}")
            else:
                print("(no change)")

            dev.close()

        except Exception as e:
            print(f"  Error: {e}")

        print()

    return 0


def cmd_dpi(args):
    """Explore DPI control commands.

    DPI values typically: 800, 1600, 3200, 6400, 12800 (doubling).
    Opcodes: look for patterns in how DPI is encoded.
    """
    devices = find_all_devices()
    if not devices:
        print("No compatible device found.")
        return 1

    d = devices[0]
    dev = open_device(d)

    print("Exploring DPI control...")
    print("  (Watch mouse pointer speed changes)\n")

    # Try common DPI payload patterns
    # DPI 800 is often 0x00 or 0x01, then doubling
    test_cases = [
        ("04 00", "DPI attempt 1"),
        ("04 01", "DPI attempt 2"),
        ("04 02", "DPI attempt 3"),
        ("04 03", "DPI attempt 4"),
        ("04 04", "DPI attempt 5"),
        ("01 00", "Alt DPI 1"),
        ("01 01", "Alt DPI 2"),
        ("01 02", "Alt DPI 3"),
        ("02 00 00", "Alt DPI with 2 bytes 1"),
        ("02 01 00", "Alt DPI with 2 bytes 2"),
        ("03 FF", "Alt DPI 3"),
        ("06 00", "Alt DPI 4"),
        ("F5", "Apply/commit"),
    ]

    for hex_cmd, desc in test_cases:
        print(f"  {desc:30} → {hex_cmd}")
        send_command(dev, hex_cmd, verbose=False)
        time.sleep(0.3)  # Give you time to test

    dev.close()
    print("\nDone. Did you notice any pointer speed changes?")
    return 0


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # list
    pl = sub.add_parser("list", help="List all 0x248A HID devices")
    pl.set_defaults(func=cmd_list)

    # send
    ps = sub.add_parser("send", help="Send raw hex command")
    ps.add_argument("hex", help="Hex bytes, e.g. '80' or '04 01 02'")
    ps.add_argument("--iface", type=int, default=0, help="Interface number (default 0)")
    ps.add_argument("--report-id", type=int, default=0, help="Report ID (default 0)")
    ps.add_argument("--length", type=int, default=65, help="Feature report length (default 65)")
    ps.set_defaults(func=cmd_send)

    # test-write
    pt = sub.add_parser("test-write", help="Test if device responds to writes")
    pt.set_defaults(func=cmd_test_write)

    # led
    pl = sub.add_parser("led", help="Probe LED control commands")
    pl.set_defaults(func=cmd_led)

    # dpi
    pd = sub.add_parser("dpi", help="Probe DPI control commands")
    pd.set_defaults(func=cmd_dpi)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
