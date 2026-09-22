"""
kimura_tray.py — optional system tray icon for kimura-gui.

Shows a Kimura icon in the system tray (GNOME/KDE top bar on Linux, menu
bar on macOS) with a click menu for connection status, battery, and DPI
stage, plus checkable toggles for which of those show up in the icon's
hover tooltip. "Show Kimura GUI" and "Quit" round out the menu.

Gracefully does nothing (TRAY_AVAILABLE = False) if no tray backend is
available — the main GUI works fine either way. On Ubuntu/GNOME this
needs the AppIndicator GObject bindings:

    sudo apt install gir1.2-ayatanaappindicator3-0.1

Without that package, this module tries a plain-Xorg fallback (works on
some window managers, but GNOME specifically won't render it without the
"AppIndicator and KStatusNotifierItem Support" shell extension either way)
before giving up and disabling itself.
"""

import os
import queue
import sys
import threading
import time

TRAY_AVAILABLE = False
pystray = None
Image = None
ImageDraw = None

try:
    import pystray as _pystray
    from PIL import Image as _Image, ImageDraw as _ImageDraw
    pystray = _pystray
    Image = _Image
    ImageDraw = _ImageDraw
    TRAY_AVAILABLE = True
except Exception:
    try:
        os.environ.setdefault("PYSTRAY_BACKEND", "xorg")
        import pystray as _pystray
        from PIL import Image as _Image, ImageDraw as _ImageDraw
        pystray = _pystray
        Image = _Image
        ImageDraw = _ImageDraw
        TRAY_AVAILABLE = True
    except Exception:
        TRAY_AVAILABLE = False

if TRAY_AVAILABLE:
    # Eagerly load the PIL codecs that Image.save() would otherwise import
    # lazily on first use. pystray's macOS backend serialises the tray icon
    # to PNG (Image.save -> _assert_image) on its background threads, and a
    # lazy import there can race the main thread's garbage collector and
    # SIGABRT the process in frozen (PyInstaller) builds — so load them all
    # here, on the main thread, before any tray thread exists.
    from PIL import BmpImagePlugin, GifImagePlugin, JpegImagePlugin, PngImagePlugin, PpmImagePlugin  # noqa: F401


class TrayState:
    """Shared, thread-safe-enough (GIL-protected simple attribute writes)
    status snapshot — the GUI updates this from its own poll loop, the tray
    thread reads it. No locking: individual attribute reads/writes are
    atomic in CPython, and staleness by a poll tick is harmless here."""

    def __init__(self):
        self.connected = False
        self.battery_pct = None
        self.dpi_stage = None
        self.last_movement_ts = 0.0

    def is_moving(self, window=1.0):
        return (time.time() - self.last_movement_ts) < window


def start_tray(state, show_callback, quit_callback):
    """Start the tray icon in a background thread. Returns the pystray Icon
    (already running detached) or None if unavailable.

    `show_callback`/`quit_callback` are called from the TRAY thread, not the
    Tk thread — they must only do thread-safe things, like pushing onto a
    queue the main loop drains (see kimura_gui.py's _poll_input)."""
    if not TRAY_AVAILABLE:
        return None

    prefs = {"battery": True, "dpi": True, "movement": True}

    def make_icon_image(size=64):
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        color = (139, 127, 232, 255) if state.connected else (140, 140, 140, 255)
        d.ellipse((6, 4, size - 6, size - 4), fill=color)
        d.ellipse((size // 2 - 4, 10, size // 2 + 4, size // 2 - 2), fill=(255, 255, 255, 180))
        return img

    def tooltip_text():
        parts = ["Kimura"]
        parts.append("Connected" if state.connected else "Not detected")
        if prefs["battery"] and state.battery_pct is not None:
            parts.append("%d%%" % state.battery_pct)
        if prefs["dpi"] and state.dpi_stage is not None:
            parts.append("DPI stage %d" % state.dpi_stage)
        if prefs["movement"]:
            parts.append("Moving" if state.is_moving() else "Idle")
        return " · ".join(parts)

    def toggle(key):
        def _toggle(icon, item):
            prefs[key] = not prefs[key]
        return _toggle

    def checked(key):
        return lambda item: prefs[key]

    def status_text(label, key, fmt):
        def _text(item):
            val = getattr(state, key)
            return label + (fmt(val) if val is not None else "unavailable")
        return _text

    menu = pystray.Menu(
        pystray.MenuItem("Show Kimura GUI", lambda icon, item: show_callback()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(status_text("Battery: ", "battery_pct", lambda v: "~%d%% (unconfirmed)" % v),
                         None, enabled=False),
        pystray.MenuItem(status_text("DPI stage: ", "dpi_stage", lambda v: str(v)),
                         None, enabled=False),
        pystray.MenuItem(lambda item: "Movement: %s" % ("Moving" if state.is_moving() else "Idle"),
                         None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Show battery in tooltip", toggle("battery"), checked=checked("battery")),
        pystray.MenuItem("Show DPI stage in tooltip", toggle("dpi"), checked=checked("dpi")),
        pystray.MenuItem("Show movement in tooltip", toggle("movement"), checked=checked("movement")),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", lambda icon, item: (quit_callback(), icon.stop())),
    )

    icon = pystray.Icon("kimura-gui", make_icon_image(), "Kimura", menu)

    def refresh_once():
        try:
            if icon.visible:
                if state.connected != refresh_once.last_connected:
                    icon.icon = make_icon_image()
                    refresh_once.last_connected = state.connected
                icon.title = tooltip_text()
        except Exception:
            pass

    refresh_once.last_connected = None

    if sys.platform == "darwin":
        # macOS: pystray's AppKit backend must not be run() on a background
        # thread — AppKit UI is main-thread only, and creating the status
        # item off it SIGABRTs the whole process. run_detached() creates
        # the status item right here on the caller's (Tk main) thread
        # instead; Tk's mainloop then pumps the NSApplication events the
        # menu needs.
        #
        # The no-op setup matters: pystray's default setup would flip
        # visible = True on its own background thread, which runs _show()
        # (AppKit + PIL) off the main thread. Show the icon here instead.
        #
        # Tray redraws are also main-thread work: refresh_once() touches
        # icon.icon/icon.title (AppKit setters), and calling that from a
        # background thread races the main thread's garbage collector and
        # intermittently SIGABRTs. So no refresh thread here — start_tray
        # attaches refresh_once to the icon, and kimura_gui's poll loop
        # (Tk main thread) calls it about once a second. Everything pystray
        # touches stays on the main thread.
        icon.run_detached(setup=lambda _icon: None)
        icon.visible = True
        icon._kimura_refresh = refresh_once
    else:
        def refresh_loop():
            while True:
                refresh_once()
                time.sleep(1.0)

        threading.Thread(target=refresh_loop, daemon=True).start()
        threading.Thread(target=icon.run, daemon=True).start()
    return icon
