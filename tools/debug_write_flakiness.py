#!/usr/bin/env python3
"""How flaky is the 33-byte output write? 6 trials, fresh handle each:
ping, write1, write2 (same handle, no ops between).
No edit-session opcodes at all — pure write probing."""
import sys
import time

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import kimura as k  # noqa: E402

BUF = bytes([0x07]) + bytes(32)


def find_transport():
    cands = k.enumerate_candidates(verbose=False)
    vendor = [d for d in cands if k.is_vendor_collection(d)]
    for d in vendor + [x for x in cands if x not in vendor]:
        kk = k.probe(d, k.DEFAULT_REPORT_IDS, k.DEFAULT_LENGTHS, verbose=False)
        if kk:
            return kk
    return None


for i in range(6):
    kk = find_transport()
    if not kk:
        print("transport FAIL")
        sys.exit(1)
    n1 = kk.dev.write(BUF)
    n2 = kk.dev.write(BUF)
    print(f"trial {i}: write1={n1} write2={n2}")
    kk.dev.close()
    time.sleep(0.4)

print("DONE")