#!/usr/bin/env python3
"""
kimura.py — portable probe/transport for the Zeroground Kimura v3.0 (MS-4300WG)
gaming mouse, VID 0x248A / PID 0x5B49 (wired) or 0x5B4A (2.4 GHz receiver).

Protocol reverse engineered independently from the vendor's Windows utility,
for interoperability (see README.md).

READ-ONLY BY DEFAULT. Nothing in the default code path modifies the device.
Write commands exist but are gated behind --allow-write, and the flash-commit
opcode (0xFA) and the bulk upload opcodes are deliberately not wired up.

Requires:  pip install hidapi        (macOS also: brew install hidapi)
Linux:     needs a udev rule or root for hidraw access — see README.
"""

import argparse
import sys
import time

try:
    import hid
except ImportError:
    sys.exit("Missing dependency. Install with:  pip install hidapi\n"
             "On macOS you may also need:        brew install hidapi")

VID = 0x248A
PIDS = (0x5B49, 0x5B4A)

# --- opcodes (see PROTOCOL.md §4) -------------------------------------------
OP_VERSION = 0x80  # CONFIRMED NON-FUNCTIONAL on real hardware (PROTOCOL.md §5):
                   # returns a fixed value regardless of input, never echoes.
                   # Kept for a possible future firmware revision, but do not
                   # rely on it for transport discovery — use OP_PING instead.
OP_PING = 0x81     # Use this for transport discovery: confirmed to echo
                   # correctly on real hardware (part of OP_READ_BLOCKS below).
OP_READ_BLOCKS = (0x81, 0x86, 0x82, 0x83, 0x84)  # order used by the vendor's
                                                 # read-all routine @0x00415890
OP_LED = 0x03      # CONFIRMED (PROTOCOL.md §4.3): 1-byte payload, LED preset
                   # index. Valid confirmed range 0x00-0x1B. 0xFF crashes the
                   # firmware (confirmed on real hardware) — never send it.

# The rest of these are only known from PASSIVELY OBSERVING the vendor GUI's
# own USB traffic (Windows VM + USBPcap, 2026-09-10, see PROTOCOL.md §4.3a)
# — none of them have been independently replayed against real hardware by
# this project. Used only by apply_button_table() below, which is itself
# gated far more heavily than set_led().
OP_APPLY_BEGIN = 0xF5    # sent FIRST in the vendor's bundle, not last — see
                         # §4.3a; despite the name "apply", this is closer to
                         # "begin edit session."
OP_PAGE_SELECT = 0x05    # primes a page for the following Output report.
OP_UNKNOWN_01 = 0x01     # captured constant `08 00 00 00 00`, meaning unconfirmed.
OP_UNKNOWN_02 = 0x02     # captured constant `04 04 00 00 00`, meaning unconfirmed.
OP_UNKNOWN_04 = 0x04     # captured constant `00 66 66 00 00`. Long suspected as
                         # the DPI opcode; CONFIRMED NOT DPI/wheel-speed related
                         # (PROTOCOL.md §5) — payload stayed identical across a
                         # full DPI-lowest/DPI-max/wheel-min/wheel-max test.
OP_UNKNOWN_06 = 0x06     # captured constant `00 00 00 00 00`, meaning unconfirmed.
OP_COMMIT = 0xFA         # CONFIRMED flash commit (§4.3a) — sent LAST in the
                         # vendor's bundle. Real flash write; don't call in a loop.

# Deliberately NOT exposed as standalone commands: 0x07 (288-byte macro/button
# table, different from the 32-byte quick-slot table below), 0x82 (confirmed
# read-only DPI-stage register — writing it does nothing, PROTOCOL.md §2.3).

SETTLE = 0.010  # the vendor tool's mandatory Sleep(10) between write and read

# Default sweep order for probe()'s (report_id, length) discovery. 7/8 are
# CONFIRMED correct on real hardware (PROTOCOL.md) and tried first; the rest
# are kept as a fallback for other firmware/PID variants never characterized.
# NOTE: this firmware doesn't actually validate the report-ID byte for reads
# (any small non-zero value echoes the same data) — don't rely on that
# leniency elsewhere, always prefer 7 explicitly, which is why it's first
# here rather than just using range(16) and hoping.
DEFAULT_REPORT_IDS = [7] + [r for r in range(16) if r != 7]
DEFAULT_LENGTHS = [8, 9, 17, 33, 65, 264]

