#!/usr/bin/env python3
"""
kimura.py — portable probe/transport for the Zeroground Kimura v3.0 (MS-4300WG)
gaming mouse, VID 0x248A / PID 0x5B49 (wired) or 0x5B4A (2.4 GHz receiver).

Protocol reverse engineered from the vendor's Windows utility GamingMouse.exe.
See PROTOCOL.md for the derivation.

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
OP_VERSION = 0x80
OP_READ_BLOCKS = (0x81, 0x86, 0x82, 0x83, 0x84)  # order used by the vendor's
                                                 # read-all routine @0x00415890
OP_WRITE_3B = 0x04
OP_APPLY = 0xF5

# Deliberately NOT exposed: 0xFA (flash commit), 0x05/0x07 (bulk upload).

SETTLE = 0.010  # the vendor tool's mandatory Sleep(10) between write and read


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
        """Opcode 0x80. The vendor decodes rx[2]*10 + rx[3]."""
        rx = self.command(OP_VERSION)
        major, minor = rx[2], rx[3]
        return major * 10 + minor, (major, minor), rx

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
    """Try opcode 0x80 across candidate (report_id, length) pairs.

    0x80 is a pure read. Nothing here changes device state.
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
                combined, parts, rx = k.version()
            except Exception:
                continue
            if verbose:
                print("    HIT  report_id=0x%02X len=%d -> version %d.%d "
                      "(raw %d)" % (rid, ln, parts[0], parts[1], combined))
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

    report_ids = args.report_ids or list(range(0, 16))
    lengths = args.lengths or [9, 17, 33, 65, 264]

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

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
