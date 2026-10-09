# Zeroground Kimura v3.0 (MS-4300WG) — Reverse Engineering Strategy

**Target:** cross-platform configuration support for macOS and Linux
**Source artifact:** `MS-4300WG KIMURA MOUSE(5).msi` (3,169,280 bytes)
SHA-256 `687345e63774d07de9ab2c928f0af6a6d112291df763284bb9242096a69ef3b5`

---

## 0. Executive summary

**No kernel driver is required, and none should be written.**

Static analysis of the MSI payload confirms the Windows package contains a
single user-space MFC application that configures the mouse over standard HID
feature reports. There is no `.sys`, no `.inf`, and no kernel component of any
kind. The mouse itself is a compliant USB HID device that `hid-generic`
(Linux) and `IOHIDFamily` (macOS) already bind to without modification.

The project is therefore **not** "port a driver." It is:

1. Recover the vendor HID command protocol.
2. Reimplement it in a portable user-space library.
3. Ship a CLI and GUI on top of that library for macOS and Linux.

---

## 1. Confirmed facts from static analysis

### 1.1 Package metadata

| Field | Value |
|---|---|
| Product name | MS-4300WG KIMURA MOUSE |
| Manufacturer | HCM |
| Product version | 1.0.0 |
| Upgrade code | `{B0A85174-951F-4D2C-9687-55E401238371}` |
| Built | 2021-07-27 07:45:04 UTC |
| Installer type | Visual Studio Deployment Project |

### 1.2 Payload inventory (47 files)

| File | Size | Notes |
|---|---|---|
| `GamingMouse.exe` | 269,824 | The entire product. PE32 x86 GUI, MFC (`mfc100u.dll`), MSVC 2010 runtime, compiled 2021-07-27 07:44:55 UTC, file version 1.0.0.1, resource language 2052 (zh-CN) |
| `hid.lib` | 12,384 | Windows SDK import library, shipped by mistake |
| `setupapi.lib` | 141,048 | Windows SDK import library, shipped by mistake |
| `msiexec.exe` | 59,904 | Bootstrapper |
| `skins/*` | ~2.8 MB | 43 UI bitmaps — the bulk of the package |

The two stray `.lib` files are a build-system leak. They are of no functional
value but they do confirm the toolchain and the intended API surface.

### 1.3 Device identity

```
USB\VID_248A&PID_5B49
USB\VID_248A&PID_5B4A
```

VID `0x248A`, PIDs `0x5B49` and `0x5B4A`. Two product IDs, matching the
device's dual-mode operation (wired vs. 2.4 GHz receiver). The enumeration
routine at `0x0041FBA0` first filters device paths by the VID/PID substrings,
then re-validates with `HidD_GetAttributes` against `VendorID` and `ProductID`
stored at object offsets `+0x2DC` and `+0x2DE`.

`0x248A` is the Maxxter/Chinese-ODM vendor block used across a wide range of
rebadged gaming peripherals. Zeroground is a Greek retail brand; the hardware
and the software are an ODM reference design. **This is good news** — protocol
knowledge is likely transferable to and from sibling devices.

### 1.4 Import surface — the decisive evidence

```
HID.DLL     HidD_GetAttributes, HidD_GetHidGuid, HidD_GetPreparsedData,
            HidD_FreePreparsedData, HidP_GetCaps, HidP_GetSpecificValueCaps,
            HidD_GetFeature, HidD_SetFeature, HidD_SetOutputReport

SETUPAPI    SetupDiGetClassDevsW, SetupDiEnumDeviceInfo,
            SetupDiEnumDeviceInterfaces, SetupDiGetDeviceInterfaceDetailW,
            SetupDiGetDeviceRegistryPropertyW, SetupDiDestroyDeviceInfoList

KERNEL32    CreateFileW, ReadFile, DeviceIoControl, Sleep, ...
```

