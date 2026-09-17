#!/usr/bin/env python3
"""Isolate which prior operation breaks the 33-byte output write.

One fresh handle, sequential tests (page 1 scratch, no commit):
  D1: write immediately after open (no feature ops)      -> baseline
  D2: 0x81 ping (feature write+read), then write         -> feature-op before write?
  D3: 0xF5 (begin session), then write                   -> session start?
  D4: 0x05 page-select, then write                       -> page-select?
  D5: another 0x05 page-select, then write               -> repeatable?
"""
import sys
import time

sys.path.insert(0, "/Users/george/Documents/kimura")
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


kk = find_transport()
if not kk:
    print("transport FAIL")
    sys.exit(1)

def w(label):
    n = kk.dev.write(BUF)
    print(f"{label}: write -> {n}")
    time.sleep(0.3)
    return n

time.sleep(0.2)
w("D1 (open only)")
kk._tx(0x81); time.sleep(0.05)
w("D2 (after ping)")
kk._tx(0xF5); time.sleep(0.27)
w("D3 (after 0xF5)")
kk._tx(0x05, bytes([0x02, 0x01, 0x20, 0x00, 0x00, 0x00])); time.sleep(0.05)
w("D4 (after page-select)")
w("D5 (repeat write, no op between)")
print("ping:", kk.ping().hex(" "))
kk.dev.close()
print("DONE (no commit sent)")