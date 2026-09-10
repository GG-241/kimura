#!/usr/bin/env python3
"""
Empirical protocol discovery for Kimura v3.0 via observation.
Send patterns, observe device behavior (LED, DPI, buttons), reverse-engineer protocol.

START HERE:
    python3 empirical_probe.py guide      # Step-by-step workflow — read this first

Usage:
    python3 empirical_probe.py list                          # Show devices
    python3 empirical_probe.py descriptor                     # Dump/decode HID report descriptors
    python3 empirical_probe.py read-all                       # Safe: sweep report IDs, try version+config reads
    python3 empirical_probe.py buttons                        # Safe: read-only button/config exploration
    python3 empirical_probe.py capture-buttons                 # Safe: watch Input reports live while pressing buttons
    python3 empirical_probe.py send <hex_bytes>                # Send raw command to interface 0
    python3 empirical_probe.py send <hex_bytes> --iface 1 --report-id 7 --length 8
    python3 empirical_probe.py led                             # Try LED write opcodes
    python3 empirical_probe.py dpi                             # Try DPI write opcodes
    python3 empirical_probe.py test-write                      # Test if writes work at all
    python3 empirical_probe.py findings                        # Print the logged findings (see below)
    python3 empirical_probe.py findings --opcode 03             # ...filtered to one opcode

FINDINGS LOG: `led`, `dpi`, and `send` all prompt for a free-text observation
after every write ("what did you see?") and, if you answer, append a
structured entry to phase-b/findings.jsonl (one JSON object per line: opcode,
payload, report_id/length, connection mode, timestamp, your observation).
Leave the prompt blank to skip logging a no-op step. `--connection wired` or
`--connection dongle` tags which physical connection the session used
(default: wired) — behavior may differ over the 2.4GHz dongle transport, so
don't assume dongle-mode matches these findings without re-testing. Run
`findings` any time to review everything logged so far — that file is the
ground truth to read before writing any real set_led()/set_dpi()/etc. code
into phase-a/kimura.py.

Findings so far (see phase-a/PROTOCOL.md for the static-analysis source):
    - The vendor-defined interface (usage_page 0xFF01, interface_number=1) is the
      only interface whose HID report descriptor defines a Feature report:
      report ID 7, 7-byte payload (8-byte buffer including the report-ID byte).
      That is the real command channel — NOT report_id=0/len=65, which earlier
      guesses used.
    - CONFIRMED WORKING: the read-all-settings opcode sequence from
      PROTOCOL.md §4.1 (0x81, 0x86, 0x82, 0x83, 0x84) echoes correctly at
      report_id=7/len=8 — `rx[1]` matches the opcode sent, exactly as the
      static analysis predicted. Run `read-all` to reproduce. This is real
      signal: the transport and command framing (buf[0]=report_id,
      buf[1]=opcode) are confirmed correct on real hardware.
    - Opcode 0x80 (documented as the version-read op) does NOT echo — it
      returns a fixed value ([07 02 55 05 15 25 35 45]) regardless of input.
      Either this firmware revision uses a different version opcode, or 0x80
      isn't implemented. Not yet a blocker since 0x81-0x84 work.
    - Static analysis of the vendor GUI (extracted from the MSI installer) found
      classes CLedSetDlg, CMacroSetDlg, CIrrButton, and confirms button/macro
      remapping is written via opcode 0x07 (a 288-byte bulk table: 72 entries of
      4 bytes each, entry-type bytes 0x80/0x00/0x04). This opcode is
      intentionally NOT wired up for writes here (see CLAUDE.md safety notes) —
      it's gated until a real capture confirms the encoding.

KNOWN HAZARD (observed twice on real hardware, 2026-08-20): running the `led`
sweep left the mouse's LED cycling/flashing erratically and input completely
unresponsive, recovered only by power-cycling via the 3-position switch on
the underside of the mouse (no permanent damage — flash survives the hang).
CONFIRMED root cause (2nd incident, reproduced without ever sending 0xF5):
sending payloads whose length doesn't match what static analysis documented
in PROTOCOL.md §4.2/§4.3. Specifically, the old LED sweep sent "03 FF 00 00"
(3 bytes) for opcode 0x03, which §4.3 says only ever writes 1 byte in the
real vendor code — and immediately after that sent "04 01" (1 byte) for
opcode 0x04, which §4.2 says is a *different* buffer/call site expecting 3
bytes. The hang occurred during/right after that "04 01" send. Extra or
missing payload bytes land on memory the real vendor code never touches at
that opcode, and that appears to be what wedges the firmware — not any one
opcode being inherently unsafe. (0xF5 is still excluded from automated
sweeps as a separate, additional precaution — see PROTOCOL.md §4.2 — but is
no longer believed to be the primary cause of either hang.)
Fixes applied: every entry in LED_TEST_CASES/DPI_TEST_CASES now has a
payload length that exactly matches its documented width, opcode 0x04 was
removed from the LED sweep (it isn't part of that buffer class), and both
sweeps pause for confirmation before every single write so you can stop
immediately if anything looks wrong. If the mouse ever stops responding
during ANY command in this file, power-cycle it the same way before doing
anything else — and do not trust a payload length that isn't in PROTOCOL.md.
"""

import argparse
import datetime
import json
import sys
import time
from pathlib import Path
# The pip "hidapi" package's "hid" module is libusb-backed and needs write
# access to /dev/bus/usb/*, which our udev rule doesn't grant. Use "hidraw"
# instead — it talks directly to /dev/hidraw*, which the udev rule (see
# setup_ubuntu.sh) already makes accessible.
import hidraw as hid

VID = 0x248A
PID = 0x5B49

# Settle delay between command and readback
SETTLE = 0.010

# --- Findings log ------------------------------------------------------
# One JSON object per line — easy to both eyeball (via `findings`) and
# machine-parse later when writing the real set_led()/set_dpi()/etc. in
# kimura.py. Every logged entry records exactly what was sent, what report
# ID/length it used, and the free-text observation, so a future session (or
# a future me) can read this file and know precisely what's confirmed vs.
# still a guess, without having to re-derive it from chat history.
FINDINGS_LOG = Path(__file__).resolve().parent / "findings.jsonl"


