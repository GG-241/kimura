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
import logging
import os
import platform
import sys
import time
from logging.handlers import RotatingFileHandler

try:
    import hid
except ImportError:
    sys.exit("Missing dependency. Install with:  pip install hidapi\n"
             "On macOS you may also need:        brew install hidapi")

# Keep in sync with pyproject.toml's [project] version. Printed on startup
# and shown in the GUI sidebar — exists so a user (or us, debugging a report)
# can tell which build is actually running without a Terminal, since
# multiple install paths (pip, AppImage, DMG, install.sh's separate
# ~/Applications wrapper on macOS) can otherwise leave stale copies around.
__version__ = "1.1.0"

LOG_DIR = os.path.expanduser("~/.kimura")
LOG_FILE = os.path.join(LOG_DIR, "kimura.log")


def _setup_logging():
    """Log to ~/.kimura/kimura.log (rotating, 5 x 512KB), in addition to
    whatever a caller does with an exception (message box, print, etc).

    CONFIRMED NEED (2026-09-24): a real macOS bug report was hard to pin
    down without the exact error text, and the GUI's --windowed/frozen
    builds have no visible console at all unless a user happens to know to
    launch the .app's binary directly from a Terminal — most won't. This
    file is the one place error details always land, regardless of how
    the app was started. Logging setup failures are swallowed (a logging
    problem should never block the app) — falls back to a do-nothing
    logger in that case, callers don't need to check.
    """
    logger = logging.getLogger("kimura")
    logger.setLevel(logging.INFO)
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        handler = RotatingFileHandler(LOG_FILE, maxBytes=512 * 1024, backupCount=5)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logger.addHandler(handler)
        logger.info("--- kimura %s starting (platform=%s, python=%s) ---",
                    __version__, platform.platform(), platform.python_version())
    except Exception:
        logger.addHandler(logging.NullHandler())
    return logger


log = _setup_logging()

VID = 0x248A
PIDS = (0x5B49, 0x5B4A)

# Per the vendor's own PID split (README.md/CLAUDE.md). NOTE: at least one
# real unit's own USB product string ("Wireless Receiver") doesn't match
# this PID's documented "wired" label — treat this as the best available
# guess, not authoritative; the GUI/CLI show the raw product string too.
CONNECTION_TYPES = {0x5B49: "Wired", 0x5B4A: "2.4GHz Wireless Receiver"}


def connection_type(pid):
    return CONNECTION_TYPES.get(pid, "Unknown (PID 0x%04X)" % pid)

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


VENDOR_OUTPUT_INTERFACE = 1  # see _control_plane_output_write()'s docstring


