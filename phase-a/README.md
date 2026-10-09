# Kimura v3.0 — Phase A output

Reverse-engineering the Zeroground Kimura v3.0 (MS-4300WG) gaming mouse for
macOS and Linux. No kernel driver is required or planned; this is a user-space
configuration tool.

## Files

| File | What it is |
|---|---|
| `PROTOCOL.md` | The recovered HID protocol — transport, object layout, full opcode table |
| `kimura.py` | Portable probe/transport, read-only by default |

## Running the probe

```bash
brew install hidapi        # macOS
pip install hidapi

python3 kimura.py list                 # show every 0x248A interface
python3 kimura.py probe --read-all     # find the transport, dump config blocks
```

`probe` sweeps candidate report IDs (0–15) and buffer lengths using opcode
`0x80`, which is a pure read — it changes nothing on the device. When the
opcode echo comes back correct, the transport is confirmed and the firmware
version is decoded.

Override the sweep if you already know the parameters:

```bash
python3 kimura.py probe --report-ids 0,1,2 --lengths 33,65
```

## Platform notes

**macOS.** The OS refuses to open top-level Generic Desktop mouse/keyboard
collections. The script tries vendor-defined collections (usage page
`0xFF00`+) first for that reason. If every interface fails to open, that is
the seize restriction, not a bug in the script — the device may not expose a
separate vendor collection over the 2.4 GHz receiver, in which case the wired
PID (`0x5B49`) is the one to target.

**Linux.** hidraw access needs a udev rule:

```
# /etc/udev/rules.d/70-kimura.rules
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="248a", ATTRS{idProduct}=="5b49", MODE="0660", TAG+="uaccess"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="248a", ATTRS{idProduct}=="5b4a", MODE="0660", TAG+="uaccess"
```

Then `sudo udevadm control --reload && sudo udevadm trigger`.

## Safety

The bulk-upload opcodes (`0x05`, `0x07`) and the flash-commit opcode (`0xFA`)
are intentionally not wired up. Phase A established that neither bulk path is
a firmware writer — `0x07` uploads a 72-entry button/macro action table — so
the bricking risk flagged earlier is resolved. `0xFA` still writes flash and
should not be called in a loop.

## Status

- [x] Phase A — static protocol recovery (complete from Windows binary)
- [x] Phase C — **Hardware enumeration confirmed** (device reachable via HID on macOS)
  - Device: Telink TLSR8278, PID 0x5B49 (wired), vendor-defined interface 0xFF01
  - **CAVEAT:** Opcode echo protocol from Windows binary analysis does not work on real hardware
  - This suggests the Windows tool uses a different transport layer (possibly SetupAPI IOCTLs)
- [ ] Phase B — differential USB capture to reverse-engineer actual on-wire protocol
- [ ] Phase D — implement working transport, `libkimura` + CLI, platform UIs

## Licence / provenance

Protocol documented independently from the vendor binary for interoperability
purposes (DMCA §1201(f); EU Directive 2009/24/EC Art. 6). No vendor code or
assets are redistributed here.