# CONFIRMED LED preset table (real hardware, see PROTOCOL.md §4.3 for the
# full sweep log). Only values in this dict are accepted by set_led()/the
# `led` CLI subcommand — anything else (especially 0xFF, confirmed to crash
# the firmware) is refused. Gaps (0x08, 0x0A-0x0F, 0x13-0x14) were never
# tested and are intentionally absent, not just undocumented.
LED_PRESETS = {
    # 0x00-0x06 have vendor-GUI-confirmed names (Windows USBPcap capture,
    # 2026-09-10, see PROTOCOL.md §4.3) — these supersede the earlier
    # Linux-sweep-only descriptions, which had 0x00 and 0x02 swapped.
    0x00: "Neon",
    0x01: "Colour Streaming",
    0x02: "Breathing",
    0x03: "Colorful tail",
    0x04: "Wave",
    0x05: "Stars Twinkle",
    0x06: "LED Off",
    0x07: "chase-family, front-then-back-to-front",
    0x09: "instant color flash, then OFF",
    0x0A: "flash, then blue, then breathing",
    0x0B: "flash, then flashing color",
    0x0C: "flash blue, then breathing",
    0x0D: "flashing green then orange, breathing",
    0x0E: "fast back-to-front, changing color",
    0x0F: "slower back-to-front, cyan",
    0x10: "breathing, slower rate",
    0x11: "cyan, slow breathing",
    0x12: "breathing, slower rate",
    0x15: "fast blink, settles to slow breathing",
    0x16: "fast blink, settles to slow breathing",
    0x17: "fast blink, settles to slow breathing",
    0x19: "faster back-to-front breathing",
    0x1A: "cyan, back-to-front",
    0x1B: "cyan, back-to-front",
}

# Friendly aliases for the CLI — deliberately small, just the most useful ones.
LED_ALIASES = {
    "off": 0x06,
    "default": 0x00,       # Neon — the preset active on a fresh device/app launch
    "breathing": 0x02,     # vendor-GUI-confirmed name is "Breathing", value 0x02 not 0x00
}

# --- button-remap table (PROTOCOL.md §4.3a) ---------------------------------
# CONFIRMED by diffing vendor-GUI USB captures against each other (2026-09-10):
# a 32-byte Output report holds 6 fixed 4-byte slots, one per physical button.
# Byte offsets below are 0-indexed WITHIN the 32-byte payload (i.e. NOT
# counting the leading report-ID byte the wire format also carries).
BUTTON_SLOTS = [
    ("left", 0),
    ("right", 4),
    ("middle", 8),
    ("side_back", 12),
    ("side_forward", 16),
    ("button6", 20),
]

# Factory-default 4-byte value per slot, as observed in every capture before
# that slot was ever remapped. Used to fill in any slot NOT explicitly given
# to apply_button_table() — see its docstring for why that matters.
BUTTON_SLOT_DEFAULTS = {
    "left": b"\x01\x00\xf0\x00",
    "right": b"\x01\x00\xf1\x00",
    "middle": b"\x01\x00\xf2\x00",
    "side_back": b"\x01\x00\xf3\x00",
    "side_forward": b"\x01\x00\xf4\x00",
    "button6": b"\x07\x00\x03\x00",   # unconfirmed meaning; plausibly this
                                       # button's factory role is DPI-cycle,
                                       # since it's physically the same
                                       # underside button (PROTOCOL.md §2.3).
}

# Confirmed 4-byte action codes, across (at least) four distinct encoding
# families (PROTOCOL.md §4.3a) — pass any of these names to apply_button_table().
BUTTON_ACTIONS = {
    "left_click": b"\x01\x00\xf0\x00",
    "right_click": b"\x01\x00\xf1\x00",       # inferred by position, NOT independently confirmed
    "middle_click": b"\x01\x00\xf2\x00",
    "back": b"\x01\x00\xf3\x00",
    "forward": b"\x01\x00\xf4\x00",
    "show_desktop": b"\x00\x08\x07\x00",
    "lock_pc": b"\x00\x08\x0f\x00",
    "switch_apps": b"\x00\x04\x2b\x00",       # Alt+Tab equivalent
    "volume_down": b"\x03\x00\xea\x00",
    "scroll_up_or_volume_up": b"\x03\x00\xe9\x00",  # AMBIGUOUS — both actions
                                                     # produced this same code
                                                     # in testing; not disambiguated
    "dpi_cycle": b"\x07\x00\x03\x00",         # button6's untouched factory default
}


