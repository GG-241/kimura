#!/usr/bin/env python3
"""
kimura_gui.py — cross-platform (Linux + macOS) control panel for the Kimura
v3.0 (MS-4300WG) mouse. Sidebar-navigation, card-based layout built on
CustomTkinter, using kimura.py's portable transport underneath (the same
hidapi/libusb backend the `kimura` CLI uses — not the Linux-only hidraw
backend from the research tooling).

Pages:
  - Device — status, battery (see kimura.read_battery()'s docstring —
    UNCONFIRMED reading, not independently verified yet)
  - LED — preset control (write, confirmed-safe presets only)
  - Button Remap — write, EXPERIMENTAL, same caveats as `kimura remap`
  - Live — read-only button/scroll indicator

Run with:  kimura-gui        (installed via pip, see pyproject.toml)
        or python3 kimura_gui.py
"""

import importlib.resources
import queue
import sys
import threading
import time
import traceback
import webbrowser

try:
    import tkinter as tk
    from tkinter import messagebox
except ImportError:
    sys.exit(
        "Missing dependency: tkinter.\n"
        "Install it with:\n"
        "  Linux (Debian/Ubuntu):  sudo apt install python3-tk\n"
        "  macOS (Homebrew):       brew install python-tk\n")

try:
    import customtkinter as ctk
except ImportError:
    sys.exit("Missing dependency. Install with:  pip install customtkinter")

try:
    from PIL import Image, ImageTk
except ImportError:
    sys.exit("Missing dependency. Install with:  pip install Pillow")

import kimura as k  # noqa: E402 — checks its own `hid` dependency on import
import kimura_tray  # noqa: E402 — degrades gracefully if pystray/PIL extras are missing

ctk.set_appearance_mode("system")
ctk.set_default_color_theme("blue")

POLL_MS = 30
ISSUE_URL = k.ISSUE_URL


def load_mouse_image():
    """Load assets/mouse_top.webp from the kimura_assets package. Works
    uniformly whether running from source, pip-installed, or PyInstaller-
    frozen (all three make kimura_assets importable with its data intact —
    for PyInstaller, via `--collect-data kimura_assets` in the build
    scripts). Returns None if it can't be found."""
    try:
        ref = importlib.resources.files("kimura_assets") / "mouse_top.webp"
        with importlib.resources.as_file(ref) as path:
            return Image.open(path).convert("RGBA")
    except Exception:
        return None


MOUSE_IMAGE_SRC_SIZE = 1200  # native size of kimura_assets/mouse_top.webp (square)
DIAGRAM_DISPLAY_SIZE = 260

# Button positions in the ORIGINAL 1200x1200 product photo's coordinate
# space (measured from the image itself — see git history for the
# detection script). Left/middle/right are visible from this top-down
# shot; the side buttons and underside button6 aren't visible from directly
# above, so their markers sit at the image edges as leader-line-style
# annotations rather than pointing at something literally visible.
DIAGRAM_POINTS = {
    "left": (500, 430, "1"),
    "middle": (599, 330, "2"),
    "right": (700, 430, "3"),
    "side_forward": (410, 630, "4"),
    "side_back": (410, 760, "5"),
    "button6": (599, 960, "6"),
}


ACCENT = "#8b7fe8"
ACCENT_HOVER = "#7566d9"
ROOT_BG = ("#eef0fb", "#14152a")
PANEL_BG = ("#e4e6fb", "#191a33")
CARD_BG = ("#f8f9ff", "#1e2036")


