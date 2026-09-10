#!/usr/bin/env python3
"""
Read-only status/recognition GUI for the Kimura v3.0 mouse.

Shows that the software actually recognizes the connected mouse and its
confirmed features: device identity, transport status, and a live button/
scroll indicator. This does NOT write anything to the device — no LED
control, no DPI, no button remapping. That's a deliberate scope choice (see
the plan this was built from): a demo/validation tool, not a control panel.

Linux-only (hidraw-backed), same platform scope as empirical_probe.py, which
this file imports from directly rather than duplicating device/report logic.

Usage:
    python3 phase-b/gui_status.py
"""

import tkinter as tk
from tkinter import ttk

from empirical_probe import (
    VID, PID, VENDOR_IFACE, VENDOR_REPORT_ID, VENDOR_FEATURE_LEN,
    find_all_devices, unique_interfaces, open_device, _decode_input_report,
)

POLL_MS = 20

# Keep this in sync with phase-a/PROTOCOL.md by hand — it changes rarely
# enough that live-parsing the markdown isn't worth the complexity.
PROTOCOL_STATUS_LINES = [
    ("Transport", "CONFIRMED — interface 1, report ID 7, 8-byte Feature buffer"),
    ("Read-all opcodes", "CONFIRMED — 0x81/0x86/0x82/0x83/0x84 echo correctly"),
    ("LED presets", "CONFIRMED — opcode 0x03, ~28 presets mapped (0x00-0x1B); 0xFF crashes firmware"),
    ("Buttons/scroll", "CONFIRMED — standard Input report, report_id=1, interface 0 (see below)"),
    ("DPI", "READ-ONLY — 0x82 reads current stage (0-5); write opcode unfound (0x04, 0x82 ruled out)"),
    ("Button remap table", "GATED — opcode 0x07 bulk write not wired up, needs a real capture first"),
]

BUTTON_BITS = [
    ("Left", 0), ("Right", 1), ("Middle", 2), ("Back", 3), ("Forward", 4),
]