Nine HID calls, six SetupAPI calls, no driver-install or service-control APIs
anywhere. This is a textbook user-space HID client. Every capability it has is
reproducible on macOS via IOKit and on Linux via hidraw.

---

## 2. Protocol shape — already recovered

The command layer is a compact cluster of ~15 functions in `0x00420000`–
`0x00420900`, plus a second cluster at `0x00416D00`–`0x00417000`. The idiom is
identical in every one:

```c
memset(txbuf /* obj+0xC4 */, 0, 0xFF);
memset(rxbuf /* obj+0x1C3 */, 0, 0xFF);
txbuf[0] = reportId;      // from obj+0x2E4, discovered at enumeration
txbuf[1] = OPCODE;        // the command
// ... payload bytes at txbuf[2..]
HidD_SetFeature(hDev, txbuf, featureLen + 1);   // featureLen from obj+0x2E8
Sleep(10);
rxbuf[0] = reportId;
HidD_GetFeature(hDev, rxbuf, featureLen + 1);
if (rxbuf[1] != OPCODE) goto fail;              // device echoes the opcode
// ... parse payload from rxbuf[2..]
```

Key structural findings:

- **Buffers are 255 bytes.** Actual report length is queried at runtime from
  `HidP_GetSpecificValueCaps(HidP_Feature, ...)`, not hardcoded — so the tool
  must do the same rather than assume a length.
- **Byte 0 is the report ID**, byte 1 is the opcode, payload follows.
- **The device echoes the opcode** in byte 1 of the response. That is a free
  and reliable synchronisation check for your own implementation.
- **A 10 ms settle delay** sits between write and read. Respect it; these MCUs
  drop back-to-back requests.
- `HidD_SetOutputReport` is used by two functions (`0x004206D0`, `0x00420820`)
  in addition to `SetFeature` — these are the bulk-transfer paths, almost
  certainly macro upload and LED table upload.

### 2.1 Opcode table (partial, statically recovered)

| Opcode | Function | Direction | Inferred purpose |
|---|---|---|---|
| `0x80` | `0x004200D0` | set → get | Read version. Response bytes `[2]`,`[3]` combined as `b2*10 + b3` |
| `0x81` | `0x00420290` | set → get | Read config block |
| `0x82` | `0x00420490` | set → get | Read config block |
| `0x83` | `0x004203C0` | set → get | Read config block |
| `0x84` | `0x00420600` | set → get | Read config block |
| `0x86` | `0x004201B0` | set → get | Read config block |
| `0xF5` | `0x00420360` | set only | Commit / apply (no readback) |
| `0x04` | `0x00420560` | set only | Write, 3 payload bytes at `[2],[3],[4]` |
| `0x01` | `0x00416ECE` | set only | Write, 1 payload byte |
| `0x02` | `0x00416DDC` | set only | Write, 2 payload bytes |
| `0x03` | `0x00416D76` | set only | Write, 1 payload byte |
| `0x06` | `0x00416F60` | set only | Write, 2 payload bytes |
| `0xFA` | `0x00416FBE` | set only | Write, no payload — likely save-to-flash |

Read opcodes are `0x8x` (high bit set); write opcodes are small integers. That
is a clean, conventional split and strongly suggests the remaining opcodes are
contiguous in the same ranges.

### 2.2 Feature set, from UI strings

DPI stages, report/polling rate, LED modes (`Breathing`, `Neon`, `Wave`,
`Customize`, `LED off`), per-button remapping (Left/Right/Middle/Forward/
Backward/Scroll), `FIRE` rapid-fire with `Fire speed`, `Wheel speed`, macro
recording with per-keystroke delays, and 4 onboard profiles (`Profile1`–`4`)
with import/export to `Config1.ini`–`Config5.ini`.

