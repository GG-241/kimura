# Phase B: Empirical Protocol Discovery

Since the Windows binary's opcode echo protocol doesn't work on real hardware, we'll reverse-engineer the actual protocol by **observation**: send commands, watch for LED/DPI changes, and document what works.

## Strategy

1. **Target LED first** — easiest to observe
2. **Then DPI** — noticeable pointer speed changes
3. **Then button mapping** (if discoverable without detailed inspection)
4. **Document patterns** — opcode ranges, payload structure, value encodings

## Quick Start

```bash
# List devices
python3 phase-b/empirical_probe.py list

# Try LED control commands (watch the LED)
python3 phase-b/empirical_probe.py led

# Try DPI commands (move the mouse, feel the speed)
python3 phase-b/empirical_probe.py dpi

# Send custom raw hex commands
python3 phase-b/empirical_probe.py send "04 01"
python3 phase-b/empirical_probe.py send "01 FF"
```

## Exploration Log

As you test, record what you observe in this section. For example:

```
Test: send "01 00"
  → No visible change

Test: send "04 01"
  → LED turned BLUE, steady (not breathing)

Test: send "02 FF 00"
  → LED turned RED

Test: send "F5"
  → Settings "applied" (if there's a confirmation LED flash)
```

## Hypothesis Framework

As you observe changes, build and test hypotheses:

- **Opcode hypothesis:** "Opcodes 0x01–0x06 are settings, 0xF5 is commit"
- **Payload hypothesis:** "The first byte is a setting type (0=DPI, 1=LED, 2=rate), second byte is the value"
- **Color hypothesis:** "Byte 1 = R, Byte 2 = G, Byte 3 = B" or reversed

Test each hypothesis by:
1. Varying **only one byte** at a time
2. Predicting the outcome before sending
3. Recording what actually happened
4. Refining the hypothesis

## Safe Exploration Boundaries

✅ **Safe to try:**
- Sending different bytes to report_id=0, feature_len=65
- LED color/mode changes
- DPI changes
- Button remapping (observing which buttons respond)

❌ **Dangerous / defer:**
- Opcodes that might write to flash (we don't know which yet)
- Bulk transfers (0x05, 0x07 — macro/button table upload)
- The 0xFA opcode (likely flash commit)

## Next: Analysis

Once you've found working commands:

1. Extract the opcode/payload patterns
2. Update `PROTOCOL.md` with the **real** working opcodes
3. Modify `phase-b/empirical_probe.py` to use discovered commands
4. Build a simple `set_led()` / `set_dpi()` function to prove reproducibility

## References

- `PROTOCOL.md` §4: Opcode table from Windows binary (use as inspiration only)
- `kimura.py`: Reference implementation showing the HID transport layer