class KimuraStatusGUI:
    def __init__(self, root):
        self.root = root
        self.root.title("Kimura v3.0 — Status")
        self.targets = []  # list of (iface_num, dev)
        self.button_labels = {}

        self._build_device_panel()
        self._build_protocol_panel()
        self._build_button_panel()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh_devices()
        self.root.after(POLL_MS, self._poll_input)

    # -- Device panel --------------------------------------------------
    def _build_device_panel(self):
        frame = ttk.LabelFrame(self.root, text="Device")
        frame.pack(fill="x", padx=8, pady=6)

        self.device_status_var = tk.StringVar(value="Scanning...")
        ttk.Label(frame, textvariable=self.device_status_var, font=("", 11, "bold")).pack(anchor="w", padx=6, pady=(4, 0))

        self.device_detail_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.device_detail_var, justify="left").pack(anchor="w", padx=6, pady=(0, 4))

        ttk.Button(frame, text="Refresh", command=self.refresh_devices).pack(anchor="e", padx=6, pady=(0, 4))

    def refresh_devices(self):
        # Close any previously-open handles before re-scanning.
        for _, dev in self.targets:
            try:
                dev.close()
            except Exception:
                pass
        self.targets = []

        devices = unique_interfaces(find_all_devices())
        if not devices:
            self.device_status_var.set(f"NOT DETECTED (looking for VID 0x{VID:04X} / PID 0x{PID:04X})")
            self.device_detail_var.set("")
            return

        mfg = (devices[0].get("manufacturer_string") or "").strip()
        prod = (devices[0].get("product_string") or "").strip()
        self.device_status_var.set(f"DETECTED — {mfg} {prod}".strip())

        lines = [f"VID=0x{VID:04X} PID=0x{PID:04X}"]
        for d in devices:
            up = d.get("usage_page") or 0
            iface = d.get("interface_number", "?")
            tag = " (vendor channel)" if up == 0xFF01 else ""
            lines.append(f"  iface={iface} usage_page=0x{up:04X}{tag}")
        self.device_detail_var.set("\n".join(lines))

        # Open interfaces 0/1/2 (non-blocking) for the live indicator, same
        # set cmd_capture_buttons() watches.
        for iface_num in (0, 1, 2):
            d = next((x for x in devices if x.get("interface_number") == iface_num), None)
            if d:
                try:
                    dev = open_device(d)
                    dev.set_nonblocking(1)
                    self.targets.append((iface_num, dev))
                except Exception:
                    pass

    # -- Protocol status panel ------------------------------------------
    def _build_protocol_panel(self):
        frame = ttk.LabelFrame(self.root, text="Confirmed protocol status (see phase-a/PROTOCOL.md)")
        frame.pack(fill="x", padx=8, pady=6)
        for label, status in PROTOCOL_STATUS_LINES:
            row = ttk.Frame(frame)
            row.pack(fill="x", padx=6, pady=1)
            ttk.Label(row, text=f"{label}:", width=20, anchor="w").pack(side="left")
            ttk.Label(row, text=status, anchor="w", wraplength=420).pack(side="left")

    # -- Live button indicator ------------------------------------------
    def _build_button_panel(self):
        frame = ttk.LabelFrame(self.root, text="Live button/scroll indicator (read-only)")
        frame.pack(fill="x", padx=8, pady=6)

        row = ttk.Frame(frame)
        row.pack(padx=6, pady=6)
        for name, _bit in BUTTON_BITS:
            lbl = tk.Label(row, text=name, width=8, relief="raised", bg="#dddddd")
            lbl.pack(side="left", padx=3)
            self.button_labels[name] = lbl

        self.wheel_var = tk.StringVar(value="Wheel: —")
        ttk.Label(frame, textvariable=self.wheel_var).pack(anchor="w", padx=6, pady=(0, 6))

        self.raw_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.raw_var, font=("monospace", 9)).pack(anchor="w", padx=6, pady=(0, 6))

    def _poll_input(self):
        # Interface 0 emits movement reports far faster than we poll (up to
        # 1000 Hz), so a single dev.read() per tick falls permanently behind
        # if the mouse is being moved at all — a quick button click can get
        # buried under queued movement reports and never surface. Drain the
        # kernel buffer completely every tick instead of reading just once.
        # any_buttons_seen accumulates bits across the WHOLE drain so a
        # press+release that both happened within one tick still shows.
        any_buttons_seen = 0
        wheel_total = 0
        last_raw = None

        for iface_num, dev in self.targets:
            while True:
                try:
                    data = dev.read(64)
                except Exception:
                    break
                if not data:
                    break
                if iface_num == 0 and data[0] == 1 and len(data) >= 7:
                    any_buttons_seen |= data[1]
                    wheel = data[6] - 256 if data[6] > 127 else data[6]
                    wheel_total += wheel
                    last_raw = data

        if last_raw is not None:
            for name, bit in BUTTON_BITS:
                pressed = bool(any_buttons_seen & (1 << bit))
                self.button_labels[name].config(bg="#66cc66" if pressed else "#dddddd")
            if wheel_total > 0:
                self.wheel_var.set(f"Wheel: UP (+{wheel_total})")
            elif wheel_total < 0:
                self.wheel_var.set(f"Wheel: DOWN ({wheel_total})")
            else:
                self.wheel_var.set("Wheel: —")
            self.raw_var.set("raw (last): " + " ".join("%02X" % b for b in last_raw[:7]))
        else:
            # Nothing arrived this tick — clear any stale "pressed" state
            # from the previous tick so buttons don't stay stuck lit.
            for name, _bit in BUTTON_BITS:
                self.button_labels[name].config(bg="#dddddd")

        self.root.after(POLL_MS, self._poll_input)

    def _on_close(self):
        for _, dev in self.targets:
            try:
                dev.close()
            except Exception:
                pass
        self.root.destroy()


def main():
    root = tk.Tk()
    KimuraStatusGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
