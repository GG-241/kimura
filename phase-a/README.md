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
python3 kimura.py led off              # requires --allow-write, see below
python3 kimura.py led 0x00 --allow-write
python3 kimura.py buttons              # watch button/scroll Input reports (read-only)
python3 kimura.py remap --button6 lock_pc --allow-write --experimental   # see below, EXPERIMENTAL
```

`probe` sweeps candidate report IDs (0–15) and buffer lengths using opcode
`0x81`, which is a pure read — it changes nothing on the device. When the
opcode echo comes back correct, the transport is confirmed. (Opcode `0x80`,
documented as the version-read op, is confirmed non-functional on real
hardware — it never echoes — so `0x81` is used for discovery instead; see
`PROTOCOL.md` §5.)

### LED control (write, gated)

`kimura.py led <preset>` sends a CONFIRMED LED preset (opcode `0x03`, see
`PROTOCOL.md` §4.3) — accepts a hex byte (`0x00`-`0x1B`, only the
confirmed-safe values) or an alias (`off`, `default`). Requires
`--allow-write`; refuses anything outside the confirmed table, especially
`0xFF` which is confirmed to crash the firmware.

### Button remapping (write, EXPERIMENTAL — read this before using)

`kimura.py remap` writes the 6-slot button-remap table (`PROTOCOL.md` §4.3a),
found via a Windows-VM USB capture of the vendor GUI — **it has never been
replayed against real hardware by this project**, only reconstructed from
passively watching the vendor's own traffic. Requires both `--allow-write`
and `--experimental` (a second, explicit acknowledgment), and prints a
confirmation prompt before sending unless `--yes` is passed.

```bash
python3 kimura.py remap --button6 lock_pc --allow-write --experimental
python3 kimura.py remap --side-forward left_click --led off --allow-write --experimental --yes
```

Each of `--left`/`--right`/`--middle`/`--side-back`/`--side-forward`/
`--button6` accepts an action name (`left_click`, `middle_click`, `back`,
`forward`, `show_desktop`, `lock_pc`, `switch_apps`, `volume_down`,
`scroll_up_or_volume_up`, `dpi_cycle`) or a raw `"XX XX XX XX"` hex code.

**Read this before using:**
- There is no confirmed way to read the device's *current* button table
  back. Any slot you don't pass gets reset to its **factory default** —
  this can silently undo other customization you'd already set (with this
  tool, the vendor GUI, or a prior `remap` run).
- The write also resends the LED preset (default: Neon, unless `--led` is
  given) and four other settings (opcodes `0x01`/`0x02`/`0x04`/`0x06`)
  whose real meaning is still unconfirmed, using the values captured from
  one specific test session — if your mouse's actual current values differ,
  they may change too.
- It ends with a real flash commit (`0xFA`).
- After running it, physically test every remapped button — this path has
  no independent verification beyond that.

### Button/scroll reading (read-only)

`kimura.py buttons` watches the standard mouse Input report (report ID 1)
and prints button/scroll state changes live. **macOS note:** the OS refuses
to open top-level Generic Desktop mouse/keyboard collections (see Platform
notes below) — this command may simply fail to find anything there.

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

**Linux.** `kimura.py` uses the portable `hid` (libusb-backed) module, which
needs USB device-node access — a **different** rule than the `hidraw` one
`phase-b/empirical_probe.py` uses:

```
# /etc/udev/rules.d/71-kimura-usb.rules
SUBSYSTEM=="usb", ATTRS{idVendor}=="248a", ATTRS{idProduct}=="5b49", MODE="0660", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="248a", ATTRS{idProduct}=="5b4a", MODE="0660", TAG+="uaccess"
```

(The hidraw-only rule below is for `phase-b/empirical_probe.py` specifically:)

```
# /etc/udev/rules.d/70-kimura.rules
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="248a", ATTRS{idProduct}=="5b49", MODE="0660", TAG+="uaccess"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="248a", ATTRS{idProduct}=="5b4a", MODE="0660", TAG+="uaccess"
```

Then `sudo udevadm control --reload && sudo udevadm trigger` and replug the mouse.

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
- [x] Phase B — dynamic differential probing on real hardware (Linux, via `phase-b/empirical_probe.py`; Windows VM + USBPcap capture of the vendor GUI): transport confirmed, LED presets fully mapped with real names, button-remap table fully decoded (4 action-code families). DPI: the physical button is confirmed real/hardware-controllable, but there is likely no software DPI-write opcode at all — the vendor GUI's own "DPI"/"Wheel speed" sliders write nothing to the device (see `PROTOCOL.md` §5), so they're almost certainly Windows OS-level settings, not device firmware.
- [x] Phase D — CLI-only scope (no `libkimura`/platform UIs, per project decision): `kimura.py`'s `probe`/`led`/`buttons` are **confirmed working on real hardware (Linux)** — needs a `SUBSYSTEM=="usb"` udev rule in addition to the hidraw one below, since this module uses the portable `hid` (libusb) backend, not `hidraw`. `remap` is implemented but **EXPERIMENTAL and unverified on real hardware** — see its section above before using. Not yet tested on macOS.

## Licence / provenance

Protocol documented independently from the vendor binary for interoperability
purposes (DMCA §1201(f); EU Directive 2009/24/EC Art. 6). No vendor code or
assets are redistributed here.
