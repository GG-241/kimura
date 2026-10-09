# Kimura v3.0 (MS-4300WG) — HID Protocol Specification

Recovered by static analysis of `GamingMouse.exe` (SHA-256 of parent MSI:
`687345e63774d07de9ab2c928f0af6a6d112291df763284bb9242096a69ef3b5`).

**IMPORTANT:** Hardware testing on macOS shows that real devices do **NOT** respond
to the opcode echo protocol documented in this spec. The Windows binary may use
SetupAPI IOCTLs or a Windows-specific transport layer instead of standard HID
feature reports. **Phase B (dynamic USB capture) is required to reverse-engineer
the actual on-wire protocol.** This spec remains useful as a reference for the
command structure and opcode table, pending real-world protocol verification.

Status: Phase A static analysis complete; Phase C hardware enumeration
confirmed; Phase B (actual protocol) deferred pending USB capture or Frida tracing.

---

## 1. Device

| | |
|---|---|
| Vendor ID | `0x248A` |
| Product ID | `0x5B49` (wired) / `0x5B4A` (2.4 GHz receiver) |
| Confirmed present | `0x248A:0x5B4A` "Wireless Receiver" |

The application enumerates via SetupAPI, filters device paths on the literal
substrings `"248a"` and `"5b49"`/`"5b4a"`, then re-validates with
`HidD_GetAttributes`.

## 2. Transport

Two distinct transports are used.

### 2.1 Feature-report transport (control plane)

Buffers are 255 bytes, zero-filled before each use. The actual on-wire length
is **queried at runtime** via `HidP_GetSpecificValueCaps(HidP_Feature, …)` and
stored at object offset `+0x2E8`; the report ID is stored at `+0x2E4`. Do not
hardcode either.

```
txbuf[0] = reportId          # object +0x2E4
txbuf[1] = opcode
txbuf[2..] = payload
HidD_SetFeature(dev, txbuf, featureLen + 1)

Sleep(10)                    # mandatory settle delay

rxbuf[0] = reportId
HidD_GetFeature(dev, rxbuf, featureLen + 1)
assert rxbuf[1] == opcode     # device echoes the opcode
# response payload in rxbuf[2..]
```

The opcode echo in byte 1 is a reliable, free ACK. Every read command in the
binary checks it and bails out if it does not match.

### 2.2 Output-report transport (bulk plane)

Used for the two large uploads. A feature report primes the transfer (setting
a destination offset and a chunk length), then a 33-byte output report carries
32 bytes of data. The loop repeats until the whole block is sent.

```
txbuf[0]  = reportId
txbuf[1]  = opcode
txbuf[2]  = subcommand
txbuf[3]  = chunk offset          # +0xC7
txbuf[4]  = chunk length (0x20)   # +0xC8
txbuf[5]  = table index           # +0xC9, opcode 0x07 only
HidD_SetFeature(...)
Sleep(...)
out[0] = reportId; out[1..32] = 32 data bytes
HidD_SetOutputReport(dev, out, 33)
```

## 3. Object layout (`GamingMouse.exe` device class)

| Offset | Meaning |
|---|---|
| `+0x0B8` | read handle (`HidD_GetFeature` target) |
| `+0x0BC` | write handle (`HidD_SetFeature` target) |
| `+0x0C4` | TX feature buffer base (255 bytes) |
| `+0x0C5` | opcode slot |
| `+0x0C6`…`+0x0C9` | payload bytes |
| `+0x17C` | secondary TX buffer base (short-command class) |
| `+0x17D` | secondary opcode slot |
| `+0x1C3` | RX feature buffer base (255 bytes) |
| `+0x1C4` | echoed opcode in response |
| `+0x1C5`, `+0x1C6` | first response payload bytes |
| `+0x2DC` | VendorID |
| `+0x2DE` | ProductID |
| `+0x2E4` | report ID |
| `+0x2E8` | feature report length |

## 4. Opcode table

### 4.1 Read commands (`0x8x`) — write then read back