def _control_plane_output_write(vid, pid, buf):
    """Send `buf` (report_id + 32 payload bytes) as a HID class SET_REPORT
    control transfer (bmRequestType 0x21, bRequest 0x09, wValue 0x02<<8 |
    report_id, wIndex=VENDOR_OUTPUT_INTERFACE) via pyusb/libusb, instead of
    hidapi's interrupt-OUT write() — see _tx_output()'s docstring for why.

    CONFIRMED (real hardware + usbmon capture, 2026-09-22): always targets
    interface 1 specifically, regardless of which interface hosts this
    object's Feature-command session (often interface 0 — see
    order_candidates()'s docstring). Sending the Output report via any
    OTHER interface's endpoint gets silently ACKed by the USB stack but
    discarded by firmware — this is the exact bug that made an earlier,
    interface-agnostic version of this write path look like it worked
    (returned success) while never actually updating the button table.

    Interface 1 is normally claimed by the kernel's usbhid driver, so this
    detaches it just for interface 1 (leaving any other open interface —
    e.g. this object's own Feature-command handle, if it's on a different
    interface — untouched) for the duration of the transfer, then
    reattaches it. No detach is needed/attempted if usbhid isn't currently
    bound there.
    """
    try:
        import usb.core
    except ImportError:
        raise KimuraError(
            "control-plane fallback needs pyusb (pip install pyusb) — "
            "there's no other way to send page data on Linux for this "
            "firmware (its interrupt-OUT endpoint isn't serviced)")

    report_id = buf[0]
    devices = list(usb.core.find(idVendor=vid, idProduct=pid, find_all=True))
    if not devices:
        raise KimuraError("control-plane fallback: no USB device 0x%04X:0x%04X found"
                          % (vid, pid))

    errors = []
    for dev in devices:
        detached = False
        try:
            if dev.is_kernel_driver_active(VENDOR_OUTPUT_INTERFACE):
                dev.detach_kernel_driver(VENDOR_OUTPUT_INTERFACE)
                detached = True
            n = dev.ctrl_transfer(
                0x21, 0x09, (0x02 << 8) | report_id, VENDOR_OUTPUT_INTERFACE, buf)
            if n == len(buf):
                log.info("control-plane output write OK (%d bytes, report_id=0x%02X)",
                         n, report_id)
                return
            errors.append("ctrl_transfer wrote %d of %d bytes" % (n, len(buf)))
        except Exception as e:
            errors.append(str(e))
        finally:
            if detached:
                try:
                    dev.attach_kernel_driver(VENDOR_OUTPUT_INTERFACE)
                except Exception:
                    pass
    log.error("control-plane output write failed on every candidate device: %s",
             "; ".join(errors))
    raise KimuraError("control-plane fallback failed on every candidate device: %s"
                      % "; ".join(errors))


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
        try:
            self.dev.send_feature_report(bytes(buf))
        except Exception as e:
            # CONFIRMED (real macOS hardware, 2026-09-24): hidapi's
            # get_feature_report()/send_feature_report() can raise a raw
            # OSError ("read error") straight from the C extension, not a
            # KimuraError — every caller up the stack (including the GUI's
            # `except k.KimuraError` handlers, and read_dpi_stage()'s own
            # try/except) only catches KimuraError, so an uncaught OSError
            # here crashes whatever Tk callback triggered it instead of
            # showing a clean error/being retried. Normalize here so every
            # caller gets one consistent, catchable exception type.
            log.error("feature-report write failed (opcode=0x%02X): %s", opcode, e)
            raise KimuraError("feature-report write failed: %s" % e)

    def _rx(self):
        try:
            data = self.dev.get_feature_report(self.report_id, self.feature_len)
        except Exception as e:
            log.error("feature-report read failed: %s", e)
            raise KimuraError("feature-report read failed: %s" % e)
        return bytes(data)

    def _tx_output(self, payload32):
        """Send a 33-byte Output report: report_id + 32 data bytes.

        Distinct from _tx()/command(), which use Feature reports. See
        PROTOCOL.md §2.2/§4.3a — this is the bulk-plane transport.

        CONFIRMED this firmware only accepts page data via a control-plane
        SET_REPORT on EP0 (bmRequestType 0x21, bRequest 0x09, wValue
        0x0207, wIndex=VENDOR_OUTPUT_INTERFACE) — its interrupt-OUT
        endpoints are declared in the descriptor but not serviced as page
        storage. On macOS this is unreachable from userspace at all (IOKit
        routes Output reports to an unserviced interrupt-OUT pipe and
        hangs ~5s; direct libusb control transfers are denied while the
        interface is claimed) — refuse immediately rather than hang the
        caller (the GUI runs this on its single Tk thread).

        On Linux, hidapi's interrupt-OUT write() is NOT used at all here,
        deliberately — CONFIRMED (real hardware + usbmon capture,
        2026-09-22) it's actively misleading: on the interface this
        object's Feature session happens to be on, it can return "success"
        while the firmware silently discards the packet (wrong interface's
        endpoint); on the correct interface (1), the endpoint isn't
        serviced at all and it fails outright. Either way its return value
        isn't trustworthy, so this goes straight to the control-plane
        route via pyusb/libusb, which targets interface 1 specifically.
        """
        if len(payload32) != 32:
            raise KimuraError("output report payload must be exactly 32 bytes, got %d"
                              % len(payload32))
        buf = bytes([self.report_id]) + bytes(payload32)

        if sys.platform == "darwin":
            log.warning("page-data write refused on macOS (by design — see docstring)")
            raise KimuraError(
                "page-data write refused on macOS: this firmware only accepts "
                "button/LED table writes via a control-plane USB request, which "
                "macOS's IOKit cannot issue from userspace for this device "
                "(confirmed via hidapi, direct IOKit, and libusb — see README.md "
                "Platform notes). This is an OS limitation, not a bug here; "
                "retrying will not help.")

        _control_plane_output_write(VID, self.info.get("product_id", 0), buf)

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

    def read_dpi_stage(self):
        """Opcode 0x82 — CONFIRMED read-only DPI stage indicator
        (PROTOCOL.md §2.3/§4.1): response byte 0 (buffer offset 2) is the
        current DPI stage, 0-5, cycling with the physical DPI button. Purely
        a read, changes nothing. Returns None on failure."""
        try:
            rx = self.command(0x82)
        except KimuraError:
            return None
        return rx[2] if len(rx) > 2 else None

    def device_details(self):
        """Read-only diagnostic snapshot for a UI/CLI 'details' view: the
        vendor read-all-blocks sequence (raw, several opcodes still
        unconfirmed — see OP_READ_BLOCKS/PROTOCOL.md) plus the DPI stage
        decoded from it. Purely informational, changes nothing, and reuses
        read_all_blocks() rather than a separate 0x82 round trip."""
        blocks = self.read_all_blocks()
        dpi_rx = blocks.get(0x82)
        dpi_stage = (dpi_rx[2] if isinstance(dpi_rx, (bytes, bytearray)) and len(dpi_rx) > 2
                     else None)
        return {"blocks": blocks, "dpi_stage": dpi_stage}

    def set_led(self, preset, persist=False):
        """Opcode 0x03, 1-byte payload — CONFIRMED LED preset selector
        (PROTOCOL.md §4.3). preset must be in LED_PRESETS (0x00-0x1B); 0xFF
        is confirmed to crash the firmware and is never accepted here.

        `persist=False` (default): a single bare Feature-report write, no
        echo check. This changes the LED live but is **NOT** written to
        flash — CONFIRMED (vendor-GUI USB capture, PROTOCOL.md §4.3a): the
        real driver never sends a bare 0x03, only ever as step 6 of its
        11-step apply bundle, ending in an 0xFA flash commit. Without that
        commit, this reverts on replug/power-cycle.

        `persist=True`: sends the full 11-step bundle via
        apply_button_table() so the change survives replug/power-cycle —
        inherits that method's caveat: since there's no read-back opcode
        for the button table, this also resets any button remap to factory
        defaults unless you pass explicit overrides of your own.
        """
        if preset not in LED_PRESETS:
            raise KimuraError(
                "refusing to send unconfirmed LED preset 0x%02X — only %s "
                "are confirmed safe (see PROTOCOL.md §4.3)"
                % (preset, ", ".join("0x%02X" % p for p in sorted(LED_PRESETS))))
        log.info("set_led(preset=0x%02X, persist=%s)", preset, persist)
        if persist:
            self.apply_button_table({}, led_preset=preset)
        else:
            self.command(OP_LED, bytes([preset]), expect_reply=False)
        log.info("set_led(preset=0x%02X, persist=%s) OK", preset, persist)

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

        log.info("apply_button_table(overrides=%r, led_preset=0x%02X) starting",
                sorted(overrides), led)
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
        log.info("apply_button_table(...) committed to flash OK")


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


