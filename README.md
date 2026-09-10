# Kimura v3.0 (MS-4300WG) — Unofficial Linux & macOS Driver

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

## Install

```bash
git clone https://github.com/GG-241/kimura.git
cd kimura
./install.sh
```

`install.sh` installs the `hidapi` dependency (via Homebrew on macOS), pip
installs this package, and on Linux prints the one-time udev rule setup
needed for USB access.

Or install directly with pip:

```bash
pip install git+https://github.com/GG-241/kimura.git
```

## Usage

```bash
kimura list                 # confirm the mouse is detected
kimura probe --read-all     # confirm the transport works
kimura led off --allow-write
kimura led default --allow-write
kimura buttons              # watch clicks/scroll live, read-only
```

### LED control (write, gated)

`kimura led <preset>` sends a confirmed LED preset — accepts a hex byte
(`0x00`-`0x1B`) or an alias (`off`, `default`, `breathing`). Requires
`--allow-write`; refuses anything outside the confirmed-safe table.

### Button remapping (write, EXPERIMENTAL)

`kimura remap` writes the button-remap table. **This has not been verified
against real hardware** — it's built from observing the vendor GUI's own
USB traffic, not independently replayed. Requires both `--allow-write` and
`--experimental`, and confirms interactively before sending unless `--yes`
is passed:

```bash
kimura remap --button6 lock_pc --allow-write --experimental
```

Each of `--left`/`--right`/`--middle`/`--side-back`/`--side-forward`/
`--button6` accepts an action name (`left_click`, `middle_click`, `back`,
`forward`, `show_desktop`, `lock_pc`, `switch_apps`, `volume_down`,
`scroll_up_or_volume_up`, `dpi_cycle`) or a raw `"XX XX XX XX"` hex code.

**Important:** there's no confirmed way to read the mouse's current button
table back, so any slot you don't specify is reset to its factory default —
this can silently undo other customization. Physically test every remapped
button afterward.

### DPI

The mouse's physical DPI button works at the hardware level, but there does
not appear to be a software DPI-set command in this protocol — the vendor
GUI's own "DPI" slider was observed writing nothing to the device. DPI
stage is changed with the button on the mouse itself, same as with no
driver installed.

## Platform notes

**macOS.** The OS refuses to open top-level Generic Desktop mouse/keyboard
collections — `kimura` tries vendor-defined collections first. If every
interface fails to open, that's the OS restriction, not a bug.

**Linux.** Needs a udev rule for USB access (handled by `install.sh`, or
manually):

```
# /etc/udev/rules.d/71-kimura-usb.rules
SUBSYSTEM=="usb", ATTRS{idVendor}=="248a", ATTRS{idProduct}=="5b49", MODE="0660", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="248a", ATTRS{idProduct}=="5b4a", MODE="0660", TAG+="uaccess"
```

Then `sudo udevadm control --reload && sudo udevadm trigger` and replug the mouse.

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

Use at your own risk. The `remap` command in particular is not fully
verified against real hardware — read its section above before using it.