def log_finding(hex_cmd, report_id, length, observation, source, connection):
    """Append one structured finding to FINDINGS_LOG. observation is
    free-text from the human watching the mouse; source identifies which
    command produced this entry (e.g. 'led', 'dpi', 'send')."""
    bytes_list = [int(x, 16) for x in hex_cmd.replace(',', ' ').split()]
    entry = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "source": source,
        "connection": connection,
        "opcode_hex": f"0x{bytes_list[0]:02X}",
        "payload_hex": " ".join("%02X" % b for b in bytes_list[1:]),
        "full_command": hex_cmd,
        "report_id": report_id,
        "length": length,
        "observation": observation,
    }
    with open(FINDINGS_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def _prompt_observation():
    """Ask what the user saw after a write. Blank input means 'don't log
    this one' — keeps the log free of noise from steps with no effect,
    unless the user explicitly wants to record 'no visible change' too."""
    return input("    Observation (what did you see)? Enter to skip logging: ").strip()

# Confirmed from the HID report descriptor (see `descriptor` command): the
# vendor-defined interface is the only one with a Feature report, at this
# report ID and length. Everything else in this file used to default to
# report_id=0, length=65, which was wrong.
VENDOR_IFACE = 1
VENDOR_REPORT_ID = 7
VENDOR_FEATURE_LEN = 8  # 1 report-id byte + 7 payload bytes


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


def unique_interfaces(devices):
    """hidraw enumerate() returns one entry per HID usage, so a single
    interface/node can appear many times. Collapse to one entry per path."""
    seen = set()
    out = []
    for d in devices:
        if d["path"] in seen:
            continue
        seen.add(d["path"])
        out.append(d)
    return out


def send_command(dev, payload_hex, report_id=VENDOR_REPORT_ID, feature_len=VENDOR_FEATURE_LEN, verbose=True):
    """
    Send a raw command as hex bytes and read response.

    Args:
        payload_hex: hex string, e.g. "80" or "81 02 03"
        report_id: HID report ID
        feature_len: feature report length (including the report-ID byte)
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
    found = unique_interfaces(find_all_devices())

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
        tag = "  <-- vendor-defined (likely command channel)" if up == 0xFF01 else ""

        print(f"  [iface={iface}] usage_page=0x{up:04X} usage=0x{usage:04X}{tag}")
        if mfg or prod:
            print(f"          {mfg} {prod}")
        print(f"          Path: {path}\n")

    return 0


def cmd_descriptor(args):
    """Dump and lightly decode the HID report descriptor for each interface.

    This is the ground truth for which report IDs exist, what type they are
    (Input/Output/Feature), and how long they are. Use this instead of
    guessing report_id/length combinations.
    """
    devices = unique_interfaces(find_all_devices())
    if not devices:
        print("No compatible device found.")
        return 1

    ITEM_NAMES = {
        0x04: "Usage Page", 0x08: "Usage", 0x14: "Logical Minimum",
        0x24: "Logical Maximum", 0x74: "Report Size", 0x94: "Report Count",
        0x84: "Report ID", 0x80: "Input", 0x90: "Output", 0xB0: "Feature",
        0xA0: "Collection", 0xC0: "End Collection",
    }

    for d in devices:
        iface = d.get("interface_number", "?")
        up = d.get("usage_page") or 0
        print(f"\n=== iface={iface} usage_page=0x{up:04X} path={d['path']} ===")
        dev = open_device(d)
        try:
            desc = dev.get_report_descriptor()
        except Exception as e:
            print(f"  Error reading descriptor: {e}")
            dev.close()
            continue
        dev.close()

        print("  Raw: " + " ".join("%02X" % b for b in desc))

        # Minimal short-item walker good enough to spot Report ID / Feature /
        # Output / Input items and their sizes — not a full HID parser.
        i = 0
        current_report_id = None
        report_size = 0
        report_count = 0
        print("  Decoded:")
        while i < len(desc):
            tag_type = desc[i]
            size_code = tag_type & 0x03
            size = {0: 0, 1: 1, 2: 2, 3: 4}[size_code]
            key = tag_type & 0xFC
            value = int.from_bytes(desc[i+1:i+1+size], "little") if size else None
            name = ITEM_NAMES.get(key)

            if key == 0x84:  # Report ID
                current_report_id = value
            elif key == 0x74:  # Report Size
                report_size = value
            elif key == 0x94:  # Report Count
                report_count = value
            elif key in (0x80, 0x90, 0xB0):  # Input / Output / Feature
                kind = {0x80: "Input", 0x90: "Output", 0xB0: "Feature"}[key]
                total_bits = report_size * report_count
                total_bytes = (total_bits + 7) // 8
                print(f"    report_id={current_report_id} {kind:8} "
                      f"{report_count}x{report_size}bit = {total_bytes} payload bytes "
                      f"(buffer len incl. report-ID byte = {total_bytes + 1})")

            i += 1 + size

    return 0


def cmd_read_all(args):
    """Safe, read-only sweep: try opcode 0x80 (version) and the documented
    read-all sequence (0x81, 0x86, 0x82, 0x83, 0x84 — see PROTOCOL.md §4.1)
    across every interface/report_id/length combination actually defined in
    the HID descriptors (via `descriptor`), instead of blind guesses.

    This never writes anything opcode 0xFA/0x05/0x07 would touch — it is the
    same class of read used by kimura.py's `probe --read-all`.
    """
    devices = unique_interfaces(find_all_devices())
    if not devices:
        print("No compatible device found.")
        return 1

    READ_OPS = [0x80, 0x81, 0x86, 0x82, 0x83, 0x84]

    for d in devices:
        iface = d.get("interface_number", "?")
        dev = open_device(d)

        # Discover candidate (report_id, length) pairs for Feature reports
        # on this interface from its descriptor, falling back to a couple of
        # historically-tried guesses if the descriptor has no Feature report.
        candidates = []
        try:
            desc = dev.get_report_descriptor()
            i, rid, rsize, rcount = 0, None, 0, 0
            while i < len(desc):
                tag_type = desc[i]
                size = {0: 0, 1: 1, 2: 2, 3: 4}[tag_type & 0x03]
                key = tag_type & 0xFC
                value = int.from_bytes(desc[i+1:i+1+size], "little") if size else None
                if key == 0x84:
                    rid = value
                elif key == 0x74:
                    rsize = value
                elif key == 0x94:
                    rcount = value
                elif key == 0xB0:  # Feature
                    total_bytes = (rsize * rcount + 7) // 8
                    candidates.append((rid, total_bytes + 1))
                i += 1 + size
        except Exception:
            pass

        if not candidates:
            print(f"iface={iface}: no Feature report in descriptor, skipping")
            dev.close()
            continue

        print(f"\niface={iface}: trying {candidates}")
        for report_id, length in candidates:
            for op in READ_OPS:
                buf = bytearray(length)
                buf[0] = report_id
                buf[1] = op
                try:
                    dev.send_feature_report(bytes(buf))
                    time.sleep(SETTLE)
                    rx = dev.get_feature_report(report_id, length)
                    echo_ok = len(rx) > 1 and rx[1] == op
                    marker = "  <-- OPCODE ECHOED, this looks real" if echo_ok else ""
                    print(f"  report_id={report_id} len={length} op=0x{op:02X}: "
                          f"{' '.join('%02X' % b for b in rx)}{marker}")
                except Exception as e:
                    print(f"  report_id={report_id} len={length} op=0x{op:02X}: ERROR {e}")

        dev.close()

    print("\nDone. If nothing shows 'OPCODE ECHOED', the opcode/report-ID pair "
          "this firmware actually uses is still unconfirmed — proceed to "
          "`guide` for the dynamic-capture workflow.")
    return 0


DIFF_READ_OPS = [0x80, 0x81, 0x86, 0x82, 0x83, 0x84]


def _read_snapshot(dev):
    """Send each of DIFF_READ_OPS and capture the FULL payload (not just the
    opcode-echo check `read-all` does) at the confirmed vendor channel
    (report_id=7, len=8). Returns {opcode: bytes}."""
    snap = {}
    for op in DIFF_READ_OPS:
        buf = bytearray(VENDOR_FEATURE_LEN)
        buf[0] = VENDOR_REPORT_ID
        buf[1] = op
        try:
            dev.send_feature_report(bytes(buf))
            time.sleep(SETTLE)
            rx = dev.get_feature_report(VENDOR_REPORT_ID, VENDOR_FEATURE_LEN)
            snap[op] = bytes(rx)
        except Exception as e:
            snap[op] = None
    return snap


def cmd_dpi_diff(args):
    """Read-only DPI opcode hunt: take a full snapshot of every confirmed
    read opcode's payload, prompt you to physically change DPI, take a
    second snapshot, and diff byte-by-byte. Whichever opcode/byte changes
    is the strongest lead yet for where DPI lives in the config structure —
    the prerequisite for eventually guessing a WRITE opcode. Purely a read
    operation; zero write/hang risk on its own.

    This does not itself let you set DPI in software — it's step 1 towards
    finding the opcode that would. See phase-b/EMPIRICAL_DISCOVERY.md and
    phase-a/PROTOCOL.md §5 for the current state of the DPI-opcode search.
    """
    devices = unique_interfaces(find_all_devices())
    d = next((x for x in devices if x.get("interface_number") == VENDOR_IFACE), None)
    if not d:
        print("No compatible device found.")
        return 1

    dev = open_device(d)

    print("Taking BEFORE snapshot (opcodes: %s)..."
          % ", ".join("0x%02X" % o for o in DIFF_READ_OPS))
    before = _read_snapshot(dev)
    for op, rx in before.items():
        print(f"  0x{op:02X}: {' '.join('%02X' % b for b in rx) if rx else 'ERROR'}")

    input("\nNow physically change DPI (press the DPI button) and press Enter "
          "when done: ")

    print("\nTaking AFTER snapshot...")
    after = _read_snapshot(dev)
    for op, rx in after.items():
        print(f"  0x{op:02X}: {' '.join('%02X' % b for b in rx) if rx else 'ERROR'}")

    print("\nDiff:")
    any_diff = False
    for op in DIFF_READ_OPS:
        b, a = before[op], after[op]
        if b is None or a is None:
            continue
        if b != a:
            any_diff = True
            diffs = [i for i in range(min(len(b), len(a))) if b[i] != a[i]]
            print(f"  0x{op:02X}: CHANGED at byte offset(s) {diffs}")
            print(f"       before: {' '.join('%02X' % x for x in b)}")
            print(f"       after:  {' '.join('%02X' % x for x in a)}")
            log_finding("%02X" % op, VENDOR_REPORT_ID, VENDOR_FEATURE_LEN,
                        f"DPI-diff: byte offset(s) {diffs} changed after physical "
                        f"DPI button press. before={b.hex()} after={a.hex()}",
                        "dpi-diff", args.connection)

    if not any_diff:
        print("  No byte changed in any of the 6 read opcodes.")
        log_finding("00", VENDOR_REPORT_ID, VENDOR_FEATURE_LEN,
                    "DPI-diff: no byte changed across 0x80/0x81/0x86/0x82/0x83/0x84 "
                    "after physical DPI button press — DPI state isn't exposed "
                    "through any of these read opcodes.",
                    "dpi-diff", args.connection)
        print("  This is still useful: DPI's state isn't exposed through any of")
        print("  these 6 read opcodes — a Windows-GUI capture would be needed to")
        print("  find it, not further blind probing of these particular reads.")

    dev.close()
    return 0


def cmd_buttons(args):
    """Read-only exploration aimed at button/macro configuration.

    Per phase-a/PROTOCOL.md and static analysis of the vendor GUI (extracted
    from the MSI installer — classes CMacroSetDlg/CIrrButton), button and
    macro remapping is written via opcode 0x07: a 288-byte table of 72
    entries x 4 bytes (entry-type byte 0x80/0x00/0x04 + payload). That WRITE
    path is intentionally not wired up here (bulk upload, gated per
    CLAUDE.md). What we *can* safely do without hardware risk is read
    whatever the device currently reports on the vendor Feature channel, so
    you have a documented baseline to diff against once you capture a real
    button remap from the Windows GUI.
    """
    devices = unique_interfaces(find_all_devices())
    d = next((x for x in devices if x.get("interface_number") == VENDOR_IFACE), None)
    if not d:
        print(f"No vendor interface (interface_number={VENDOR_IFACE}) found.")
        return 1

    dev = open_device(d)
    print(f"Reading vendor Feature report (report_id={VENDOR_REPORT_ID}, "
          f"len={VENDOR_FEATURE_LEN}) with no prior write (device default/idle state):")
    rx = dev.get_feature_report(VENDOR_REPORT_ID, VENDOR_FEATURE_LEN)
    print("  " + " ".join("%02X" % b for b in rx))

    print("\nThis is what buttons/macros currently look like from this channel.")
    print("The actual per-button action table (opcode 0x07, 288 bytes) is a bulk")
    print("Output-report upload, not a Feature read — it won't show up here.")
    print("Next step: run `guide` for how to capture it for real.")
    dev.close()
    return 0


def _decode_input_report(iface, data):
    """Best-effort human-readable decode of a raw Input report, based on
    the HID report descriptor (see `descriptor`). Returns a string."""
    if not data:
        return None
    report_id = data[0]

    if iface == 0 and report_id == 1 and len(data) >= 7:
        # [0]=report_id [1]=button bitmask [2:4]=dX (s16 LE) [4:6]=dY (s16 LE) [6]=wheel (s8)
        buttons = data[1]
        bits_set = [i for i in range(8) if buttons & (1 << i)]
        dx = int.from_bytes(bytes(data[2:4]), "little", signed=True)
        dy = int.from_bytes(bytes(data[4:6]), "little", signed=True)
        wheel = data[6] - 256 if data[6] > 127 else data[6]
        return (f"[mouse]   buttons=0x{buttons:02X} bits_set={bits_set} "
                f"dX={dx:+d} dY={dy:+d} wheel={wheel:+d}  raw={' '.join('%02X' % b for b in data[:7])}")
    if iface == 0 and report_id == 2 and len(data) >= 3:
        # Consumer control: 16-bit usage code, e.g. volume up/down
        code = int.from_bytes(bytes(data[1:3]), "little")
        return f"[consumer] usage_code=0x{code:04X}  raw={' '.join('%02X' % b for b in data[:3])}"
    if iface == 0 and report_id == 3 and len(data) >= 2:
        return f"[system]  bits=0x{data[1]:02X}  raw={' '.join('%02X' % b for b in data[:2])}"
    if iface == 1 and report_id == 7:
        return f"[vendor]  raw={' '.join('%02X' % b for b in data[:8])}..."
    if iface == 2:
        # No report-ID byte on this interface: [0]=modifiers [1]=reserved [2:8]=keycodes
        if len(data) >= 8 and any(data[:8]):
            return f"[keyboard] modifiers=0x{data[0]:02X} keycodes={list(data[2:8])}  raw={' '.join('%02X' % b for b in data[:8])}"
        return None
    return f"[iface={iface}] raw={' '.join('%02X' % b for b in data)}"


def cmd_capture_buttons(args):
    """Passively watch Input reports across all interfaces while you
    physically press buttons — 100% read-only, no writes at all, so there's
    no hang/crash risk. This is how to map which bit/report corresponds to
    which physical button, without touching the gated opcode 0x07 button
    table.

    Standard clicks (left/right/middle) show up as report_id=1 button bits
    on interface 0. Side/extra buttons may appear the same way, as a
    consumer-control code (report_id=2, e.g. if mapped to volume), a
    system-control bit (report_id=3), a keyboard keycode (interface 2, if
    mapped to a key/macro), or vendor-specific data on interface 1 — this
    prints whichever channel actually produces something, so press each
    button one at a time and watch which line appears.
    """
    devices = unique_interfaces(find_all_devices())
    targets = []
    for iface_num in (0, 1, 2):
        d = next((x for x in devices if x.get("interface_number") == iface_num), None)
        if d:
            dev = open_device(d)
            dev.set_nonblocking(1)
            targets.append((iface_num, dev))

    if not targets:
        print("No compatible device found.")
        return 1

    print(f"Watching {len(targets)} interface(s) for Input reports: "
          f"{[i for i, _ in targets]}")
    print("Press each physical button ONE AT A TIME (pause briefly between each)")
    print("and note which line appears for which button — paste the output back")
    print("for it to be recorded in PROTOCOL.md / findings.jsonl.")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            saw_anything = False
            for iface_num, dev in targets:
                try:
                    data = dev.read(64)
                except Exception:
                    continue
                if data:
                    saw_anything = True
                    line = _decode_input_report(iface_num, data)
                    if line:
                        ts = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
                        print(f"  {ts}  {line}")
            if not saw_anything:
                time.sleep(0.01)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        for _, dev in targets:
            dev.close()

    return 0


def cmd_send(args):
    """Send a raw hex command. With --measure, also runs the same
    drag-distance movement measurement `dpi` uses — handy for testing
    specific values in whatever order you choose (e.g. an A/B/A/B
    counterbalanced comparison) rather than being stuck with the fixed
    sweep order in DPI_TEST_CASES."""
    devices = unique_interfaces(find_all_devices())
    if not devices:
        print("No compatible device found.")
        return 1

    iface = args.iface if args.iface is not None else VENDOR_IFACE

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

    magnitude = None
    if args.measure:
        d0 = next((x for x in devices if x.get("interface_number") == 0), None)
        if not d0:
            print("Warning: interface 0 not found, can't measure movement.")
        else:
            dev0 = open_device(d0)
            dev0.set_nonblocking(1)
            magnitude = _measure_drag(dev0)
            dev0.close()

    note = args.note
    if magnitude is not None:
        note = f"magnitude={magnitude}" + (f"; {note}" if note else "")

    if note:
        # Non-interactive (--note or --measure was passed): log without prompting.
        log_finding(args.hex, args.report_id, args.length, note, "send", args.connection)
        print(f"Logged to {FINDINGS_LOG.name}")
    elif sys.stdin.isatty():
        observation = _prompt_observation()
        if observation:
            log_finding(args.hex, args.report_id, args.length, observation, "send", args.connection)
            print(f"Logged to {FINDINGS_LOG.name}")

    dev.close()
    return 0


def cmd_measure(args):
    """Pure observation, no write at all: measure current drag-distance
    movement magnitude via interface 0. Useful for checking the mouse's
    CURRENT movement scaling (e.g. after changing DPI with the physical
    onboard button, which produces no HID event at all — see PROTOCOL.md
    §2.3) without sending any vendor command first. Run it, drag the mouse
    the same way you have in past measurements, and compare the number
    against previously-logged 'dpi'/'send --measure' magnitudes for the
    same drag to see whether movement scaling actually changed."""
    devices = unique_interfaces(find_all_devices())
    d0 = next((x for x in devices if x.get("interface_number") == 0), None)
    if not d0:
        print("No compatible device found (interface 0).")
        return 1

    dev0 = open_device(d0)
    dev0.set_nonblocking(1)
    magnitude = _measure_drag(dev0, max_seconds=args.seconds)
    dev0.close()

    if args.note:
        log_finding("00", 0, 1, f"magnitude={magnitude}; {args.note}", "measure", args.connection)
        print(f"Logged to {FINDINGS_LOG.name}")
    elif sys.stdin.isatty():
        observation = _prompt_observation()
        if observation:
            log_finding("00", 0, 1, f"magnitude={magnitude}; {observation}", "measure", args.connection)
            print(f"Logged to {FINDINGS_LOG.name}")

    return 0


def cmd_findings(args):
    """Print the accumulated findings log (phase-b/findings.jsonl) as a
    readable table. This is the file to read before writing any real
    set_led()/set_dpi()/etc. code in kimura.py — it's the ground truth for
    what's actually been confirmed on hardware, vs. still a guess."""
    if not FINDINGS_LOG.exists():
        print(f"No findings logged yet ({FINDINGS_LOG} doesn't exist).")
        print("Run `led`, `dpi`, or `send` and answer the observation prompt to start logging.")
        return 0

    entries = []
    with open(FINDINGS_LOG) as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))

    if not entries:
        print(f"{FINDINGS_LOG} exists but is empty.")
        return 0

    if args.opcode:
        want = args.opcode.lower().replace("0x", "")
        entries = [e for e in entries if e["opcode_hex"].lower().replace("0x", "") == want]

    print(f"{len(entries)} finding(s) in {FINDINGS_LOG}:\n")
    for e in entries:
        print(f"  [{e['ts']}] source={e['source']:6} connection={e['connection']:12} "
              f"cmd={e['full_command']:12} (report_id={e['report_id']}, len={e['length']})")
        print(f"      -> {e['observation']}")
    return 0