def order_candidates(cands):
    """Order HID interface candidates, vendor-defined usage pages first.

    Deliberately does NOT prefer interface_number == 1 (the real vendor/
    page-data channel on the hardware characterized in PROTOCOL.md and
    confirmed again via a 2026-09-22 usbmon capture) for the FEATURE-
    command session: on real hardware this firmware answers Feature
    SET_REPORT/GET_REPORT (ping, LED, page-select) identically regardless
    of which interface's control endpoint receives the request, so
    whichever interface answers first (often interface 0, when usage_page
    is unreliable — see open_generic_mouse_collection()'s docstring for
    the same Linux hidapi quirk) works fine for Feature commands. Output-
    report (page-data) writes are handled entirely separately in
    _tx_output()/_control_plane_output_write(), which always target
    interface 1 explicitly via a dedicated pyusb/libusb claim — keeping
    that claim off whatever interface this function's caller opens for
    Feature commands avoids the two colliding (CONFIRMED: they do collide,
    real hardware, 2026-09-22 — pyusb got EBUSY when the Feature session
    was also forced onto interface 1).
    """
    vendor = [d for d in cands if is_vendor_collection(d)]
    return vendor + [d for d in cands if d not in vendor]


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
    order = order_candidates(cands)
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
    order = order_candidates(cands)

    if args.persist:
        print("--persist: this also resends the button-remap table. Since there's")
        print("no way to read the mouse's current table back first, any slots you")
        print("haven't customized via `kimura remap` will be (re)set to factory")
        print("defaults. See README.md before using.")

    for d in order:
        k = probe(d, DEFAULT_REPORT_IDS, DEFAULT_LENGTHS, verbose=False)
        if k:
            try:
                k.set_led(preset, persist=args.persist)
            except KimuraError as e:
                print("Error: %s" % e)
                k.dev.close()
                return 3
            print("Sent LED preset 0x%02X (%s)%s."
                  % (preset, LED_PRESETS[preset], " and committed to flash" if args.persist else ""))
            if not args.persist:
                print("This is a live preview only — it will revert on replug/power-cycle.")
                print("Pass --persist to make it stick.")
            k.dev.close()
            return 0
    print("Could not establish transport — run `kimura.py probe` for diagnosis.")
    return 2


