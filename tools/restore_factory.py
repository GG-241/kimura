#!/usr/bin/env python3
"""Factory restore after dongle/cable reset — controlled, verified writes.

Sequence:
  1. probe write with a harmless 33B scratch to page 1 (no session)
  2. if OK: begin edit session, write factory page0 + page1,
     verify EVERY output write, only then LED params + single 0xFA commit.
If any output write fails, abort BEFORE 0xFA (nothing flashed).
"""
import sys
import time

sys.path.insert(0, "/Users/george/Documents/kimura")
import kimura as k  # noqa: E402


def find_transport():
    cands = k.enumerate_candidates(verbose=False)
    vendor = [d for d in cands if k.is_vendor_collection(d)]
    for d in vendor + [x for x in cands if x not in vendor]:
        kk = k.probe(d, k.DEFAULT_REPORT_IDS, k.DEFAULT_LENGTHS, verbose=False)
        if kk:
            return kk
    return None


PAGE0 = bytes.fromhex("0100f0000100f1000100f2000100f3000100f40007000300") + bytes(8)
assert len(PAGE0) == 32, len(PAGE0)
PAGE1 = bytes(32)
BUF = bytes([0x07]) + PAGE1  # scratch: page-1 data, not committed


def main():
    kk = find_transport()
    if not kk:
        print("transport FAIL")
        return 1
    print("ping:", kk.ping().hex(" "))

    # --- step 1: harmless scratch write, no edit session open
    n = kk.dev.write(BUF)
    print(f"pre-session scratch write -> {n}")
    if n <= 0:
        print("write still blocked pre-session; ABORT (nothing written)")
        kk.dev.close()
        return 2

    # --- step 2: full restore, every write verified
    def step(op, payload=b"", s=0.05):
        kk._tx(op, payload)
        time.sleep(s)

    ok = True
    step(0xF5, s=0.27)
    step(0x05, bytes([0x02, 0x00, 0x20, 0x00, 0x00, 0x00]))
    n = kk.dev.write(bytes([0x07]) + PAGE0)
    print(f"page0 write (factory buttons) -> {n}")
    ok &= n == 33
    if ok:
        step(0x05, bytes([0x02, 0x01, 0x20, 0x00, 0x00, 0x00]))
        n = kk.dev.write(bytes([0x07]) + PAGE1)
        print(f"page1 write (zeros) -> {n}")
        ok &= n == 33
    if not ok:
        print("FAILED mid-bundle — sending 0xF6-less abort; NO commit sent")
        kk.dev.close()
        return 3
    step(0x03, bytes([0x00, 0, 0, 0, 0, 0]))          # LED = Neon
    step(0x02, bytes([0x04, 0x04, 0, 0, 0, 0]))
    step(0x04, bytes([0x00, 0x66, 0x66, 0, 0, 0]))
    step(0x01, bytes([0x08, 0, 0, 0, 0, 0]))
    step(0x06, bytes([0, 0, 0, 0, 0, 0]))
    step(0xFA, s=0.2)                                 # single flash commit
    print("ping after commit:", kk.ping().hex(" "))
    kk.dev.close()
    print("DONE — factory bundle applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())