# PAYLOAD LENGTHS MUST MATCH phase-a/PROTOCOL.md §4.3 EXACTLY.
# CONFIRMED HAZARD (real hardware, 2026-08-20): the previous version of this
# list sent "03 FF 00 00" (3 payload bytes) for opcode 0x03, but §4.3 says
# 0x03 only ever writes 1 byte (at +0x17E) in the real vendor code. The extra
# 2 bytes land on memory the real code never touches. Right after that, "04
# 01" was sent — opcode 0x04 is NOT part of this short-command class at all;
# it's a *different* buffer/call site (§4.2, 3 bytes at +0xC6/+0xC7/+0xC8),
# so sending it as a 1-byte short-command payload was itself a modeling bug.
# The mouse hung (LED flashing rapidly, input unresponsive) during/after
# that "04 01" send — recovered only via physical power-cycle. Root cause is
# most likely payload-length mismatch, not any single opcode being
# inherently dangerous. Opcode 0x04 has been removed from this list (it
# belongs in DPI_TEST_CASES, correctly sized); every remaining entry's
# payload length now matches the documented width exactly.
# --- CONFIRMED 2026-08-20 -------------------------------------------------
# Opcode 0x02's payload has NO observable effect on LED: 00 00 / FF FF /
# 00 FF / FF 00 all produced the identical "blue breathing" default. That's
# not 0x02 setting anything — it's the mouse's idle animation reasserting
# itself. 0x02's real purpose (if any) is still unknown; kept in the sweep
# below (once, not four times) only as a baseline/no-op reference point.
#
# Opcode 0x03's single payload byte is a CONFIRMED LED preset/mode selector
# (full table in PROTOCOL.md §4.3 / EMPIRICAL_DISCOVERY.md). 0x00-0x07,
# 0x09, 0x10-0x12, and 0x15-0x17 are all mapped. Remaining entries below
# fill the gaps (0x08, 0x0A-0x0F, 0x13-0x14) and probe past 0x17 to find
# the preset count / upper bound.
LED_TEST_CASES = [
    ("01 00", "opcode 0x01, payload 0x00 (1 byte, matches PROTOCOL.md §4.3)"),
    ("01 01", "opcode 0x01, payload 0x01"),
    ("01 02", "opcode 0x01, payload 0x02"),
    ("01 03", "opcode 0x01, payload 0x03"),
    ("02 FF 00", "opcode 0x02, payload 0xFF 0x00 — CONFIRMED no LED effect (baseline only)"),
    ("06 00 00", "opcode 0x06, payload 0x00 0x00 (2 bytes, matches §4.3 — was wrongly 1 byte before)"),
    ("06 01 00", "opcode 0x06, payload 0x01 0x00"),
    ("03 00", "opcode 0x03, payload 0x00 — CONFIRMED: blue, breathing (default)"),
    ("03 01", "opcode 0x03, payload 0x01 — CONFIRMED: slight purple, breathing"),
    ("03 02", "opcode 0x03, payload 0x02 — CONFIRMED: orange-red, breathing"),
    ("03 03", "opcode 0x03, payload 0x03 — CONFIRMED: cyan, altered breathing (flashing)"),
    ("03 04", "opcode 0x03, payload 0x04 — CONFIRMED: chase, lights on back-to-front"),
    ("03 05", "opcode 0x03, payload 0x05 — CONFIRMED: chase, color changes per pass"),
    ("03 06", "opcode 0x03, payload 0x06 — CONFIRMED: LED OFF"),
    ("03 07", "opcode 0x03, payload 0x07 — CONFIRMED: like 0x05, starts yellow"),
    ("03 08", "opcode 0x03, payload 0x08 — GAP, not yet tested"),
    ("03 09", "opcode 0x03, payload 0x09 — CONFIRMED: instant blue, then OFF"),
    ("03 0A", "opcode 0x03, payload 0x0A — GAP, not yet tested"),
    ("03 0B", "opcode 0x03, payload 0x0B — GAP, not yet tested"),
    ("03 0C", "opcode 0x03, payload 0x0C — GAP, not yet tested"),
    ("03 0D", "opcode 0x03, payload 0x0D — GAP, not yet tested"),
    ("03 0E", "opcode 0x03, payload 0x0E — GAP, not yet tested"),
    ("03 0F", "opcode 0x03, payload 0x0F — GAP, not yet tested"),
    ("03 10", "opcode 0x03, payload 0x10 — CONFIRMED: breathing, slower rate"),
    ("03 11", "opcode 0x03, payload 0x11 — CONFIRMED: cyan, slow breathing"),
    ("03 12", "opcode 0x03, payload 0x12 — CONFIRMED: breathing, slower, green"),
    ("03 13", "opcode 0x03, payload 0x13 — GAP, not yet tested"),
    ("03 14", "opcode 0x03, payload 0x14 — GAP, not yet tested"),
    ("03 15", "opcode 0x03, payload 0x15 — CONFIRMED: fast blink, settles to slow breathing"),
    ("03 16", "opcode 0x03, payload 0x16 — CONFIRMED: same/similar to 0x15"),
    ("03 17", "opcode 0x03, payload 0x17 — CONFIRMED: same/similar to 0x15"),
    ("03 18", "opcode 0x03, payload 0x18 — probing past known range"),
    ("03 19", "opcode 0x03, payload 0x19"),
    ("03 1A", "opcode 0x03, payload 0x1A"),
    ("03 1B", "opcode 0x03, payload 0x1B"),
    ("03 1C", "opcode 0x03, payload 0x1C — if this repeats an earlier effect, the list likely wraps here"),
    # 0xF5 ("apply/commit", PROTOCOL.md §4.2) is DELIBERATELY NOT in this list —
    # send it only manually, once every preceding write is already confirmed
    # correct, never as part of an automated sweep.
]