class KimuraGUI(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Kimura v3.0 Control")
        self.geometry("860x580")
        self.minsize(780, 520)
        self.configure(fg_color=ROOT_BG)
        try:
            # Window/taskbar icon — the AppImage/.app bundle icons (set at
            # build time, see packaging/build_appimage.sh and build_dmg.sh)
            # cover the launcher/dock entry, but the live Tk window itself
            # falls back to Tk's default feather icon unless set here too.
            icon_img = load_mouse_image()
            if icon_img is not None:
                self._window_icon = ImageTk.PhotoImage(icon_img.resize((128, 128), Image.LANCZOS))
                self.iconphoto(True, self._window_icon)
        except Exception:
            pass
        try:
            # Slight overall window transparency for a softer, more
            # "ethereal" look. Supported on X11 (compositor required),
            # macOS, and Windows; silently no-op-ish elsewhere.
            self.attributes("-alpha", 0.94)
        except Exception:
            pass

        self.dev = None        # Kimura instance (vendor channel), for writes
        self.watch_dev = None  # raw hid.device (generic mouse channel), read-only
        self._watch_dev_info = None  # enumerate_candidates() dict for watch_dev, for reattach on close
        self._current_page = None
        self.button_labels = {}
        self.pages = {}
        self.nav_buttons = {}
        self._busy_buttons = []  # disabled together during any async device op
        self._closed = False

        self._build_layout()
        self.report_callback_exception = self._log_callback_exception
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # System tray icon (optional — degrades to no-op if unavailable, see
        # kimura_tray.py). Its callbacks run on the tray's own thread, so
        # they only push onto this queue; _poll_input() (already running on
        # the Tk thread) drains it.
        self._tray_state = kimura_tray.TrayState()
        self._tray_queue = queue.Queue()
        self._tray_icon = kimura_tray.start_tray(
            self._tray_state,
            show_callback=lambda: self._tray_queue.put("show"),
            quit_callback=lambda: self._tray_queue.put("quit"))

        self.refresh_device()
        self._last_dpi_poll = 0.0
        self._last_tray_poll = 0.0
        self.after(POLL_MS, self._poll_input)

    # -- overall layout ---------------------------------------------------
    def _build_layout(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        sidebar = ctk.CTkFrame(self, width=180, corner_radius=0, fg_color=PANEL_BG)
        sidebar.grid(row=0, column=0, rowspan=2, sticky="nswe")

        ctk.CTkLabel(sidebar, text="Kimura", font=ctk.CTkFont(size=22, weight="bold"),
                    text_color=ACCENT).grid(row=0, column=0, padx=20, pady=(24, 0), sticky="w")
        ctk.CTkLabel(sidebar, text="v3.0 (MS-4300WG)", font=ctk.CTkFont(size=11),
                    text_color="gray60").grid(row=1, column=0, padx=20, pady=0, sticky="w")
        ctk.CTkLabel(sidebar, text="App v%s" % k.__version__, font=ctk.CTkFont(size=10),
                    text_color="gray50").grid(row=2, column=0, padx=20, pady=(0, 24), sticky="w")

        for i, name in enumerate(("Device", "LED", "Button Remap", "Live"), start=3):
            btn = ctk.CTkButton(sidebar, text=name, anchor="w", corner_radius=8,
                                fg_color="transparent", text_color=("gray10", "gray90"),
                                hover_color=("#d3d6fb", "#2a2c4d"),
                                command=lambda n=name: self._show_page(n))
            btn.grid(row=i, column=0, padx=12, pady=4, sticky="we")
            self.nav_buttons[name] = btn

        topbar = ctk.CTkFrame(self, corner_radius=0, height=54, fg_color=PANEL_BG)
        topbar.grid(row=0, column=1, sticky="we")
        self.status_var = ctk.StringVar(value="Scanning...")
        ctk.CTkLabel(topbar, textvariable=self.status_var,
                    font=ctk.CTkFont(size=13, weight="bold")).pack(side="left", padx=16, pady=12)
        self.battery_var = ctk.StringVar(value="")
        ctk.CTkLabel(topbar, textvariable=self.battery_var,
                    font=ctk.CTkFont(size=13)).pack(side="left", padx=4)
        ctk.CTkButton(topbar, text="Report an Issue", width=130, fg_color="transparent",
                     border_width=1, border_color=("gray70", "gray40"),
                     text_color=("gray20", "gray80"), hover_color=("#d3d6fb", "#2a2c4d"),
                     command=lambda: webbrowser.open(ISSUE_URL)).pack(
            side="right", padx=(0, 8), pady=10)
        self._refresh_btn = ctk.CTkButton(topbar, text="Refresh", width=90, fg_color=ACCENT,
                                          hover_color=ACCENT_HOVER, command=self.refresh_device)
        self._refresh_btn.pack(side="right", padx=16, pady=10)

        content = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        content.grid(row=1, column=1, sticky="nswe", padx=18, pady=18)

        self.pages["Device"] = self._build_device_page(content)
        self.pages["LED"] = self._build_led_page(content)
        self.pages["Button Remap"] = self._build_remap_page(content)
        self.pages["Live"] = self._build_live_page(content)
        self._show_page("Device")

    def _show_page(self, name):
        for n, page in self.pages.items():
            (page.pack(fill="both", expand=True) if n == name else page.pack_forget())
        for n, btn in self.nav_buttons.items():
            btn.configure(fg_color=("#d3d6fb", "#2a2c4d") if n == name else "transparent")
        self._current_page = name
        # Only hold the generic mouse collection open while the Live page
        # is actually visible — see _open_watch_dev()'s docstring for why.
        if name == "Live":
            self._open_watch_dev()
        else:
            self._close_watch_dev()

    # -- device / battery ---------------------------------------------------
    def _build_device_page(self, parent):
        page = ctk.CTkFrame(parent, corner_radius=14, fg_color=CARD_BG)
        ctk.CTkLabel(page, text="Device", font=ctk.CTkFont(size=18, weight="bold")).pack(
            anchor="w", padx=24, pady=(22, 4))
        self.device_detail_var = ctk.StringVar(value="")
        ctk.CTkLabel(page, textvariable=self.device_detail_var, justify="left", anchor="w",
                    font=ctk.CTkFont(size=13)).pack(anchor="w", padx=24, pady=(0, 8))
        note = ("Battery reading is UNCONFIRMED — a plausible value found on a\n"
                "previously-unread channel, not yet independently cross-checked\n"
                "against the vendor GUI or an actual charge-level change.")
        ctk.CTkLabel(page, text=note, justify="left", anchor="w", text_color="gray60",
                    font=ctk.CTkFont(size=11)).pack(anchor="w", padx=24, pady=(0, 16))

        ctk.CTkLabel(page, text="Mouse Details", font=ctk.CTkFont(size=14, weight="bold")).pack(
            anchor="w", padx=24, pady=(0, 6))
        self.mouse_details_var = ctk.StringVar(value="Click Refresh to read details.")
        ctk.CTkLabel(page, textvariable=self.mouse_details_var, justify="left", anchor="w",
                    font=ctk.CTkFont(size=12)).pack(anchor="w", padx=24, pady=(0, 6))
        self.raw_details_box = ctk.CTkTextbox(page, height=110, font=ctk.CTkFont(size=11, family="monospace"))
        self.raw_details_box.pack(fill="x", padx=24, pady=(0, 6))
        self.raw_details_box.insert("1.0", "(raw diagnostic block dump appears here after Refresh)")
        self.raw_details_box.configure(state="disabled")
        ctk.CTkLabel(page, text="Raw diagnostic dump — several opcodes above are still "
                                "unconfirmed (see PROTOCOL.md).", justify="left", anchor="w",
                    text_color="gray60", font=ctk.CTkFont(size=11)).pack(
            anchor="w", padx=24, pady=(0, 16))

        btn = ctk.CTkButton(page, text="Factory Reset", fg_color="#b03a3a", hover_color="#8f2e2e",
                           command=self._factory_reset)
        btn.pack(anchor="w", padx=24, pady=(0, 22))
        self._busy_buttons.append(btn)
        return page

    def _refresh_mouse_details(self):
        if not self.dev:
            self.mouse_details_var.set("No device connected.")
            self._set_raw_details_text("(no device connected)")
            return
        d = self.dev.info
        pid = d.get("product_id", 0)
        summary = (
            "Connection: %s (product string: %r)\n"
            "PID: 0x%04X   Interface: %s\n"
            "Path: %s"
            % (k.connection_type(pid), (d.get("product_string") or "").strip(),
               pid, d.get("interface_number"), d["path"].decode(errors="replace")))
        self.mouse_details_var.set(summary + "\nDPI stage: reading...")
        self._set_raw_details_text("(reading...)")
        self._refresh_btn.configure(state="disabled")

        # device_details() does 5 Feature-report round trips — async so a
        # slow/hung read (see _run_async()'s docstring) doesn't freeze the
        # whole window on every Refresh click.
        dev = self.dev

        def worker():
            return dev.device_details()

        def done(details, error):
            self._refresh_btn.configure(state="normal")
            if error:
                k.log.error("Mouse Details refresh failed: %s", error)
                self.mouse_details_var.set("Could not read device details: %s" % error)
                self._set_raw_details_text("")
                return
            dpi = details["dpi_stage"]
            self.mouse_details_var.set(
                summary + "\nDPI stage: %s" % (dpi if dpi is not None else "unknown"))
            lines = []
            for op, rx in details["blocks"].items():
                if isinstance(rx, tuple):
                    lines.append("0x%02X  %s: %s" % (op, rx[0], rx[1]))
                else:
                    lines.append("0x%02X  %s" % (op, " ".join("%02X" % b for b in rx[:8])))
            self._set_raw_details_text("\n".join(lines))

        self._run_async(worker, done)

    def _set_raw_details_text(self, text):
        self.raw_details_box.configure(state="normal")
        self.raw_details_box.delete("1.0", "end")
        self.raw_details_box.insert("1.0", text)
        self.raw_details_box.configure(state="disabled")

    def _factory_reset(self):
        if not self.dev:
            messagebox.showerror("No device", "No transport established. Click Refresh.")
            return
        if not messagebox.askyesno(
                "Confirm Factory Reset",
                "This will flash:\n"
                "  - button table -> FACTORY (left/right/middle/back/forward click, "
                "underside = DPI cycle)\n"
                "  - LED preset   -> Neon\n"
                "  - one flash commit\n\n"
                "Any custom button remap will be lost. Continue?"):
            return
        dev = self.dev
        self._set_busy(True, "Applying factory reset...")

        def worker():
            dev.apply_button_table({}, led_preset=k.LED_ALIASES["default"])

        def done(result, error):
            self._set_busy(False)
            if error:
                k.log.error("Factory Reset failed: %s", error)
                messagebox.showerror("Error", str(error))
            else:
                messagebox.showinfo("Done", "Factory bundle applied.\n\n"
                                    "Physically verify every button and the LED now.")

        self._run_async(worker, done)

    def refresh_device(self):
        self._close_devices()

        cands = k.enumerate_candidates(verbose=False)
        if not cands:
            self.status_var.set("NOT DETECTED — plug in the mouse and click Refresh")
            self.battery_var.set("")
            self.device_detail_var.set("")
            self._tray_state.connected = False
            self._tray_state.battery_pct = None
            self._tray_state.dpi_stage = None
            self._refresh_mouse_details()
            return

        order = k.order_candidates(cands)
        chosen_d = None
        for d in order:
            kk = k.probe(d, k.DEFAULT_REPORT_IDS, k.DEFAULT_LENGTHS, verbose=False)
            if kk:
                self.dev = kk
                chosen_d = d
                mfg = (d.get("manufacturer_string") or "").strip()
                prod = (d.get("product_string") or "").strip()
                self.status_var.set(("DETECTED — %s %s" % (mfg, prod)).strip())
                self.device_detail_var.set("PID 0x%04X, interface %s" % (
                    d.get("product_id", 0), d.get("interface_number")))
                break
        else:
            self.status_var.set("Found a 0x248A device but couldn't establish transport")

        # Battery: a short-lived open+read+close, NOT held open continuously
        # — see _open_watch_dev()'s docstring for why holding this interface
        # open breaks the mouse as a normal pointing device.
        generic_d = k.pick_generic_candidate(cands)
        if generic_d is not None and chosen_d is not None and generic_d["path"] == chosen_d["path"]:
            pct = k.read_battery(self.dev.dev)  # Feature-report read, no interrupt claim needed
        else:
            wdev, wd = k.open_generic_mouse_collection(verbose=False)
            pct = k.read_battery(wdev) if wdev else None
            if wdev:
                wdev.close()
                k.reattach_kernel_driver(k.VID, wd.get("product_id", 0), wd.get("interface_number"))
        if pct is not None:
            self.battery_var.set("· Battery: ~%d%% (unconfirmed)" % pct)
            self._tray_state.battery_pct = pct
        else:
            self.battery_var.set("")
            self._tray_state.battery_pct = None
        self._tray_state.connected = self.dev is not None
        self._refresh_mouse_details()

        # Re-open the Live-tab watch handle if that's the page showing —
        # refresh_device() (via _close_devices() above) just closed it.
        if self._current_page == "Live":
            self._open_watch_dev()

    def _open_watch_dev(self):
        """Open the generic mouse collection for the Live tab's live
        button/movement display. ONLY called while that page is actually
        visible (see _show_page()) — see the big warning in
        _close_watch_dev() for why this can't be held open all the time.
        """
        if self.watch_dev or not self.dev:
            return
        cands = k.enumerate_candidates(verbose=False)
        generic_d = k.pick_generic_candidate(cands)
        if generic_d is not None and generic_d["path"] == self.dev.info["path"]:
            self.dev.dev.set_nonblocking(1)
            self.watch_dev = self.dev.dev
            self._watch_dev_info = None  # shared with self.dev — its own cleanup covers it
        else:
            wdev, wd = k.open_generic_mouse_collection(verbose=False)
            if wdev:
                wdev.set_nonblocking(1)
            self.watch_dev = wdev
            self._watch_dev_info = wd

    def _close_watch_dev(self):
        """Close the Live tab's watch handle and reattach its kernel
        driver.

        CONFIRMED real-world need (2026-09-24): opening the generic mouse
        collection (interface 0 — see open_generic_mouse_collection()'s
        docstring) and reading raw Input reports from it (what the Live
        tab does) requires hidapi to exclusively claim that interface via
        libusb on Linux, detaching it from the kernel's OWN mouse driver
        for as long as the handle stays open — NOT just on a crash. This
        was reproduced live: simply having the app running with this
        handle open (independent of any error) made the interface show as
        claimed by `usbfs` instead of `usbhid`, breaking the system mouse
        cursor for the whole session, on both Linux (confirmed) and
        (reported, mechanism unconfirmed) macOS. The mitigation is to only
        hold this handle open while the Live page is actually visible —
        see _show_page() — not for the app's whole lifetime.
        """
        if not self.watch_dev:
            return
        shared = self.dev is not None and self.watch_dev is self.dev.dev
        if not shared:
            try:
                self.watch_dev.close()
            except Exception:
                pass
            if self._watch_dev_info:
                k.reattach_kernel_driver(k.VID, self._watch_dev_info.get("product_id", 0),
                                         self._watch_dev_info.get("interface_number"))
        self.watch_dev = None
        self._watch_dev_info = None

    def _close_devices(self):
        self._close_watch_dev()
        if self.dev:
            try:
                self.dev.close()  # Kimura.close() — closes + reattaches the kernel driver
            except Exception:
                pass
            self.dev = None

    # -- async device I/O ---------------------------------------------------
    def _run_async(self, worker, on_done):
        """Run a blocking Kimura call on a background thread so the GUI
        stays responsive no matter what the device does.

        CONFIRMED real risk this addresses (2026-09-24): hidapi's
        get_feature_report() can raise OSError on macOS mid-operation —
        kimura.py's _tx()/_rx() now normalize that into a catchable
        KimuraError, but there's no cross-platform guarantee a hung
        (rather than erroring) HID call always returns in bounded time.
        Since all device I/O previously ran directly in a button's Tk
        callback, a genuine hang would freeze the whole window (unmovable,
        unclosable) for as long as the underlying call blocks. Running it
        here instead keeps the Tk main loop pumping regardless.

        `worker()` takes no args and returns a result (or raises).
        `on_done(result, error)` is called back on the MAIN thread via
        `after()` once the worker finishes — exactly one of result/error
        is not None. Buttons that trigger this should disable themselves
        first and re-enable in `on_done`, since Kimura isn't safe for
        concurrent calls from multiple threads.
        """
        def run():
            try:
                result = worker()
            except Exception as e:
                self.after(0, lambda: on_done(None, e))
            else:
                self.after(0, lambda: on_done(result, None))
        threading.Thread(target=run, daemon=True).start()

    def _set_busy(self, busy, status_text=None):
        """Disable/enable the write-action buttons during an async op, and
        optionally show a status message (restored automatically when
        busy=False). Prevents concurrent device access from two buttons at
        once (Kimura isn't thread-safe for that) and gives visible
        feedback that something is happening instead of the button just
        looking unresponsive."""
        state = "disabled" if busy else "normal"
        for btn in self._busy_buttons:
            btn.configure(state=state)
        if busy:
            self._status_before_busy = self.status_var.get()
            if status_text is not None:
                self.status_var.set(status_text)
        else:
            self.status_var.set(getattr(self, "_status_before_busy", self.status_var.get()))

    # -- LED page ----------------------------------------------------------
    def _build_led_page(self, parent):
        page = ctk.CTkFrame(parent, corner_radius=14, fg_color=CARD_BG)
        ctk.CTkLabel(page, text="LED", font=ctk.CTkFont(size=18, weight="bold")).pack(
            anchor="w", padx=24, pady=(22, 12))

        row = ctk.CTkFrame(page, fg_color="transparent")
        row.pack(anchor="w", padx=24, fill="x")

        names = ["%s (0x%02X)" % (k.LED_PRESETS[p], p) for p in sorted(k.LED_PRESETS)]
        self._led_name_to_value = dict(zip(names, sorted(k.LED_PRESETS)))
        self.led_var = ctk.StringVar(value=names[0] if names else "")
        ctk.CTkComboBox(row, variable=self.led_var, values=names, width=280,
                        state="readonly").pack(side="left")
        btn = ctk.CTkButton(row, text="Apply", width=90, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                           command=self._apply_led)
        btn.pack(side="left", padx=(12, 0))
        self._busy_buttons.append(btn)
        return page

    def _apply_led(self):
        if not self.dev:
            messagebox.showerror("No device", "No transport established. Click Refresh.")
            return
        name = self.led_var.get()
        preset = self._led_name_to_value.get(name)
        if preset is None:
            return
        if not messagebox.askyesno(
                "Confirm",
                "Send LED preset %s to the mouse?\n\n"
                "This also resends the button-remap table so the change survives "
                "replug/power-cycle — any button slot not set via Button Remap will "
                "be (re)set to its factory default." % name):
            return
        dev = self.dev
        self._set_busy(True, "Sending LED preset...")

        def worker():
            dev.set_led(preset, persist=True)

        def done(result, error):
            self._set_busy(False)
            if error:
                k.log.error("LED Apply failed: %s", error)
                messagebox.showerror("Error", str(error))
            else:
                messagebox.showinfo("Sent", "LED preset sent and committed to flash.")

        self._run_async(worker, done)

    # -- Remap page (EXPERIMENTAL) ------------------------------------------
    def _build_remap_page(self, parent):
        page = ctk.CTkFrame(parent, corner_radius=14, fg_color=CARD_BG)
        ctk.CTkLabel(page, text="Button Remap", font=ctk.CTkFont(size=18, weight="bold")).pack(
            anchor="w", padx=24, pady=(22, 4))

        warn = ("EXPERIMENTAL — not verified on real hardware. Any button left on\n"
                "\"(factory default)\" is still RESET to that default on Apply — there is\n"
                "no way to read the mouse's current table back first. This also resends\n"
                "the LED preset and other settings. See README.md before using.")
        ctk.CTkLabel(page, text=warn, justify="left", anchor="w", text_color="#d94848",
                    font=ctk.CTkFont(size=11)).pack(anchor="w", padx=24, pady=(0, 14))

        body = ctk.CTkFrame(page, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=(0, 20))
        body.grid_columnconfigure(1, weight=1)

        diagram = tk.Canvas(body, width=DIAGRAM_DISPLAY_SIZE, height=DIAGRAM_DISPLAY_SIZE,
                           highlightthickness=0, bg=self._canvas_bg())
        diagram.grid(row=0, column=0, sticky="n", padx=(0, 24))
        self._draw_mouse_diagram(diagram)

        form = ctk.CTkFrame(body, fg_color="transparent")
        form.grid(row=0, column=1, sticky="we")

        self.remap_vars = {}
        action_names = ["(factory default)"] + sorted(k.BUTTON_ACTIONS)
        for i, (slot, _) in enumerate(k.BUTTON_SLOTS):
            num = DIAGRAM_POINTS[slot][2]
            ctk.CTkLabel(form, text="%s  %s" % (num, slot.replace("_", " ").title()),
                        anchor="w", width=170).grid(row=i, column=0, sticky="w", pady=4)
            var = ctk.StringVar(value="(factory default)")
            ctk.CTkComboBox(form, variable=var, values=action_names, width=220,
                            state="readonly").grid(row=i, column=1, pady=4, padx=(8, 0))
            self.remap_vars[slot] = var

        btn = ctk.CTkButton(page, text="Apply Remap", fg_color=ACCENT, hover_color=ACCENT_HOVER,
                           command=self._apply_remap)
        btn.pack(anchor="w", padx=24, pady=(0, 20))
        self._busy_buttons.append(btn)
        return page

    def _canvas_bg(self):
        mode = ctk.get_appearance_mode()
        return "#242424" if mode == "Dark" else "#f2f2f2"

    def _draw_mouse_diagram(self, canvas):
        # Real product photo (assets/mouse_top.webp) with numbered markers
        # overlaid at positions measured from the image. Left/middle/right
        # are visible from this top-down shot; the side buttons and
        # underside button6 aren't visible from directly above, so their
        # markers sit near the image edges as leader-style annotations.
        scale = DIAGRAM_DISPLAY_SIZE / MOUSE_IMAGE_SRC_SIZE
        img = load_mouse_image()
        if img is not None:
            img = img.resize((DIAGRAM_DISPLAY_SIZE, DIAGRAM_DISPLAY_SIZE), Image.LANCZOS)
            self._mouse_photo = ImageTk.PhotoImage(img)  # keep a reference — Tk drops GC'd images
            canvas.create_image(0, 0, anchor="nw", image=self._mouse_photo)
        else:
            canvas.create_rectangle(0, 0, DIAGRAM_DISPLAY_SIZE, DIAGRAM_DISPLAY_SIZE,
                                    fill=self._canvas_bg(), outline="")
            canvas.create_text(DIAGRAM_DISPLAY_SIZE // 2, DIAGRAM_DISPLAY_SIZE // 2,
                               text="(mouse image not found)", fill="gray")

        for slot, (x, y, num) in DIAGRAM_POINTS.items():
            sx, sy = x * scale, y * scale
            canvas.create_oval(sx - 12, sy - 12, sx + 12, sy + 12,
                              fill="#f2f2f2", outline="#3a5fa0", width=2)
            canvas.create_text(sx, sy, text=num, font=("", 11, "bold"), fill="#3a5fa0")

    def _apply_remap(self):
        if not self.dev:
            messagebox.showerror("No device", "No transport established. Click Refresh.")
            return
        overrides = {slot: var.get() for slot, var in self.remap_vars.items()
                     if var.get() != "(factory default)"}
        if not overrides:
            messagebox.showinfo("Nothing to do", "No slots changed from factory default.")
            return
        summary = "\n".join("  %s -> %s" % (s, v) for s, v in overrides.items())
        if not messagebox.askyesno(
                "Confirm EXPERIMENTAL write",
                "About to write:\n%s\n\nAll other slots reset to factory default.\n"
                "This write path is unverified on real hardware. Continue?" % summary):
            return
        dev = self.dev
        self._set_busy(True, "Sending button table...")

        def worker():
            dev.apply_button_table(overrides)

        def done(result, error):
            self._set_busy(False)
            if error:
                k.log.error("Button Remap Apply failed: %s", error)
                messagebox.showerror("Error", str(error))
            else:
                messagebox.showinfo("Sent", "Button table sent.\n\n"
                                    "Physically test every remapped button now — this write "
                                    "path has no independent verification beyond that.")

        self._run_async(worker, done)

    # -- Live page (read-only) ------------------------------------------
    def _build_live_page(self, parent):
        page = ctk.CTkFrame(parent, corner_radius=14, fg_color=CARD_BG)
        ctk.CTkLabel(page, text="Live (read-only)", font=ctk.CTkFont(size=18, weight="bold")).pack(
            anchor="w", padx=24, pady=(22, 16))

        row = ctk.CTkFrame(page, fg_color="transparent")
        row.pack(padx=24, pady=(0, 12))
        for name in k.BUTTON_NAMES.values():
            lbl = ctk.CTkLabel(row, text=name, width=80, height=32, corner_radius=8,
                               fg_color=("gray80", "gray25"))
            lbl.pack(side="left", padx=4)
            self.button_labels[name] = lbl

        self.wheel_var = ctk.StringVar(value="Wheel: —")
        ctk.CTkLabel(page, textvariable=self.wheel_var).pack(anchor="w", padx=24, pady=(0, 20))
        return page

    def _poll_input(self):
        if self.watch_dev:
            any_buttons = 0
            wheel_total = 0
            moved = False
            got = False
            while True:
                try:
                    data = self.watch_dev.read(64)
                except Exception:
                    break
                if not data:
                    break
                decoded = k.decode_mouse_report(data)
                if decoded:
                    buttons, _bits, dx, dy, wheel = decoded
                    any_buttons |= buttons
                    wheel_total += wheel
                    if dx or dy:
                        moved = True
                    got = True
            if moved:
                self._tray_state.last_movement_ts = time.time()
            if got:
                for bit, name in k.BUTTON_NAMES.items():
                    pressed = bool(any_buttons & (1 << bit))
                    self.button_labels[name].configure(
                        fg_color="#4caf50" if pressed else ("gray80", "gray25"))
                self.wheel_var.set("Wheel: %+d" % wheel_total if wheel_total else "Wheel: —")
            else:
                for lbl in self.button_labels.values():
                    lbl.configure(fg_color=("gray80", "gray25"))

        # DPI stage is a Feature-report round-trip on the vendor channel —
        # poll it far less often than the 30ms Input-report loop above.
        now = time.time()
        if self.dev and now - self._last_dpi_poll > 2.0:
            self._last_dpi_poll = now
            self._tray_state.dpi_stage = self.dev.read_dpi_stage()

        # macOS tray is redrawn from this (main) thread — on darwin,
        # kimura_tray attaches a refresh hook to the icon instead of running
        # thread, because pystray's AppKit setters may only be touched from
        # the main thread (see kimura_tray.py).
        if self._tray_icon and now - self._last_tray_poll > 1.0:
            self._last_tray_poll = now
            getattr(self._tray_icon, "_kimura_refresh", lambda: None)()

        self._drain_tray_queue()
        if not self._closed:
            self.after(POLL_MS, self._poll_input)

    def _drain_tray_queue(self):
        while True:
            try:
                cmd = self._tray_queue.get_nowait()
            except queue.Empty:
                break
            if cmd == "show":
                self.deiconify()
                self.lift()
                self.focus_force()
            elif cmd == "quit":
                self._on_close()

    def _on_close(self):
        if self._closed:
            return
        self._closed = True
        self._close_devices()
        if self._tray_icon:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
        self.destroy()

    def _log_callback_exception(self, exc_type, exc_value, tb):
        """Replaces Tk's default report_callback_exception (which only
        prints to stderr — invisible in the --windowed/frozen builds most
        users actually run). Logs to ~/.kimura/kimura.log AND still prints,
        so a Terminal-launched run shows the same thing as before. This is
        the safety net for any callback exception NOT already caught by a
        specific `except k.KimuraError` handler — e.g. the DPI-poll crash
        that motivated this (kimura.py's _tx()/_rx() now normalize hidapi's
        raw OSErrors into KimuraError, but this stays as defense in depth
        for anything else that slips through uncaught)."""
        k.log.error("Uncaught Tkinter callback exception", exc_info=(exc_type, exc_value, tb))
        traceback.print_exception(exc_type, exc_value, tb)


def main():
    print("kimura-gui %s" % k.__version__, file=sys.stderr)
    app = KimuraGUI()
    # A SIGTERM (process manager, `timeout`, force-quit) bypasses
    # WM_DELETE_WINDOW/_on_close() entirely by default, skipping device
    # cleanup — see _close_watch_dev()'s docstring for why that matters
    # (leaves a kernel driver detached). Route it through the same
    # graceful shutdown path instead.
    import signal
    signal.signal(signal.SIGTERM, lambda signum, frame: app._on_close())
    app.mainloop()


if __name__ == "__main__":
    sys.exit(main() or 0)