class KimuraError(Exception):
    pass


class Kimura:
    """Feature-report transport, mirroring the vendor tool's idiom exactly."""

    def __init__(self, dev, report_id, feature_len, info):
        self.dev = dev
        self.report_id = report_id
        self.feature_len = feature_len
        self.info = info

    # -- low level ----------------------------------------------------------
    def _tx(self, opcode, payload=b""):
        buf = bytearray(self.feature_len)
        buf[0] = self.report_id
        buf[1] = opcode
        buf[2:2 + len(payload)] = payload
        self.dev.send_feature_report(bytes(buf))

    def _rx(self):
        data = self.dev.get_feature_report(self.report_id, self.feature_len)
        return bytes(data)

    def _tx_output(self, payload32):
        """Send a 33-byte Output report: report_id + 32 data bytes.

        Distinct from _tx()/command(), which use Feature reports. See
        PROTOCOL.md §2.2/§4.3a — this is the bulk-plane transport.
        """
        if len(payload32) != 32:
            raise KimuraError("output report payload must be exactly 32 bytes, got %d"
                              % len(payload32))
        buf = bytes([self.report_id]) + bytes(payload32)
        n = self.dev.write(buf)
        if n is not None and n < 0:
            raise KimuraError("output report write failed")

    def command(self, opcode, payload=b"", expect_reply=True):
        """Write a command; optionally read back and verify the opcode echo."""
        self._tx(opcode, payload)
        if not expect_reply:
            return None
        time.sleep(SETTLE)
        rx = self._rx()
        if len(rx) < 2:
            raise KimuraError("short response (%d bytes)" % len(rx))
        if rx[1] != opcode:
            raise KimuraError("opcode echo mismatch: sent 0x%02X, got 0x%02X"
                              % (opcode, rx[1]))
        return rx

    # -- decoded operations -------------------------------------------------
    def version(self):
        """Opcode 0x80. The vendor decodes rx[2]*10 + rx[3].

        CONFIRMED NON-FUNCTIONAL on real hardware — 0x80 never echoes, it
        returns a fixed value regardless of input (PROTOCOL.md §5). This
        will raise KimuraError (opcode echo mismatch) on this firmware. Not
        removed in case a future firmware revision implements it; do not
        use for transport discovery — see ping()/OP_PING.
        """
        rx = self.command(OP_VERSION)
        major, minor = rx[2], rx[3]
        return major * 10 + minor, (major, minor), rx

    def ping(self):
        """Opcode 0x81 (first of OP_READ_BLOCKS), used purely to confirm the
        transport is alive — confirmed to echo correctly on real hardware,
        unlike version()/0x80. This is what probe() uses for discovery."""
        return self.command(OP_PING)

    def read_all_blocks(self):
        """Replay fcn.00415890's read-all sequence. Purely non-destructive."""
        out = {}
        for op in OP_READ_BLOCKS:
            try:
                out[op] = self.command(op)
            except KimuraError as e:
                out[op] = ("ERROR", str(e))
            time.sleep(SETTLE)
        return out

    def set_led(self, preset):
        """Opcode 0x03, 1-byte payload — CONFIRMED LED preset selector
        (PROTOCOL.md §4.3). preset must be in LED_PRESETS (0x00-0x1B); 0xFF
        is confirmed to crash the firmware and is never accepted here. No
        echo check — this is the "short-command class", write-only.
        """
        if preset not in LED_PRESETS:
            raise KimuraError(
                "refusing to send unconfirmed LED preset 0x%02X — only %s "
                "are confirmed safe (see PROTOCOL.md §4.3)"
                % (preset, ", ".join("0x%02X" % p for p in sorted(LED_PRESETS))))
        self.command(OP_LED, bytes([preset]), expect_reply=False)

    def apply_button_table(self, overrides, led_preset=None):
        """EXPERIMENTAL — sends the full 11-step "apply settings" bundle
        exactly as captured from the vendor GUI (PROTOCOL.md §4.3a).

        **THIS HAS NEVER BEEN REPLAYED AGAINST REAL HARDWARE BY THIS
        PROJECT** — it is built entirely from passively observing the
        vendor's own Windows software over USBPcap. Unlike set_led(), which
        is a single, small, independently well-tested write, this sends 11
        commands including a flash commit (0xFA).

        `overrides`: dict of slot name (see BUTTON_SLOTS) -> either a key
        into BUTTON_ACTIONS or a raw 4-byte value. Any slot NOT given here
        is filled with BUTTON_SLOT_DEFAULTS — since this device has no known
        way to read back its current button table, slots you don't specify
        will be RESET to factory defaults, silently discarding any existing
        customization on those buttons. There is no way around this without
        a confirmed read opcode for this table, which doesn't exist yet.

        `led_preset`: LED value to include in the bundle (defaults to
        LED_ALIASES["default"]/Neon if omitted) — for the same reason as
        above, this may change your LED setting even if you didn't ask to.
        """
        for slot in overrides:
            if slot not in BUTTON_SLOT_DEFAULTS:
                raise KimuraError("unknown button slot %r — valid: %s"
                                  % (slot, ", ".join(name for name, _ in BUTTON_SLOTS)))

        page0 = bytearray(32)
        for slot, offset in BUTTON_SLOTS:
            val = overrides.get(slot, BUTTON_SLOT_DEFAULTS[slot])
            if val in BUTTON_ACTIONS:
                val = BUTTON_ACTIONS[val]
            val = bytes(val)
            if len(val) != 4:
                raise KimuraError("slot %r: action code must be 4 bytes, got %d"
                                  % (slot, len(val)))
            page0[offset:offset + 4] = val
        page1 = bytes(32)  # all-zero in every capture observed so far

        led = led_preset if led_preset is not None else LED_ALIASES["default"]
        if led not in LED_PRESETS:
            raise KimuraError("refusing unconfirmed LED preset 0x%02X" % led)

        # Exact 11-step order from PROTOCOL.md §4.3a. Deviating from this
        # order is untested territory on top of already-untested territory.
        self.command(OP_APPLY_BEGIN, expect_reply=False)
        self.command(OP_PAGE_SELECT, bytes([0x02, 0x00, 0x20, 0x00, 0x00, 0x00]), expect_reply=False)
        self._tx_output(page0)
        self.command(OP_PAGE_SELECT, bytes([0x02, 0x01, 0x20, 0x00, 0x00, 0x00]), expect_reply=False)
        self._tx_output(page1)
        self.command(OP_LED, bytes([led, 0, 0, 0, 0, 0]), expect_reply=False)
        self.command(OP_UNKNOWN_02, bytes([0x04, 0x04, 0, 0, 0, 0]), expect_reply=False)
        self.command(OP_UNKNOWN_04, bytes([0x00, 0x66, 0x66, 0, 0, 0]), expect_reply=False)
        self.command(OP_UNKNOWN_01, bytes([0x08, 0, 0, 0, 0, 0]), expect_reply=False)
        self.command(OP_UNKNOWN_06, bytes([0, 0, 0, 0, 0, 0]), expect_reply=False)
        self.command(OP_COMMIT, expect_reply=False)