| Opcode | Function | Caller | Notes |
|---|---|---|---|
| `0x80` | `0x004200D0` | `0x00417368` | Read version. Response decoded as `rx[2]*10 + rx[3]` |
| `0x81` | `0x00420290` | `0x004158C6` | Config block read (1st in read-all sequence) |
| `0x86` | `0x004201B0` | `0x004158E0` | Config block read (2nd) |
| `0x82` | `0x00420490` | `0x00415914` | Config block read (3rd) |
| `0x83` | `0x004203C0` | `0x00415929` | Config block read (4th). Emitted as a 16-bit store `mov word [esi+0xC5], 0x0083`, i.e. opcode `0x83` with an explicit `0x00` in `+0xC6`. Echo-checked at `0x00420464`. Returns a single byte to `*arg_8h` |
| `0x84` | `0x00420600` | `0x00415940` | Config block read (5th) |

`fcn.00415890` is the **read-all-settings** routine and issues these in the
order `0x81, 0x86, 0x82, 0x83, 0x84`. Replicating that sequence is the safest
first end-to-end test after `0x80`.

### 4.2 Write commands — no readback

| Opcode | Function | Payload | Notes |
|---|---|---|---|
| `0x04` | `0x00420560` | 3 bytes at `+0xC6`,`+0xC7`,`+0xC8` | Caller `0x00416E23` |
| `0xF5` | `0x00420360` | none | Apply / commit. Caller `0x004169DE`, immediately after a `PostMessage` + `Sleep` UI update |

### 4.3 Short-command class (buffer base `+0x17C`)

A second, smaller command buffer with its own opcode space. All are
write-only, no echo check.

| Opcode | Address | Payload |
|---|---|---|
| `0x01` | `0x00416ECE` | 1 byte at `+0x17E` |
| `0x02` | `0x00416DDC` | 2 bytes at `+0x17E`, `+0x17F` |
| `0x03` | `0x00416D76` | 1 byte at `+0x17E` |
| `0x06` | `0x00416F60` | 2 bytes at `+0x17E`, `+0x17F` |
| `0xFA` | `0x00416FBE` | none — **likely save-to-flash** |

### 4.4 Bulk upload commands

| Opcode | Sub | Function | Total size | Chunk | Notes |
|---|---|---|---|---|---|
| `0x05` | `0x02` | `0x004206D0` | 64 bytes | 32 | `mov word [ebx+0xC5], 0x0205`. Caller `0x00416CFA` |
| `0x07` | `0x09` | `0x00420820` | 288 bytes | 32 | `mov word [ebx+0xC5], 0x0907`. Extra index byte at `+0xC9` from `arg_8h` |

**Neither is a firmware path.** `fcn.00420960` — the sole caller of opcode
`0x07` — builds the 288-byte block in a local buffer as **72 entries of 4
bytes each** (`mov byte [ebp + esi*4 - 0x204]`, …), writing entry-type
constants `0x80`, `0x00` and `0x04` into the first byte of each record. That
is a button/macro action table, not firmware. Opcode `0x05` (64 bytes) is
reached from the macro dialog path.

This resolves the outstanding bricking hazard from the strategy document: no
code path in this binary writes firmware. The remaining caution is opcode
`0xFA`, which is almost certainly a flash commit and should not be called in a
loop.

### 4.5 Non-HID path

`fcn.00420030` opens a handle and issues a raw `DeviceIoControl`, then closes
it. Called once from `0x0041574A`. This is a Windows-specific IOCTL — likely
`IOCTL_HID_GET_FEATURE` or a hub query — and has no bearing on the port.

## 5. What Phase A cannot tell you

Static analysis gives structure. These still need dynamic capture:

- **DPI encoding.** `fcn.00415890` contains `mov eax, 0x1E` followed by
  `lea ecx, [eax + eax*4]` (a multiply by 5), which suggests DPI is stored as
  a scaled integer, but the scale factor is not provable statically.
- **LED channel order** (RGB vs. GRB) and effect IDs.
- **Button action encoding** — the meaning of the `0x80` / `0x00` / `0x04`
  record types in the 4-byte table entries.
- **Which config block each of `0x81`–`0x86` returns.**
- **Polling-rate encoding.**

All five fall out of a one-setting-at-a-time differential capture.
