#!/usr/bin/env python3
"""Find the delay (if any) between page-select feature write and the
33-byte output write that makes hid_write succeed on macOS.

Each trial: fresh handle -> ping -> 0xF5 -> page-select -> sleep DELAY ->
write 33B (report 7 + 32 zeros to scratch page 1) -> report result.
Harmless: page 1 scratch buffer, no 0xFA commit ever sent.
"""
import sys
import time

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kimura as k  # noqa: E402


def find_transport():
    cands = k.enumerate_candidates(verbose=False)
    vendor = [d for d in cands if k.is_vendor_collection(d)]
    for d in vendor + [x for x in cands if x not in vendor]:
        kk = k.probe(d, k.DEFAULT_REPORT_IDS, k.DEFAULT_LENGTHS, verbose=False)
        if kk:
            return kk
    return None


DELAYS = [0.05, 0.2, 0.5, 1.0, 2.0]

for delay in DELAYS:
    kk = find_transport()
    if not kk:
        print("transport FAIL")
        sys.exit(1)
    kk._tx(0xF5)
    time.sleep(0.27)  # vendor's 0xF5 -> page-select gap (~270 ms)
    kk._tx(0x05, bytes([0x02, 0x01, 0x20, 0x00, 0x00, 0x00]))  # page 1 scratch
    time.sleep(delay)
    buf = bytes([0x07]) + bytes(32)
    n = kk.dev.write(buf)
    print(f"delay={delay:>4}s -> write {n}")
    kk.dev.close()
    time.sleep(0.3)

print("DONE (no commit sent — nothing flashed)")