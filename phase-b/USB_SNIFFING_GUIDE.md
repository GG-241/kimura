# USB Traffic Sniffing Guide for Kimura Protocol Reverse-Engineering

Since standard HID APIs (on macOS and Linux) don't show the device responding to commands, we need to capture the **actual USB traffic** to understand the real protocol.

## Quick Start

### Step 1: Install capture tools

```bash
sudo apt-get install tcpdump tshark wireshark
```

### Step 2: Start capturing USB traffic

**Terminal 1 — Start capture:**

```bash
sudo bash phase-b/usb_capture.sh capture
```

This will start tcpdump capturing USB traffic. You should see output like:

```
[INFO] Found device: Bus 001, Device 005
Bus 001 Device 005: ID 248a:5b49 Telink Semiconductor Corp. Wireless Receiver
[INFO] Starting USB traffic capture on bus 001
[SUCCESS] Capture started (PID: 12345)
[INFO] Waiting for capture... (press Ctrl+C to stop)
```

The capture is now running. Leave this terminal open.

### Step 3: Send test commands

**Terminal 2 — Send commands while capture is running:**

```bash
cd ~/Kimura_Drivers

# Send various test patterns
python3 phase-b/empirical_probe.py send "04 01"
sleep 0.5
python3 phase-b/empirical_probe.py send "02 FF 00"
sleep 0.5
python3 phase-b/empirical_probe.py send "01 00"
sleep 0.5
python3 phase-b/empirical_probe.py send "F5"
```

Watch the mouse LED/pointer while sending. Note which commands seem to have any effect (even if subtle).

### Step 4: Stop capture and analyze

**Terminal 1 — Stop capture:**

Press `Ctrl+C`. The script will print the PCAP filename and analysis instructions.

**Analyze the captured traffic:**

```bash
sudo bash phase-b/usb_capture.sh analyze kimura_usb_capture_*.pcap
```

Or open in Wireshark (GUI):

```bash
wireshark kimura_usb_capture_*.pcap
```

## What to Look For in the Capture

The USB traffic will show:

1. **Host → Device (requests):**
   - These are our commands being sent
   - Look for repeating patterns
   - Note the byte sequences

2. **Device → Host (responses):**
   - The device's reply data
   - Should be non-zero if the device processes commands
   - Look for changes corresponding to your LED/DPI observations

3. **Timing:**
   - Gaps between send and receive
   - Should be around 10ms (the SETTLE delay we've been using)

## Analysis Strategy

For **each command you test**:

1. **Document the hex bytes sent** (e.g., "04 01")
2. **Look up that command in the capture**
3. **Find the device's response**
4. **Compare responses** — which bytes changed?
5. **Correlate with observations** — did the LED change, pointer speed change, etc.?

### Example analysis flow:

```
Command sent: 04 01
Expected in PCAP: Search for bytes "04 01" going to the device
Look for response: Next packet from device should echo or acknowledge
If LED changed: That response byte might be related to LED control
```

## Using Wireshark (GUI - Easier)

1. Open the PCAP file in Wireshark:
   ```bash
   wireshark kimura_usb_capture_*.pcap
   ```

2. **Filter by USB device:**
   - In the filter bar: `usb.bus_id == "001"` (replace 001 with your bus number)

3. **Look for patterns:**
   - Expand packets to see raw data
   - Follow USB requests/responses
   - Note repeated byte sequences

4. **Export data for analysis:**
   - Right-click packet → Copy → Bytes (Hex Stream)
   - Paste into a text editor to analyze patterns

## Troubleshooting

### "tcpdump: Permission denied"

Use `sudo`:
```bash
sudo bash phase-b/usb_capture.sh capture
```

### "usbmon not found"

Load the module:
```bash
sudo modprobe usbmon
```

### "Mouse not found in lsusb"

Verify it's plugged in:
```bash
lsusb | grep -i 248a
```

### PCAP file is huge

Kill the capture after a shorter time. Only 30 seconds of data is usually plenty. You can also filter in Wireshark to view only the traffic you care about.

## Next Steps

Once you've captured and analyzed the traffic:

1. **Document the opcodes:**
   - Write down which bytes correspond to which commands
   - Note the structure: opcode + payload

2. **Create empirical findings:**
   - Update `EMPIRICAL_DISCOVERY.md` with real protocol findings
   - Test predictions: "If I send these bytes, the LED should turn blue"

3. **Modify empirical_probe.py:**
   - Update the hardcoded opcodes with real working ones
   - Test each one to confirm

## Advanced: Filter USB Traffic

If the full capture is too noisy, use tcpdump filters:

```bash
# Capture only to/from our device
sudo tcpdump -i usbmon1 '(usb.bus_id == "001") and (usb.device_address == 005)' -w capture.pcap

# Capture only Feature Requests (more likely to be our commands)
sudo tcpdump -i usbmon1 'usb.bmRequestType == 0x21' -w capture.pcap
```

## References

- [Wireshark USB Protocol Analysis](https://wiki.wireshark.org/USB)
- [tcpdump Manual](https://www.tcpdump.org/)
- [USB HID Protocol Basics](https://www.usb.org/sites/default/files/hid1_11.pdf)