def _confirm_step(desc, hex_cmd):
    """Pause for the human to observe + bail out before every write. Returns
    'send', 'skip', or 'stop'."""
    resp = input(f"  About to send {desc:40} ({hex_cmd}) — Enter to send, "
                 f"'s' to skip, 'q' to stop: ").strip().lower()
    if resp == "q":
        return "stop"
    if resp == "s":
        return "skip"
    return "send"


def _measure_drag(dev0, max_seconds=4):
    """Read interface-0 mouse Input reports (report_id=1) for up to
    max_seconds while the user drags the mouse, summing |dX|+|dY|. This
    turns "does DPI feel different" into a number: drag the SAME physical
    distance after each candidate write, and compare totals — a real DPI
    change should show up as a clearly different total, not a subjective
    judgment call. Returns the total magnitude (int)."""
    print(f"    >>> Drag the mouse in a straight line now (same distance "
          f"each time) — measuring for {max_seconds}s...")
    total = 0
    start = time.monotonic()
    while time.monotonic() - start < max_seconds:
        try:
            data = dev0.read(64)
        except Exception:
            data = None
        if data and data[0] == 1 and len(data) >= 6:
            dx = int.from_bytes(bytes(data[2:4]), "little", signed=True)
            dy = int.from_bytes(bytes(data[4:6]), "little", signed=True)
            total += abs(dx) + abs(dy)
        else:
            time.sleep(0.005)
    print(f"    >>> Measured movement magnitude: {total}")
    return total