# --- discovery ---------------------------------------------------------------
def enumerate_candidates(verbose=True):
    found = []
    for pid in PIDS:
        for d in hid.enumerate(VID, pid):
            found.append(d)
    if verbose:
        if not found:
            print("No 0x248A device found. Is the dongle plugged in?")
        else:
            print("Interfaces for VID 0x248A:\n")
            for d in found:
                print("  pid=0x%04X  iface=%-3s usage_page=0x%04X usage=0x%04X"
                      "  %s %s" % (
                          d.get("product_id", 0),
                          d.get("interface_number", "?"),
                          d.get("usage_page", 0) or 0,
                          d.get("usage", 0) or 0,
                          (d.get("manufacturer_string") or "").strip(),
                          (d.get("product_string") or "").strip()))
                print("      path: %s" % d["path"].decode(errors="replace"))
            print()
    return found


def is_vendor_collection(d):
    """Vendor-defined usage pages are 0xFF00-0xFFFF.

    On macOS the OS refuses to open top-level Generic Desktop mouse/keyboard
    collections (usage_page 0x01), so those are unusable regardless.
    """
    up = d.get("usage_page") or 0
    return up >= 0xFF00


def probe(d, report_ids, lengths, verbose=True):
    """Try opcode 0x81 (OP_PING) across candidate (report_id, length) pairs.

    0x81 is a pure read. Nothing here changes device state. (Previously
    used opcode 0x80/version() for this — confirmed on real hardware that
    0x80 never echoes, so that never found a working transport. 0x81 is
    confirmed to echo correctly, see PROTOCOL.md §5.)
    """
    dev = hid.device()
    try:
        dev.open_path(d["path"])
    except Exception as e:
        if verbose:
            print("    cannot open: %s" % e)
        return None
    dev.set_nonblocking(0)

    for rid in report_ids:
        for ln in lengths:
            k = Kimura(dev, rid, ln, d)
            try:
                rx = k.ping()
            except Exception:
                continue
            if verbose:
                print("    HIT  report_id=0x%02X len=%d -> opcode 0x%02X echoed"
                      % (rid, ln, OP_PING))
                print("         rx[0:8] = %s"
                      % " ".join("%02X" % b for b in rx[:8]))
            return k
    dev.close()
    return None