def cmd_factory_reset(args):
    if not args.allow_write:
        print("Refusing to write without --allow-write.")
        return 1

    print("This will flash:")
    print("  - button table -> FACTORY (left/right/middle/back/forward click, "
          "underside = DPI cycle)")
    print("  - LED preset   -> 0x00 (Neon)")
    print("  - one 0xFA flash commit")
    if not args.yes:
        answer = input("\nType RESTORE to proceed: ").strip()
        if answer != "RESTORE":
            print("Aborted — nothing written.")
            return 1

    cands = enumerate_candidates(verbose=False)
    if not cands:
        print("No 0x248A device found.")
        return 1
    order = order_candidates(cands)

    for d in order:
        k = probe(d, DEFAULT_REPORT_IDS, DEFAULT_LENGTHS, verbose=False)
        if k:
            try:
                k.apply_button_table({}, led_preset=LED_ALIASES["default"])
            except KimuraError as e:
                print("Error: %s" % e)
                k.dev.close()
                return 3
            print("Factory bundle applied. Physically verify every button and the LED now.")
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
    order = order_candidates(cands)
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


def pick_generic_candidate(cands):
    """Pick the STANDARD (non-vendor) mouse collection candidate — Input
    reports (buttons/scroll), report_id=1, per PROTOCOL.md §2.3 — without
    opening anything. Shared by open_generic_mouse_collection() and by
    callers that need to detect a same-interface collision with the
    vendor channel (some units expose both on the same candidate) before
    deciding whether to open a second handle or reuse an existing one.

    CONFIRMED (real hardware, Linux): this backend's usage_page field is
    unreliable — `list` shows 0x0000 for every interface here even though
    the real HID descriptor (per phase-b/empirical_probe.py descriptor,
    which reads it directly via hidraw) confirms interface 0 is the
    generic mouse collection. Prefer usage_page==0x0001 when it's actually
    populated (e.g. may work on macOS), but fall back to interface_number
    == 0 when every candidate reports usage_page 0 — don't just silently
    match nothing.
    """
    generic = [d for d in cands
              if not is_vendor_collection(d) and (d.get("usage_page") or 0) == 0x0001]
    if not generic:
        generic = [d for d in cands if d.get("interface_number") == 0]
    return generic[0] if generic else None


def open_generic_mouse_collection(verbose=True):
    """Open the STANDARD (non-vendor) mouse collection for reading Input
    reports — see pick_generic_candidate() for the selection logic. This
    is deliberately separate from the vendor-channel discovery in probe(),
    which targets usage_page >= 0xFF00.

    macOS refuses to open top-level Generic Desktop mouse/keyboard
    collections (see README.md platform notes) — this may simply fail to
    find anything there; that is an OS restriction, not a bug here.
    """
    cands = enumerate_candidates(verbose=False)
    d = pick_generic_candidate(cands)
    if d is not None:
        dev = hid.device()
        try:
            dev.open_path(d["path"])
            dev.set_nonblocking(1)
            return dev, d
        except Exception as e:
            if verbose:
                print("  cannot open iface=%s: %s" % (d.get("interface_number"), e))
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