Host-side state lives in the registry under `Software\HCM` and
`Software\HCM\Macro\`; autostart is `HCMRun`, described as
`HCM Gaming Mouse Driver`. None of that matters for the port except as a hint
to the `.ini` schema, which is worth reading for its field names.

---

## 3. Remaining work

### Phase A — complete the opcode table (mostly static)

The idiom is uniform enough to script. Walk every `HidD_SetFeature` call site,
extract the constant stored at `obj+0xC5`, and record the payload writes at
`+0xC6`..`+0xCC`. That mechanically yields the full command table. Loading
`GamingMouse.exe` into Ghidra with MFC signatures applied will also recover the
dialog handler → command mapping, which is what actually labels each opcode
with its user-facing meaning.

Read `Config1.ini`'s schema as a cross-check: the `.ini` field ordering usually
mirrors the on-wire byte ordering in these ODM tools.

### Phase B — dynamic confirmation

Static analysis gives structure; capture gives ground truth for value
encodings (DPI scaling factor, LED colour order, button scancode map).

Run the utility in a Windows VM with the dongle passed through and capture with
USBPcap + Wireshark. Use strict differential method: change exactly one
setting, capture, revert, capture, diff. DPI 800 → 1600 tells you the divisor.
LED red → green → blue tells you channel order. One button remap tells you the
action encoding.

If VM passthrough of the 2.4 GHz dongle proves unstable — common — hook
`HidD_SetFeature` with Frida on bare-metal Windows instead. Cleaner data, less
setup.

Also dump the report descriptor on Linux (`usbhid-dump`, or `hid-recorder`
from hid-tools) for both PIDs. You need to know which collection carries the
vendor feature report, and whether the descriptor is well-formed. It is the
only thing that could justify a kernel-side quirk, and it almost certainly
will not.

### Phase C — replay verification

Before building anything, prove the protocol end to end. Python + `hidapi`,
open the vendor collection, send opcode `0x80`, confirm a sane version comes
back. Then send one captured write and confirm the mouse's behaviour changes.

**macOS caveat:** the OS restricts opening HID devices that expose top-level
mouse or keyboard collections. Target the vendor-defined collection
specifically. If you get a seize/permission error, that is what it is — it is
not a code bug.

**Linux caveat:** you need a udev rule granting your user access to the hidraw
node, or everything runs as root.

### Phase D — implementation

Layer the code so the protocol is written once:

```
libkimura      — portable C or Rust. Protocol + hidapi backend.
kimuractl      — CLI over libkimura. Ships on both platforms.
macOS GUI      — SwiftUI menu-bar app, IOHIDManager. No kext, no dext,
                 no Apple entitlement needed.
Linux GUI      — contribute a device entry to libratbag instead of
                 writing one. You inherit the Piper GUI, distro
                 packaging, and ongoing maintenance for free.
```

The libratbag route is the single highest-leverage decision in this project.
It converts "a tool three people use" into "supported hardware."

---

## 4. Hazards

**Firmware update paths.** Two functions use `HidD_SetOutputReport` for bulk
writes. At least one is macro upload; if either turns out to be a firmware
path, a malformed write bricks the mouse permanently and unrecoverably. This
is the only irreversible mistake available in this project. Identify those two
code paths positively before sending anything resembling a bulk write.

**Onboard flash wear.** Opcode `0xFA` (no payload, no readback) is very likely
commit-to-flash. Do not call it in a loop during testing.

**Legal.** Reverse engineering for interoperability is protected under DMCA
§1201(f) and Article 6 of the EU Software Directive (Directive 2009/24/EC).
Publish your own protocol documentation and your own clean implementation. Do
not redistribute `GamingMouse.exe`, the skin assets, or the `.lib` files.

---

## 5. Immediate next steps

1. Recover the ZIP file (currently 0 bytes) — it may contain a different or
   newer build worth diffing against this one.
2. Script the opcode extraction across all `SetFeature` call sites (Phase A).
3. Dump the report descriptor for both `5B49` and `5B4A` on Linux.
4. Stand up the Windows VM capture rig (Phase B).