def _send_with_retry(dev, hex_cmd, desc, settle, source, connection,
                      report_id=VENDOR_REPORT_ID, length=VENDOR_FEATURE_LEN,
                      measure_dev0=None):
    """Send hex_cmd, log an observation for it, then offer to resend the
    *exact same* command again — useful for confirming a visible effect is
    reproducible (not a fluke) before trusting it. Returns 'stop' if the
    user wants to abort the whole sweep from here, otherwise None.

    If measure_dev0 is given (an already-open interface-0 device), also run
    an objective drag-distance measurement via _measure_drag() and fold the
    magnitude into the logged observation — logged even if the user leaves
    the free-text observation blank, since the number itself is data."""
    while True:
        send_command(dev, hex_cmd, verbose=False)
        time.sleep(settle)

        magnitude = None
        if measure_dev0 is not None:
            magnitude = _measure_drag(measure_dev0)

        observation = _prompt_observation()
        if magnitude is not None:
            observation = f"magnitude={magnitude}" + (f"; {observation}" if observation else "")
        if observation:
            log_finding(hex_cmd, report_id, length, observation, source, connection)
            print(f"    Logged to {FINDINGS_LOG.name}")
        resp = input(f"    'r' to resend the same command again, "
                     f"Enter to continue, 'q' to stop: ").strip().lower()
        if resp == "q":
            return "stop"
        if resp != "r":
            return None