def read_battery(dev):
    """Read the Feature report_id=5 channel on interface 0 (usage_page
    0xFF00 — a second, separate vendor channel from the main command
    channel at interface 1/report_id=7/usage_page 0xFF01). `dev` must
    already be open on interface 0 — the same handle
    open_generic_mouse_collection() returns.

    UNCONFIRMED (found 2026-09-15): byte 1 of the response is a strong
    candidate for battery percentage — stable across repeated reads, and in
    the plausible 0-100 range (observed 0x58 = 88). Not yet independently
    cross-checked against the vendor GUI or an actual charge-level change;
    treat the returned value as a best guess, not a confirmed reading.
    Returns None if the read fails or the response is too short.
    """
    try:
        data = dev.get_feature_report(5, 8)
    except Exception:
        return None
    if len(data) < 2:
        return None
    return data[1]


def cmd_battery(args):
    dev, d = open_generic_mouse_collection()
    if not dev:
        print("Could not open the standard mouse Input-report collection "
              "(same one `buttons` needs) — battery is read through it.")
        return 1
    pct = read_battery(dev)
    dev.close()
    if pct is None:
        print("Could not read the battery channel.")
        return 1
    print("Battery: ~%d%% (UNCONFIRMED reading — see read_battery() docstring)" % pct)
    return 0


def cmd_details(args):
    cands = enumerate_candidates(verbose=False)
    if not cands:
        print("No 0x248A device found.")
        return 1
    order = order_candidates(cands)

    for d in order:
        kk = probe(d, DEFAULT_REPORT_IDS, DEFAULT_LENGTHS, verbose=False)
        if not kk:
            continue
        pid = d.get("product_id", 0)
        print("Connection: %s (product string: %r)"
              % (connection_type(pid), (d.get("product_string") or "").strip()))
        print("PID: 0x%04X   Interface: %s   Path: %s"
              % (pid, d.get("interface_number"), d["path"].decode(errors="replace")))

        # Same-interface collision as the GUI handles (kimura_gui.py
        # refresh_device()): on some units the vendor and generic mouse
        # channels are the SAME candidate, and opening it twice fails —
        # reuse the already-open handle instead.
        generic_d = pick_generic_candidate(cands)
        if generic_d is not None and generic_d["path"] == d["path"]:
            pct = read_battery(kk.dev)
            wdev = None
        else:
            wdev, _ = open_generic_mouse_collection(verbose=False)
            pct = read_battery(wdev) if wdev else None
        if wdev:
            wdev.close()
        print("Battery: ~%d%% (unconfirmed)" % pct if pct is not None else "Battery: unavailable")

        details = kk.device_details()
        dpi = details["dpi_stage"]
        print("DPI stage: %s" % (dpi if dpi is not None else "unknown"))

        print("\nRaw read-all-blocks (diagnostic — several opcodes still unconfirmed,")
        print("see PROTOCOL.md §5):")
        for op, rx in details["blocks"].items():
            if isinstance(rx, tuple):
                print("  0x%02X  %s: %s" % (op, rx[0], rx[1]))
            else:
                print("  0x%02X  %s" % (op, " ".join("%02X" % b for b in rx[:8])))
        kk.dev.close()
        return 0
    print("Could not establish transport — run `kimura.py probe` for diagnosis.")
    return 2


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


ISSUE_URL = "https://github.com/GG-241/kimura/issues/new"


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Found a bug or unexpected behavior? Report it: %s" % ISSUE_URL)
    p.add_argument("--version", action="version", version="kimura %s" % __version__)
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
    pled.add_argument("--persist", action="store_true",
                      help="commit to flash so it survives replug/power-cycle "
                           "(also resends the button table, resetting unremapped "
                           "slots to factory default)")
    pled.set_defaults(func=cmd_led)

    pb = sub.add_parser("buttons", help="watch button/scroll Input reports (read-only)")
    pb.set_defaults(func=cmd_buttons)

    pbat = sub.add_parser("battery", help="read battery level (read-only, UNCONFIRMED)")
    pbat.set_defaults(func=cmd_battery)

    pdet = sub.add_parser("details",
        help="show connection/DPI/battery + raw diagnostic block dump (read-only)")
    pdet.set_defaults(func=cmd_details)

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

    pfr = sub.add_parser("factory-reset",
        help="restore button table + LED to factory defaults (write, gated, flash commit)")
    pfr.add_argument("--allow-write", action="store_true",
                     help="required to actually send the write")
    pfr.add_argument("--yes", action="store_true",
                     help="skip the interactive RESTORE confirmation prompt")
    pfr.set_defaults(func=cmd_factory_reset)

    args = p.parse_args()
    print("kimura %s" % __version__, file=sys.stderr)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
