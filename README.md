#Kimura v3.0 (MS-4300WG) — Unofficial Linux & macOS Driver

A community, user-space configuration tool for the **Zeroground Kimura v3.0
(MS-4300WG)** gaming mouse, for Linux and macOS.

**Why this exists:** Zeroground only ships an official configuration
utility for Windows. Before starting this project, an attempt was made to
contact Zeroground directly to ask whether a macOS/Linux driver was
available or planned — there was no response. This project reverse-engineers
the mouse's USB HID protocol independently, for interoperability, so Linux
and macOS users can configure LED and button behavior without needing
Windows or the vendor's own software.

No kernel driver is required. The mouse is a standard USB HID device —
`hid-generic` (Linux) and `IOHIDFamily` (macOS) already talk to it natively.
This tool is a small user-space client that speaks the vendor's own
configuration protocol over that existing HID connection.

## What works today

- **LED control** — all 7 named presets (Neon, Colour Streaming, Breathing,
  Colorful tail, Wave, Stars Twinkle, LED Off), plus additional presets
  found during testing.
- **Button/scroll reading** — live, read-only.
- **Button remapping** — implemented, but marked experimental; see the
  warnings in `phase-a/README.md` before using it.
- **DPI** — the mouse's physical DPI button is confirmed to work at the
  hardware level, but there does not appear to be a software DPI-set
  command in this protocol at all (the vendor GUI's own "DPI" slider writes
  nothing to the device — see `phase-a/README.md` for detail). Practically,
  DPI stage is changed with the button on the mouse itself, same as with no
  driver installed.

## Requirements

```bash
brew install hidapi        # macOS
pip install hidapi          # or: pip3 install hidapi --break-system-packages (Ubuntu/Debian)
```

Linux also needs a udev rule for USB device access — see
`phase-a/README.md`.

## Quick start

```bash
python3 phase-a/kimura.py list                 # confirm the mouse is detected
python3 phase-a/kimura.py probe --read-all     # confirm the transport works
python3 phase-a/kimura.py led off --allow-write
python3 phase-a/kimura.py buttons              # watch clicks/scroll live
```

Full usage, all commands, and safety notes: **`phase-a/README.md`**.

## Project layout

```
phase-a/kimura.py     the driver itself — portable, macOS + Linux
phase-a/README.md     full usage instructions, all commands, safety notes
phase-b/               Linux-only tooling used during protocol discovery
```

## License

Free for personal and noncommercial use under the [PolyForm Noncommercial
License 1.0.0](LICENSE). If Zeroground, a retailer, or anyone else wants to
bundle this code or use it to build an official driver, please open an
issue on this repository to discuss a commercial license.

## Disclaimer

This is an independent, unofficial project, not affiliated with or
endorsed by Zeroground. It was built by observing the mouse's own USB
traffic and the official Windows utility's behavior, for the sole purpose
of interoperability. No vendor code, binaries, or assets are included in
this repository.

Use at your own risk. Some write operations documented here are not fully
verified against real hardware — see `phase-a/README.md` for exactly what
is confirmed versus experimental before using any write command.

