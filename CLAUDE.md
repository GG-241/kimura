# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is a **reverse-engineering project** to enable cross-platform configuration support (macOS and Linux) for the Zeroground Kimura v3.0 (MS-4300WG) gaming mouse. The project discovers and implements the vendor's HID protocol without requiring a kernel driver.

**Key finding:** The Windows executable contains *only* a user-space HID client. The mouse is standard USB HID; `hid-generic` (Linux) and `IOHIDFamily` (macOS) already bind to it natively.

## Repository Structure

```
.
├── KIMURA_RE_STRATEGY.md    # Complete reverse-engineering strategy, hazards, and phases
├── phase-a/
│   ├── README.md            # Phase A status and running instructions
│   ├── PROTOCOL.md          # Recovered HID protocol specification (all opcodes)
│   └── kimura.py            # Portable read-only probe/transport in Python
└── driveres_kimura30.zip    # Backup of driver/firmware (status unknown)
```

## Phase System

The project is structured in phases, with **Phase A now complete**:

- **Phase A** (✓ complete): Static protocol recovery — disassembly of the Windows executable, opcode table, transport idiom
- **Phase B**: Dynamic USB capture to determine value encodings (DPI scale, LED colors, button codes)
- **Phase C**: Replay verification on real hardware
- **Phase D**: Implementation — portable library, CLI, and platform-specific UIs (libratbag on Linux, menu-bar app on macOS)

See `KIMURA_RE_STRATEGY.md` §3 for detailed phase descriptions and deliverables.

## Running the Probe

The main tool is `kimura.py`, a portable HID transport that is **read-only by default**.

### Setup

```bash
# macOS
brew install hidapi
pip install hidapi

# Linux (requires hidraw access)
pip install hidapi
sudo tee /etc/udev/rules.d/70-kimura.rules > /dev/null <<EOF
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="248a", ATTRS{idProduct}=="5b49", MODE="0660", TAG+="uaccess"
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="248a", ATTRS{idProduct}=="5b4a", MODE="0660", TAG+="uaccess"
EOF
sudo udevadm control --reload && sudo udevadm trigger
```

### Commands

```bash
# List all Kimura interfaces (VID 0x248A)
python3 phase-a/kimura.py list

# Probe for working transport (finds report_id and buffer length)
python3 phase-a/kimura.py probe

# After transport is found, dump all config blocks
python3 phase-a/kimura.py probe --read-all
```

The probe uses opcode `0x80` (read firmware version), which is purely non-destructive.

## Key Technical Details

### Device Identifiers

| | |
|---|---|
| Vendor ID | `0x248A` |
| Product ID (wired) | `0x5B49` |
| Product ID (2.4 GHz) | `0x5B4A` |

### HID Protocol Summary

**Transport:** 255-byte feature reports (actual length queried at runtime)

**Idiom:**
1. Build command with `buf[0]` = report ID, `buf[1]` = opcode, `buf[2+]` = payload
2. Send via `HidD_SetFeature`
3. Sleep 10 ms
4. Read via `HidD_GetFeature`, verify opcode echo in `rx[1]`

**Opcode ranges:**
- `0x8x`: Read commands (with readback)
- `0x0x`: Write commands (no readback)
- `0xFA`: Flash commit (intentionally gated)
- `0x05`, `0x07`: Bulk uploads (intentionally gated)

See `phase-a/PROTOCOL.md` for the complete opcode table and object layout.

## Important Safety Notes

1. **Opcode `0xFA`** (flash commit) is gate-kept — write to flash and should not be called in a loop.
2. **Bulk upload opcodes** (`0x05`, `0x07`) are intentionally not wired up in the Python tool. `0x07` uploads a button/macro table (288 bytes), not firmware, so bricking risk is low. `0x05` is macro data. Only call these after dynamic capture (Phase B) confirms their purpose.
3. **10 ms settle delay** between command send and readback is mandatory — the MCU drops back-to-back requests.
4. **Dynamic capture is essential before Phase D** to determine value encodings (DPI scale factor, LED channel order, button action codes).

## Platform-Specific Quirks

### macOS

- The OS refuses to open top-level Generic Desktop mouse/keyboard collections.
- The probe tries vendor-defined collections (usage page `0xFF00`+) first.
- If all interfaces fail to open with a seize/permission error, that is the OS restriction, not a bug — the wired PID (`0x5B49`) may expose a separate vendor collection.

### Linux

- hidraw access requires a udev rule (see Setup above).
- Report descriptor can be dumped with `sudo usbhid-dump -d 248a: -e descriptor` for validation.

## Documentation Files

| File | Purpose |
|---|---|
| `KIMURA_RE_STRATEGY.md` | High-level strategy, static analysis findings, phase descriptions, hazards, and immediate next steps. Read this first for context. |
| `phase-a/README.md` | Phase A status checklist, setup instructions, and platform notes. |
| `phase-a/PROTOCOL.md` | Detailed HID protocol specification: transport, object layout, and opcode table with addresses. Referenced by the code. |
| `phase-a/kimura.py` | Portable Python probe. Read the module docstring and command help for usage. |

## Next Steps for Development

**Phase B (dynamic capture):**
- Run the vendor utility in a Windows VM with USB passthrough.
- Capture with USBPcap + Wireshark using differential method (one setting at a time).
- Extract value encodings: DPI divisor, LED color order, button action codes.
- Alternatively, hook `HidD_SetFeature` with Frida on Windows for cleaner data.

**Phase C (replay verification):**
- Use the probe to issue opcode `0x80` and confirm version response.
- Issue captured write commands and verify device behavior matches expectations.

**Phase D (implementation):**
- Layer the code: portable library (C or Rust) over hidapi, CLI, then platform UIs.
- Consider libratbag integration on Linux for automatic distro packaging and ongoing maintenance.

## Legal / Provenance

Protocol documentation and implementation are independent of the vendor binary, for interoperability purposes under DMCA §1201(f) and EU Directive 2009/24/EC Article 6. The vendor binary (`GamingMouse.exe`), skin assets, and SDK libraries are not redistributed here.

## Reverse-Engineering Method

The project uses static binary analysis (IDA Pro / Ghidra), focusing on:
- HID API call sites and their immediate context
- Opcode constant definitions and payload writes
- Object layout (addresses, offsets, sizes)
- Dialog event handlers and feature strings

This method is sound for user-space HID clients, which have a narrow, well-documented API surface. See `KIMURA_RE_STRATEGY.md` §2 for the detailed analysis.