def cmd_led(args):
    """Explore LED control commands, one at a time with a pause to observe,
    and the option to resend the same command to check reproducibility.

    LED modes from the Windows GUI: Breathing, Neon, Wave, Customize, LED off.
    We'll try common opcode patterns.

    SAFETY: this used to fire the whole sweep unattended, ending in opcode
    0xF5 ("apply/commit"). On real hardware that sequence left the mouse's
    LED cycling erratically and input unresponsive until the physical power
    switch (the 3-position switch on the underside) was cycled — a firmware
    hang, not permanent damage, but avoid it: this pauses before each write
    so you can stop the moment something looks wrong, and 0xF5 is not
    included at all (send it manually, deliberately, if you ever need to).
    """
    devices = unique_interfaces(find_all_devices())
    d = next((x for x in devices if x.get("interface_number") == VENDOR_IFACE), None)
    if not d:
        print("No compatible device found.")
        return 1

    dev = open_device(d)

    print("Exploring LED control...")
    print("  (Watch the LED color/mode changes after each step)")
    print("  After sending, 'r' resends the same command so you can confirm")
    print("  the effect is reproducible, not a one-off.")
    print("  If the mouse ever stops responding or the LED behaves oddly,")
    print("  press 'q' at the next prompt and power-cycle the mouse (the")
    print("  3-position switch underneath) before doing anything else.\n")

    for hex_cmd, desc in LED_TEST_CASES:
        result = _confirm_step(desc, hex_cmd)
        if result == "stop":
            print("Stopped by user.")
            break
        if result == "skip":
            continue
        if _send_with_retry(dev, hex_cmd, desc, settle=0.5,
                             source="led", connection=args.connection) == "stop":
            print("Stopped by user.")
            break

    dev.close()
    print("\nDone. Did you see any LED changes?")
    print(f"Findings logged this session: run `python3 empirical_probe.py findings` to review.")
    return 0


def cmd_test_write(args):
    """Test if writes actually work by observing response changes."""
    devices = unique_interfaces(find_all_devices())
    if not devices:
        print("No compatible device found.")
        return 1

    print("Testing write capability on all interfaces...")
    print("Sending different patterns and watching for response changes.\n")

    for d in devices:
        iface = d.get("interface_number", "?")
        # Use the descriptor-confirmed report/length for the vendor
        # interface; fall back to a best-effort guess elsewhere.
        report_id = VENDOR_REPORT_ID if iface == VENDOR_IFACE else 0
        length = VENDOR_FEATURE_LEN if iface == VENDOR_IFACE else 65

        print(f"Interface {iface} (report_id={report_id}, len={length}):")

        try:
            dev = open_device(d)

            # Read baseline
            rx1 = dev.get_feature_report(report_id, length)
            baseline = bytes(rx1)
            print(f"  Baseline: {' '.join('%02X' % b for b in baseline)}")

            # Send test pattern 1: all 0xFF
            print(f"  Sending 0xFF pattern...", end=" ")
            buf = bytearray(length)
            buf[:] = [0xFF] * length
            buf[0] = report_id
            dev.send_feature_report(bytes(buf))
            time.sleep(0.050)
            rx2 = dev.get_feature_report(report_id, length)
            after1 = bytes(rx2)

            if baseline != after1:
                print(f"✓ CHANGED: {' '.join('%02X' % b for b in after1)}")
            else:
                print("(no change)")

            # Send test pattern 2: 0x00
            print(f"  Sending 0x00 pattern...", end=" ")
            buf = bytearray(length)
            buf[0] = report_id
            dev.send_feature_report(bytes(buf))
            time.sleep(0.050)
            rx3 = dev.get_feature_report(report_id, length)
            after2 = bytes(rx3)

            if baseline != after2 or after1 != after2:
                print(f"✓ CHANGED: {' '.join('%02X' % b for b in after2)}")
            else:
                print("(no change)")

            # Send test pattern 3: alternating 0xAA 0x55
            print(f"  Sending alternating pattern...", end=" ")
            buf = bytearray(length)
            for i in range(length):
                buf[i] = 0xAA if i % 2 == 0 else 0x55
            buf[0] = report_id
            dev.send_feature_report(bytes(buf))
            time.sleep(0.050)
            rx4 = dev.get_feature_report(report_id, length)
            after3 = bytes(rx4)

            if baseline != after3:
                print(f"✓ CHANGED: {' '.join('%02X' % b for b in after3)}")
            else:
                print("(no change)")

            dev.close()

        except Exception as e:
            print(f"  Error: {e}")

        print()

    return 0


# PAYLOAD LENGTHS MUST MATCH phase-a/PROTOCOL.md §4.2/§4.3 EXACTLY — see the
# hazard comment above LED_TEST_CASES. Opcode 0x04 needs 3 bytes (§4.2,
# +0xC6/+0xC7/+0xC8), not 1 — the previous "04 00".."04 04" entries here were
# undersized the same way the LED sweep's "04 01" was, and are the leading
# suspects for the real-hardware hang. Opcode 0x06 needs 2 bytes (§4.3), not
# 1 — "06 00" below was also undersized before this fix.
#
# CONFIRMED HAZARD (real hardware, 2026-08-20): "03 FF" used to be in this
# list as "Alt DPI 3". Opcode 0x03 is NOT a DPI opcode — it's the LED
# preset/mode selector confirmed via LED_TEST_CASES, with a valid index
# range of roughly 0x00-0x17. Sending 0xFF (way out of that range) crashed
# the mouse — flashed cyan, then stopped responding. Opcode 0x03 has been
# removed from this list entirely; it never belonged here. If you want to
# keep exploring 0x03's valid range, do it via `led`, not `dpi`, and stay
# near the confirmed 0x00-0x1C range — don't jump to arbitrary large values
# like 0xFF on any opcode without a documented reason to expect it's valid.
#
# UPDATE 2026-08-20: mounting evidence (from findings.jsonl) that the whole
# "short-command class" — 0x01, 0x02, 0x03, 0x06 — is LED-related, not DPI:
# 0x03 is confirmed LED preset, 0x02 shows no differentiation by payload,
# and 0x01 (00-03) all logged "purple breathing" in an earlier `led` run.
# Opcode 0x04 is the one with real static-analysis backing as a distinct
# settings write (its own 3-byte buffer/call site, PROTOCOL.md §4.2) — so
# this sweep now focuses there, with LARGE, widely-spaced steps (not tiny
# +1 increments) so a real DPI change is much easier to notice/measure,
# while still staying well short of the 0xFF that caused a crash on a
# different opcode. Each byte of the 3-byte payload is tested independently
# in case DPI lives in a byte other than the first. `cmd_dpi` also measures
# actual movement-report magnitude via drag testing (see _measure_drag) so
# you don't have to rely on how the cursor "feels".
DPI_TEST_CASES = [
    ("04 00 00 00", "byte0=0x00 (baseline)"),
    ("04 01 00 00", "byte0=0x01"),
    ("04 04 00 00", "byte0=0x04 — bigger step"),
    ("04 08 00 00", "byte0=0x08"),
    ("04 10 00 00", "byte0=0x10"),
    ("04 20 00 00", "byte0=0x20"),
    ("04 40 00 00", "byte0=0x40"),
    ("04 00 04 00", "byte1=0x04 instead — testing the 2nd payload byte"),
    ("04 00 10 00", "byte1=0x10"),
    ("04 00 40 00", "byte1=0x40"),
    ("04 00 00 04", "byte2=0x04 instead — testing the 3rd payload byte"),
    ("04 00 00 10", "byte2=0x10"),
    ("04 00 00 40", "byte2=0x40"),
    # 0xF5 deliberately excluded — send it manually and deliberately only,
    # once every preceding write is already confirmed correct.
]