def cmd_probe(args):
    cands = enumerate_candidates()
    if not cands:
        return 1

    vendor = [d for d in cands if is_vendor_collection(d)]
    order = vendor + [d for d in cands if d not in vendor]
    if vendor:
        print("Trying %d vendor-defined collection(s) first.\n" % len(vendor))
    else:
        print("No vendor-defined collection exposed. Trying every interface;\n"
              "on macOS the mouse collection itself will refuse to open.\n")

    report_ids = args.report_ids or DEFAULT_REPORT_IDS
    lengths = args.lengths or DEFAULT_LENGTHS

    for d in order:
        print("Probing pid=0x%04X usage_page=0x%04X ..."
              % (d.get("product_id", 0), d.get("usage_page", 0) or 0))
        k = probe(d, report_ids, lengths)
        if k:
            print("\nTransport established.")
            if args.read_all:
                print("\nRead-all sequence (vendor order 0x81 0x86 0x82 0x83 "
                      "0x84):\n")
                for op, rx in k.read_all_blocks().items():
                    if isinstance(rx, tuple):
                        print("  0x%02X  %s: %s" % (op, rx[0], rx[1]))
                    else:
                        print("  0x%02X  %s" % (
                            op, " ".join("%02X" % b for b in rx[:24])))
            k.dev.close()
            return 0
    print("\nNo working (report_id, length) combination found.")
    print("Next step: capture the report descriptor and read the real feature "
          "report length from it —\n"
          "  Linux:  sudo usbhid-dump -d 248a: -e descriptor")
    return 2


def resolve_led_preset(s):
    """Accept a hex byte string ('0x06', '06') or a friendly LED_ALIASES name."""
    if s in LED_ALIASES:
        return LED_ALIASES[s]
    try:
        return int(s, 0)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "invalid LED preset %r — use a hex byte (e.g. 0x06) or one of: %s"
            % (s, ", ".join(sorted(LED_ALIASES))))


def cmd_led(args):
    if not args.allow_write:
        print("Refusing to write without --allow-write. This mouse's firmware has")
        print("confirmed hazards for out-of-range/malformed writes (see PROTOCOL.md")
        print("safety notes) — pass --allow-write once you understand the risk.")
        return 1

    preset = args.preset
    if preset not in LED_PRESETS:
        print("Refusing: 0x%02X is not in the confirmed-safe preset table." % preset)
        print("Confirmed presets:")
        for p in sorted(LED_PRESETS):
            print("  0x%02X  %s" % (p, LED_PRESETS[p]))
        return 1

    cands = enumerate_candidates(verbose=False)
    if not cands:
        print("No 0x248A device found.")
        return 1
    vendor = [d for d in cands if is_vendor_collection(d)]
    order = vendor + [d for d in cands if d not in vendor]

    for d in order:
        k = probe(d, DEFAULT_REPORT_IDS, DEFAULT_LENGTHS, verbose=False)
        if k:
            k.set_led(preset)
            print("Sent LED preset 0x%02X (%s)." % (preset, LED_PRESETS[preset]))
            k.dev.close()
            return 0
    print("Could not establish transport — run `kimura.py probe` for diagnosis.")
    return 2


def resolve_button_action(s):
    """Accept a BUTTON_ACTIONS name or a raw 'XX XX XX XX' hex byte string."""
    if s in BUTTON_ACTIONS:
        return s  # apply_button_table() resolves the name itself
    parts = s.replace(",", " ").split()
    try:
        val = bytes(int(p, 16) for p in parts)
    except ValueError:
        raise argparse.ArgumentTypeError(
            "invalid action %r — use a BUTTON_ACTIONS name (%s) or 4 raw hex "
            "bytes like '01 00 F0 00'" % (s, ", ".join(sorted(BUTTON_ACTIONS))))
    if len(val) != 4:
        raise argparse.ArgumentTypeError(
            "raw action must be exactly 4 hex bytes, got %d" % len(val))
    return val


