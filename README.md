# Kimura v3.0 (MS-4300WG) — Unofficial Linux & macOS Driver

A community, user-space configuration tool for the [**Zeroground Kimura v3.0
(MS-4300WG)**](https://www.zero-ground.com/kimura-30-leuko) gaming mouse, for Linux and macOS.

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
installs this package, sets up desktop integration (a "Kimura GUI.app" in
`~/Applications` on macOS, an application-menu entry on Linux), and on
Linux also prints the one-time udev rule setup needed for USB access.

### Standalone builds (no Python/pip needed)

Prefer a single file you can just run? Build one yourself:

```bash
bash packaging/build_appimage.sh   # Linux -> dist/Kimura-GUI-x86_64.AppImage
bash packaging/build_dmg.sh        # macOS -> dist/Kimura-GUI.dmg (run on a Mac)
```

Both scripts use a throwaway venv + PyInstaller and don't touch your normal
Python environment. The AppImage is confirmed working (built and tested in
this project's own dev environment, including talking to real hardware).
The DMG is confirmed working on Apple Silicon (verified on real macOS with
brew's Python 3.13 + `python-tk@3.13`). It needs a Python >= 3.12 — older
python.org 3.11.x builds have a tkinter bug that intermittently crashes
the GUI. Neither build is code-signed, so macOS Gatekeeper will require
right-click → Open on first launch.

Or install directly with pip:

```bash
pip install git+https://github.com/GG-241/kimura.git
```

## GUI

```bash
kimura-gui
```

Or launch "Kimura GUI" from your Applications folder (macOS) or application
menu (Linux) after running `install.sh`. Needs `tkinter` — usually already
present, but if not: `sudo apt install python3-tk` (Linux) or
`brew install python-tk` (macOS). Four tabs, plus a live battery reading in
the top bar:

- **Device** — live status, battery (see below), a Mouse Details panel
  (connection type, PID/interface/path, DPI stage, and the raw diagnostic
  block dump — see "Mouse details" below), Refresh button, and Factory
  Reset
- **LED** — pick a preset from the dropdown, Apply (persists to flash by
  default — see LED control above)
- **Button Remap** (experimental) — same write path and same caveats as
  `kimura remap` below, with a confirmation dialog before sending; shows
  the real mouse photo with numbered markers for each slot
- **Live (read-only)** — button/scroll indicator

**Battery** is read from a previously-undocumented Feature channel and is
**unconfirmed** — a stable, plausible-range value, not yet independently
cross-checked. Treat it as a best guess (`kimura battery` on the CLI, or the
top bar in the GUI).

**System tray icon** (optional): shows connection/battery/DPI-stage status
in a click menu, with checkable toggles for what appears in the hover
tooltip. The GUI works fine without it; on Ubuntu/GNOME it needs:
```bash
sudo apt install gir1.2-ayatanaappindicator3-0.1
```

## CLI Usage

```bash
kimura list                 # confirm the mouse is detected
kimura probe --read-all     # confirm the transport works
kimura details              # connection type, DPI stage, battery, raw diagnostics
kimura led off --allow-write --persist
kimura led default --allow-write --persist
kimura buttons              # watch clicks/scroll live, read-only
kimura factory-reset --allow-write
```

### Mouse details (read-only)

`kimura details` (and the GUI's Device page) shows connection type
(wired/2.4GHz, from the PID) alongside the raw USB product string, the
interface/path in use, current DPI stage, battery, and a raw dump of the
vendor read-all-blocks sequence (`0x81`/`0x86`/`0x82`/`0x83`/`0x84`) for
diagnostics — several of those opcodes' meanings are still unconfirmed
(see `phase-a/PROTOCOL.md` §5), so treat the raw block values as
diagnostic, not a documented API.

### LED control (write, gated)

`kimura led <preset>` sends a confirmed LED preset — accepts a hex byte
(`0x00`-`0x1B`) or an alias (`off`, `default`, `breathing`). Requires
`--allow-write`; refuses anything outside the confirmed-safe table.

By default this is a **live preview only** — it changes the LED instantly
but reverts on replug or power-cycle, because the real vendor driver never
sends the LED opcode on its own; it's always one step inside an 11-command
bundle that ends with a flash commit (confirmed byte-for-byte from a USB
capture of the vendor GUI, see `phase-a/PROTOCOL.md` §4.3a). Pass
`--persist` to send that full bundle and make the change stick. Since
there's no confirmed way to read the mouse's current button table back,
`--persist` also resends it — any button slot you haven't customized via
`kimura remap` in the same session gets (re)set to its factory default.
The GUI's LED page always persists, with the same caveat shown before Apply.

### Factory Reset (write, gated)

`kimura factory-reset` restores the button table (left/right/middle click,
back/forward, underside = DPI cycle) and LED (Neon) to their factory
defaults in one flash commit — the same confirmed bundle `--persist`
above uses, with no overrides. Requires `--allow-write` and typing
`RESTORE` to confirm (skip the prompt with `--yes`). Also available from
the GUI's Device page.

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

Separately, `--persist`, `remap`, and `factory-reset` (anything that writes
the 32-byte button/LED table) do not work on macOS at all: this firmware
only accepts that data via a control-plane USB request, and macOS's IOKit
HID stack cannot issue one for this device from userspace (confirmed three
independent ways — hidapi, direct IOKit, and libusb all fail). `kimura`
detects this and refuses immediately with a clear error instead of hanging;
there is no known macOS workaround. The single-opcode, non-persistent
`kimura led <preset>` (no `--persist`) is unaffected and still works.

**Linux.** Needs a udev rule for USB access (handled by `install.sh`, or
manually):

```
# /etc/udev/rules.d/71-kimura-usb.rules
SUBSYSTEM=="usb", ATTRS{idVendor}=="248a", ATTRS{idProduct}=="5b49", MODE="0660", TAG+="uaccess"
SUBSYSTEM=="usb", ATTRS{idVendor}=="248a", ATTRS{idProduct}=="5b4a", MODE="0660", TAG+="uaccess"
```

Then `sudo udevadm control --reload && sudo udevadm trigger` and replug the mouse.

The 32-byte button/LED table writes used by `--persist`, `remap`, and
`factory-reset` need a **control-plane** USB request on the vendor
interface (confirmed interface 1) — its interrupt-OUT endpoint is declared
but not wired to page storage. `kimura` sends these via `pyusb`/libusb
directly rather than hidapi's `write()`, which CONFIRMED (real hardware,
2026-09-22) is unreliable here two different ways: on whichever interface
happens to host the Feature-command session it can return "success" while
firmware silently discards the packet (wrong interface), and on the
correct interface its own interrupt-OUT endpoint isn't serviced at all
(fails outright). The control-plane write briefly detaches the kernel's
`usbhid` driver from *just* the vendor interface for the duration of each
transfer and reattaches it immediately after — no extra udev rule beyond
the one above (`uaccess` already covers `/dev/bus/usb/*`), and this
doesn't disturb the Feature-command session, which normally lives on a
different interface.

**Mouse stops working system-wide while the app is running/after a crash
(Linux, fixed 2026-09-24).** Any hidapi handle opened on interface 0 —
the mouse's actual cursor-movement interface — detaches it from the
kernel's own `usbhid` driver, on Linux, for as long as the handle stays
open; hidapi does not reattach it on close. This was previously a real,
reproduced-live bug: opening `kimura`/`kimura-gui` at all could leave the
system mouse cursor unresponsive until a physical replug, worse if the
process was killed (SIGTERM) rather than closed normally, since cleanup
never ran. Now fixed several ways: the Feature-command session prefers
interface 1 over interface 0 whenever possible (costs nothing — this
firmware answers Feature commands identically regardless of interface);
the Live tab's button/movement view and the battery reading only hold
interface 0 open for as long as they're actually needed (not the app's
whole lifetime); every code path that does open a handle explicitly
reattaches the kernel driver on close, including on SIGTERM. If you ever
do see the mouse stop responding as a normal pointing device, a udev
device reset without unplugging works too — see `reattach_kernel_driver()`
in `kimura.py` for the exact mechanism, or as a first resort just unplug
and replug the receiver.

A parallel symptom was also reported on **macOS** (mouse unresponsive
after the app opened, fixed by unplugging/replugging the receiver) — the
underlying mechanism there is NOT confirmed (macOS's IOHIDFamily doesn't
expose an equivalent "kernel driver detach" the same way), so the Linux
fixes above can't be verified to fully cover it. If you hit this on
macOS, the same interface-0-avoidance and lazy-Live-tab-opening changes
should reduce exposure, but treat it as unconfirmed until tested on real
macOS hardware.

## Dependencies

Both `kimura` (CLI) and `kimura-gui` check their dependencies on startup and
exit with a clear, actionable message (which package/command to run) instead
of a raw traceback if something's missing — `hid`/`hidapi`, and for the GUI
also `tkinter`, `customtkinter`, and `Pillow`. `pyusb` (needed on Linux for
button/LED-persist/factory-reset writes — see Platform notes above) and the
system tray's `pystray` are checked lazily, only when actually used/started,
and degrade with a clear error or a silent no-tray fallback respectively
rather than crashing the app.

The standalone AppImage/DMG builds bundle all of these already (see
Standalone builds above), so this mainly matters if you're running from
source or via `pip install`.

## Reporting issues

Found a bug, a crash, or something that doesn't match this README?
[Open an issue on GitHub](https://github.com/GG-241/kimura/issues/new) —
both the CLI (`--help`) and the GUI (top bar, "Report an Issue") link here
too. Useful details to include: your OS, how you installed (AppImage/DMG/
pip/source), the exact command or button you used, and the full error
text if there was one.

Both the CLI and GUI also log to `~/.kimura/kimura.log` (rotated, kept
small) — every write attempt, its result, and any error, including ones
that only show as a dialog box with no visible Terminal (the AppImage/DMG
builds have no console window). Attaching the last few lines of that file
to a bug report is usually more useful than the dialog text alone.

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

## Support this project

Kimura is an independent, spare-time project and is offered free of
charge. If you found it useful and would like to support its continued
development, you can buy the author a coffee — it is genuinely
appreciated:

<p align="center">
  <img src="donate-qr.png" alt="Donation QR code" width="240">
</p>