def cmd_dpi(args):
    """Explore DPI control commands, one at a time with a pause to observe,
    the option to resend the same command to check reproducibility, and an
    objective movement-magnitude measurement after each write (drag the
    mouse the same physical distance each time, compare the numbers instead
    of guessing by feel).

    DPI values typically: 800, 1600, 3200, 6400, 12800 (doubling).
    Opcodes: look for patterns in how DPI is encoded.

    SAFETY: see cmd_led's docstring — the automated sweep used to end with
    opcode 0xF5 and that hung the mouse on real hardware (recoverable via the
    physical power switch, but avoid repeating it). This pauses before every
    write and never sends 0xF5 automatically.
    """
    devices = unique_interfaces(find_all_devices())
    d = next((x for x in devices if x.get("interface_number") == VENDOR_IFACE), None)
    if not d:
        print("No compatible device found.")
        return 1
    d0 = next((x for x in devices if x.get("interface_number") == 0), None)

    dev = open_device(d)
    dev0 = None
    if d0:
        dev0 = open_device(d0)
        dev0.set_nonblocking(1)
    else:
        print("Warning: interface 0 (mouse movement reports) not found — "
              "falling back to subjective observation only, no measurement.\n")

    print("Exploring DPI control...")
    print("  After each write, drag the mouse the SAME physical distance")
    print("  every time (e.g. one full mousepad-width swipe) — the tool")
    print("  measures the reported movement magnitude so you can compare")
    print("  numbers across steps instead of guessing by feel.")
    print("  'r' resends the same command to confirm reproducibility.")
    print("  If the mouse ever stops responding, press 'q' at the next")
    print("  prompt and power-cycle it (the 3-position switch underneath).\n")

    for hex_cmd, desc in DPI_TEST_CASES:
        result = _confirm_step(desc, hex_cmd)
        if result == "stop":
            print("Stopped by user.")
            break
        if result == "skip":
            continue
        if _send_with_retry(dev, hex_cmd, desc, settle=0.3,
                             source="dpi", connection=args.connection,
                             measure_dev0=dev0) == "stop":
            print("Stopped by user.")
            break

    dev.close()
    if dev0:
        dev0.close()
    print("\nDone. Compare the logged magnitudes: run "
          "`python3 empirical_probe.py findings --opcode 04` to review.")
    return 0


GUIDE_TEXT = """
================================================================================
 KIMURA BUTTON/PROTOCOL DISCOVERY — WORKFLOW GUIDE
================================================================================

GOAL: figure out the exact bytes the mouse expects for button remapping, DPI,
and LED, so phase-a/kimura.py can drive the mouse without the vendor's
Windows app.

GOOD NEWS — the transport is CONFIRMED. Run `read-all` and you'll see
opcodes 0x81, 0x86, 0x82, 0x83, 0x84 echo back exactly as PROTOCOL.md
predicted, on interface 1 / report_id 7 / 8-byte buffer. That means
buf[0]=report_id, buf[1]=opcode framing is real and working — you have a
live, verified channel to the device today, no VM required for THIS part.

WHAT'S STILL UNKNOWN — the *payload encodings*: which bit in which config
block is DPI, which is an LED channel, which is a button slot, and what
opcode actually WRITES them (the LED/DPI opcodes in `led`/`dpi` below are
still guesses from the "short-command class" in PROTOCOL.md §4.3 — none
have been confirmed to visibly change the mouse yet). That's what the rest
of this guide is for. It requires DYNAMIC capture, because static analysis
only gives you the shape of the protocol, not live byte values.

--------------------------------------------------------------------------------
STEP 0 — Confirm the transport (one-time, already done for you)
--------------------------------------------------------------------------------
  python3 empirical_probe.py descriptor
  python3 empirical_probe.py read-all

  `descriptor` dumps each interface's real HID report descriptor — the
  vendor-defined interface (usage_page 0xFF01, interface_number=1) is the one
  with a Feature report: report ID 7, 8-byte buffer. `read-all` proves that
  channel is live by round-tripping the documented read-all-settings opcode
  sequence and confirming the opcode echoes back. Everything in this script
  now defaults to report_id=7/len=8 instead of the old wrong guess of
  report_id=0/len=65.

--------------------------------------------------------------------------------
STEP 1 — Best option: capture the REAL vendor GUI traffic (Windows VM)
--------------------------------------------------------------------------------
This is the reliable path — it's what phase-b was designed for.

  1. Set up a Windows VM with USB passthrough for the Kimura mouse
     (VirtualBox/VMware/QEMU all support USB passthrough).
  2. Install the vendor app from the MSI in this repo
     ("MS-4300WG KIMURA MOUSE(5).msi").
  3. Inside the VM, start a USB capture BEFORE touching the GUI:
       - USBPcap + Wireshark (Windows-native), or
       - Frida-hook HidD_SetFeature/HidD_GetFeature for cleaner structured
         output instead of raw USB frames.
  4. In the vendor GUI, change EXACTLY ONE setting at a time, e.g.:
       - Remap Left button -> "Disable"
       - Remap Left button -> "Right click"
       - Remap Forward button -> a single key ("A")
       - Set LED to solid red, then solid green, then off
       - Set DPI to 800, then 1600, then 3200
     Stop/start the capture (or bookmark the timestamp) around each change so
     you know which USB frames correspond to which UI action. This is the
     "differential method" — one variable changes at a time so the opcode
     and payload byte(s) responsible are unambiguous.
  5. In the capture, filter for Feature-report SET_REPORT control transfers
     (bRequest 0x09, wValue high byte 0x03) on interface 1, report ID 7 —
     that's the frame carrying the opcode + payload you're after. (Our own
     earlier capture from this Linux box, kimura_usb_capture_*.pcap, had
     ZERO of these — that's why nothing changed. You need this from the
     Windows session where the GUI actually issues the write.)
  6. Feed the recovered opcode/payload back into:
       python3 empirical_probe.py send "<opcode> <payload>"
     to verify it reproduces the same behavior on Linux.

--------------------------------------------------------------------------------
STEP 2 — Fallback: differential probing directly on Linux (no VM)
--------------------------------------------------------------------------------
Slower and noisier, but works if a VM isn't available. Do ONE variable at a
time and watch the physical mouse (LED / cursor speed / button behavior)
after each command:

  # Start a capture in another terminal first:
  sudo tcpdump -i usbmon3 -w /tmp/kimura_capture_$(date +%s).pcap
  # (find the right usbmonN with: cat /sys/kernel/debug/usb/usbmon/0u | head,
  #  or `usbmon` via `sudo modprobe usbmon && ls /sys/kernel/debug/usb/usbmon`)

  python3 empirical_probe.py led            # steps through plausible LED opcodes, pausing for your OK before each
  python3 empirical_probe.py dpi            # same, for DPI opcodes — press 'q' anytime to stop if something looks wrong
  python3 empirical_probe.py send "80"      # try version-read opcode alone
  python3 empirical_probe.py read-all       # safe: sweeps read opcodes, reports opcode-echo matches

  Then inspect the capture with tshark, e.g.:
    tshark -r /tmp/kimura_capture_*.pcap -Y "usb.setup.bRequest==9" \\
      -T fields -e frame.number -e usb.setup.wValue -e usb.capdata

--------------------------------------------------------------------------------
STEP 3 — Buttons specifically
--------------------------------------------------------------------------------
Static analysis of the vendor GUI (see MSI-extracted binary,
class CMacroSetDlg/CIrrButton) confirms buttons+macros are written via
opcode 0x07 as a 288-byte table: 72 entries of 4 bytes, where the first byte
of each entry is a type tag (0x80 = built-in action like click/DPI/volume,
0x00 = disabled, 0x04 = key/macro reference — exact field layout still
unconfirmed). This upload path is intentionally NOT wired up for writing in
this script (see CLAUDE.md safety notes on opcodes 0x05/0x07) until a real
capture confirms the byte layout — writing an unverified 288-byte table
risks putting the button config into a broken state.

  python3 empirical_probe.py buttons   # safe, read-only baseline

To actually learn the table layout: in the Windows GUI, remap ONE button at
a time (e.g. Left -> Disable, then Left -> Right-click, then Forward ->
Volume Up) and capture each 288-byte Output-report write. Diff consecutive
captures — the 4-byte entry that changed tells you which table slot maps to
which physical button, and what the entry encoding looks like for each
action type.

--------------------------------------------------------------------------------
Reference
--------------------------------------------------------------------------------
  phase-a/PROTOCOL.md   — full opcode table from static analysis
  KIMURA_RE_STRATEGY.md — overall phase plan and hazards
  CLAUDE.md             — safety notes (0xFA, 0x05, 0x07 are gated on purpose)
================================================================================
"""