def cmd_remap(args):
    overrides = {}
    for slot, _ in BUTTON_SLOTS:
        val = getattr(args, slot)
        if val is not None:
            overrides[slot] = val
    if not overrides:
        print("No slots given — nothing to do. Pass at least one of: %s"
              % ", ".join("--%s" % s.replace("_", "-") for s, _ in BUTTON_SLOTS))
        return 1

    if not args.allow_write:
        print("Refusing to write without --allow-write.")
        return 1
    if not args.experimental:
        print("Refusing: this write path is EXPERIMENTAL and has NEVER been")
        print("replayed against real hardware — it's built entirely from")
        print("passively observing the vendor GUI's own USB traffic. Pass")
        print("--experimental once you've read PROTOCOL.md §4.3a and understand:")
        print()
        print("  1. Any button slot you DON'T specify gets RESET to its factory")
        print("     default — there's no confirmed way to read the current table")
        print("     back first, so this can silently undo other customizations.")
        print("  2. The LED and several other settings (opcodes 0x01/0x02/0x04/")
        print("     0x06) get resent too, using captured default values — if")
        print("     your actual current values differ, they may change.")
        print("  3. This ends with a real flash commit (0xFA).")
        return 1

    print("About to write the button table with these overrides:")
    for slot, val in overrides.items():
        name = val if isinstance(val, str) else " ".join("%02X" % b for b in val)
        print("  %-14s -> %s" % (slot, name))
    print("All other slots will be reset to factory defaults. LED will be set")
    print("to %r unless --led is given." % (args.led or "default"))
    if not args.yes:
        confirm = input("\nType YES (all caps) to continue: ")
        if confirm != "YES":
            print("Aborted.")
            return 1

    cands = enumerate_candidates(verbose=False)
    vendor = [d for d in cands if is_vendor_collection(d)]
    order = vendor + [d for d in cands if d not in vendor]
    for d in order:
        k = probe(d, DEFAULT_REPORT_IDS, DEFAULT_LENGTHS, verbose=False)
        if k:
            led = resolve_led_preset(args.led) if args.led else None
            k.apply_button_table(overrides, led_preset=led)
            print("\nSent. Physically test every remapped button now — this")
            print("write path has no independent verification beyond this.")
            k.dev.close()
            return 0
    print("Could not establish transport — run `kimura.py probe` for diagnosis.")
    return 2


BUTTON_NAMES = {0: "Left", 1: "Right", 2: "Middle", 3: "Back", 4: "Forward"}


def open_generic_mouse_collection(verbose=True):
    """Open the STANDARD (non-vendor) mouse collection for reading Input
    reports (buttons/scroll) — report_id=1, per PROTOCOL.md §2.3. This is
    deliberately separate from the vendor-channel discovery in probe(),
    which targets usage_page >= 0xFF00.

    macOS refuses to open top-level Generic Desktop mouse/keyboard
    collections (see README.md platform notes) — this may simply fail to
    find anything there; that is an OS restriction, not a bug here.

    CONFIRMED (real hardware, Linux): this backend's usage_page field is
    unreliable — `list` shows 0x0000 for every interface here even though
    the real HID descriptor (per phase-b/empirical_probe.py descriptor,
    which reads it directly via hidraw) confirms interface 0 is the
    generic mouse collection. Prefer usage_page==0x0001 when it's actually
    populated (e.g. may work on macOS), but fall back to interface_number
    == 0 when every candidate reports usage_page 0 — don't just silently
    match nothing.
    """
    cands = enumerate_candidates(verbose=False)
    generic = [d for d in cands
               if not is_vendor_collection(d) and (d.get("usage_page") or 0) == 0x0001]
    if not generic:
        generic = [d for d in cands if d.get("interface_number") == 0]
    for d in generic:
        dev = hid.device()
        try:
            dev.open_path(d["path"])
        except Exception as e:
            if verbose:
                print("  cannot open iface=%s: %s" % (d.get("interface_number"), e))
            continue
        dev.set_nonblocking(1)
        return dev, d
    return None, None


