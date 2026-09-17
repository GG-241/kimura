#!/usr/bin/env python3
"""Diagnose the 33-byte output-report write failure (hid_write -> -1).

Harmless: sends 0xF5 + page-select (no commit), then tries several
write variants against a scratch page buffer of zeros.
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


def main():
    kk = find_transport()
    if not kk:
        print("transport FAIL")
        return 1
    print("transport OK")

    def step(op, payload=b"", sleep=0.05):
        kk._tx(op, payload)
        time.sleep(sleep)

    step(0xF5)
    step(0x05, bytes([0x02, 0x01, 0x20, 0x00, 0x00, 0x00]))  # page 1 (scratch)

    # Variant A: normal 33-byte write (report id 7 + 32 zeros), with retries
    buf = bytes([0x07]) + bytes(32)
    for attempt in range(3):
        try:
            n = kk.dev.write(buf)
            print(f"A{attempt}: write 33B -> {n}")
        except Exception as e:
            print(f"A{attempt}: write 33B raised {e!r}")
            n = -1
        if n > 0:
            break
        time.sleep(0.3 * (attempt + 1))

    if n <= 0:
        # Variant B: shorter output (report id 7 + 6 bytes) — does ANY output write work?
        try:
            nb = kk.dev.write(bytes([0x07]) + bytes(6))
            print(f"B: write 7B -> {nb}")
        except Exception as e:
            print(f"B: write 7B raised {e!r}")

        # Variant C: re-enumerate, reopen a fresh handle, write again
        time.sleep(0.3)
        kk2 = find_transport()
        if kk2:
            try:
                nc = kk2.dev.write(bytes([0x07]) + bytes(32))
                print(f"C: fresh handle write 33B -> {nc}")
            except Exception as e:
                print(f"C: fresh handle write 33B raised {e!r}")
        else:
            print("C: re-open failed")

    time.sleep(0.2)
    print("ping:", kk.ping().hex(" "))
    kk.dev.close()
    print("DONE (no commit sent — nothing flashed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())