def cmd_guide(args):
    print(GUIDE_TEXT)
    return 0


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # guide
    pg = sub.add_parser("guide", help="Print the step-by-step discovery workflow (read this first)")
    pg.set_defaults(func=cmd_guide)

    # list
    pl = sub.add_parser("list", help="List all 0x248A HID devices")
    pl.set_defaults(func=cmd_list)

    # descriptor
    pdsc = sub.add_parser("descriptor", help="Dump/decode HID report descriptors (ground truth for report IDs/lengths)")
    pdsc.set_defaults(func=cmd_descriptor)

    # read-all
    pra = sub.add_parser("read-all", help="Safe: sweep report IDs/lengths trying documented read opcodes")
    pra.set_defaults(func=cmd_read_all)

    # buttons
    pb = sub.add_parser("buttons", help="Safe, read-only exploration of the vendor Feature channel for button config")
    pb.set_defaults(func=cmd_buttons)

    # capture-buttons
    pcb = sub.add_parser("capture-buttons", help="Safe, read-only: watch Input reports live while pressing buttons, to map bits to physical buttons")
    pcb.set_defaults(func=cmd_capture_buttons)

    # send
    ps = sub.add_parser("send", help="Send raw hex command")
    ps.add_argument("hex", help="Hex bytes, e.g. '80' or '04 01 02'")
    ps.add_argument("--iface", type=int, default=None, help=f"Interface number (default {VENDOR_IFACE}, the vendor channel)")
    ps.add_argument("--report-id", type=int, default=VENDOR_REPORT_ID, help=f"Report ID (default {VENDOR_REPORT_ID})")
    ps.add_argument("--length", type=int, default=VENDOR_FEATURE_LEN, help=f"Feature report length incl. report-ID byte (default {VENDOR_FEATURE_LEN})")
    ps.add_argument("--connection", default="wired", choices=["wired", "dongle"], help="Physical connection mode for this session (default: wired) — recorded in the findings log")
    ps.add_argument("--note", default=None, help="Observation text to log immediately, non-interactively (e.g. for scripting). If omitted and running interactively, you'll be prompted.")
    ps.add_argument("--measure", action="store_true", help="After sending, measure drag-distance movement magnitude via interface 0 (same as `dpi`'s measurement) and log it automatically")
    ps.set_defaults(func=cmd_send)

    # test-write
    pt = sub.add_parser("test-write", help="Test if device responds to writes")
    pt.set_defaults(func=cmd_test_write)

    # led
    pl2 = sub.add_parser("led", help="Probe LED control commands")
    pl2.add_argument("--connection", default="wired", choices=["wired", "dongle"], help="Physical connection mode for this session (default: wired) — recorded in the findings log")
    pl2.set_defaults(func=cmd_led)

    # dpi
    pd = sub.add_parser("dpi", help="Probe DPI control commands")
    pd.add_argument("--connection", default="wired", choices=["wired", "dongle"], help="Physical connection mode for this session (default: wired) — recorded in the findings log")
    pd.set_defaults(func=cmd_dpi)

    # measure
    pm = sub.add_parser("measure", help="Safe, read-only: measure current drag-distance movement magnitude, no write at all")
    pm.add_argument("--seconds", type=int, default=4, help="How long to measure for (default 4)")
    pm.add_argument("--connection", default="wired", choices=["wired", "dongle"], help="Physical connection mode for this session (default: wired) — recorded in the findings log")
    pm.add_argument("--note", default=None, help="Observation text to log immediately, non-interactively")
    pm.set_defaults(func=cmd_measure)

    # dpi-diff
    pdd = sub.add_parser("dpi-diff", help="Safe, read-only: snapshot config reads before/after a physical DPI change, diff for clues")
    pdd.add_argument("--connection", default="wired", choices=["wired", "dongle"], help="Physical connection mode for this session (default: wired) — recorded in the findings log")
    pdd.set_defaults(func=cmd_dpi_diff)

    # findings
    pf = sub.add_parser("findings", help="Print the logged findings (phase-b/findings.jsonl) as a readable table")
    pf.add_argument("--opcode", default=None, help="Filter to one opcode, e.g. '03' or '0x03'")
    pf.set_defaults(func=cmd_findings)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