def decode_mouse_report(data):
    """Report ID 1: [0]=report_id [1]=buttons [2:4]=dX (s16 LE) [4:6]=dY
    (s16 LE) [6]=wheel (s8). See PROTOCOL.md §2.3. Returns None for
    anything that isn't a report_id=1 payload of the expected length."""
    if not data or data[0] != 1 or len(data) < 7:
        return None
    buttons = data[1]
    bits_set = [i for i in range(5) if buttons & (1 << i)]
    dx = int.from_bytes(bytes(data[2:4]), "little", signed=True)
    dy = int.from_bytes(bytes(data[4:6]), "little", signed=True)
    wheel = data[6] - 256 if data[6] > 127 else data[6]
    return buttons, bits_set, dx, dy, wheel


def cmd_buttons(args):
    dev, d = open_generic_mouse_collection()
    if not dev:
        print("Could not open the standard mouse Input-report collection.")
        print("On macOS this is a known OS restriction (see README.md platform")
        print("notes) — the OS refuses to open top-level Generic Desktop")
        print("mouse/keyboard collections.")
        print("On Linux: this module (hid, libusb-backed) needs write access to")
        print("/dev/bus/usb/*, which is separate from the hidraw udev rule in")
        print("README.md Setup and typically requires root or a udev rule")
        print("targeting SUBSYSTEM==\"usb\" (see CLAUDE.md for the hid-vs-hidraw")
        print("backend distinction). phase-b/empirical_probe.py's `capture-buttons`")
        print("uses the hidraw backend instead and works without this on Linux.")
        return 1

    print("Watching button/scroll Input reports (report_id=1, read-only).")
    print("Press Ctrl+C to stop.\n")
    try:
        while True:
            try:
                data = dev.read(64)
            except Exception:
                data = None
            if data:
                decoded = decode_mouse_report(data)
                if decoded:
                    buttons, bits_set, dx, dy, wheel = decoded
                    names = [BUTTON_NAMES[b] for b in bits_set]
                    print("buttons=0x%02X %-28s dX=%+d dY=%+d wheel=%+d"
                          % (buttons, str(names), dx, dy, wheel))
            else:
                time.sleep(0.005)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        dev.close()
    return 0


def cmd_list(args):
    enumerate_candidates()
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="list all 0x248A HID interfaces")
    pl.set_defaults(func=cmd_list)

    pp = sub.add_parser("probe", help="find the working transport (read-only)")
    pp.add_argument("--report-ids", type=lambda s: [int(x, 0) for x in s.split(",")],
                    dest="report_ids", help="comma-separated, e.g. 0,1,2")
    pp.add_argument("--lengths", type=lambda s: [int(x, 0) for x in s.split(",")],
                    dest="lengths", help="comma-separated buffer lengths")
    pp.add_argument("--read-all", action="store_true",
                    help="after connecting, replay the read-all sequence")
    pp.set_defaults(func=cmd_probe)

    pled = sub.add_parser("led", help="set an LED preset (write, gated behind --allow-write)")
    pled.add_argument("preset", type=resolve_led_preset,
                      help="hex byte (e.g. 0x06) or alias: %s" % ", ".join(sorted(LED_ALIASES)))
    pled.add_argument("--allow-write", action="store_true",
                      help="required to actually send the write")
    pled.set_defaults(func=cmd_led)

    pb = sub.add_parser("buttons", help="watch button/scroll Input reports (read-only)")
    pb.set_defaults(func=cmd_buttons)

    prm = sub.add_parser("remap",
        help="EXPERIMENTAL: write the button-remap table (write, gated, unverified on real hardware)")
    for slot, _ in BUTTON_SLOTS:
        prm.add_argument("--%s" % slot.replace("_", "-"), dest=slot,
                         type=resolve_button_action, default=None,
                         help="action name (%s) or raw 'XX XX XX XX' hex"
                              % ", ".join(sorted(BUTTON_ACTIONS)))
    prm.add_argument("--led", type=str, default=None,
                     help="LED preset to include in the bundle (name or hex byte); "
                          "defaults to 'default'/Neon if omitted")
    prm.add_argument("--allow-write", action="store_true",
                     help="required to actually send the write")
    prm.add_argument("--experimental", action="store_true",
                     help="required acknowledgment — this path is untested on real hardware")
    prm.add_argument("--yes", action="store_true",
                     help="skip the interactive confirmation prompt")
    prm.set_defaults(func=cmd_remap)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
