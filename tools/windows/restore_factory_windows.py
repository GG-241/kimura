#!/usr/bin/env python3
r"""Factory restore for the Kimura v3.0 (MS-4300WG) — WINDOWS ONLY.

Run this inside a Windows VM with the mouse passed through (the same
setup used to make the Wireshark captures). It replays the vendor GUI's
own captured "apply settings" bundle with:
  - page 0 = the captured FACTORY-DEFAULT button table
  - page 1 = zeros
  - LED preset 0x00 (Neon)
  - one 0xFA flash commit at the end

Why Windows: the 32-byte page data must be written as a control-plane
HID SET_REPORT — exactly what HidD_SetOutputReport() does. (hidapi's
hid_write on Windows and macOS both use the interrupt-OUT pipe, which
this mouse's firmware never services, so those paths time out.)

Usage (in the VM):
    python restore_factory_windows.py
It asks for confirmation before sending. Requires no extra packages.
"""
import ctypes
import sys
import time
from ctypes import wintypes

hid = ctypes.WinDLL("hid.dll")
setupapi = ctypes.WinDLL("setupapi.dll")
kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)


class GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


class SP_DEVICE_INTERFACE_DATA(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("InterfaceClassGuid", GUID),
                ("Flags", ctypes.c_ulong), ("Reserved", ctypes.c_void_p)]


class SP_DEVICE_INTERFACE_DETAIL_DATA_W(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("DevicePath", ctypes.c_wchar * 1)]


# --- setupapi plumbing ------------------------------------------------------
SetupDiGetClassDevsW = setupapi.SetupDiGetClassDevsW
SetupDiGetClassDevsW.restype = ctypes.c_void_p
SetupDiGetClassDevsW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_ulong]
SetupDiEnumDeviceInterfaces = setupapi.SetupDiEnumDeviceInterfaces
SetupDiEnumDeviceInterfaces.restype = ctypes.c_int
SetupDiEnumDeviceInterfaces.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                        ctypes.c_ulong, ctypes.c_void_p]
SetupDiGetDeviceInterfaceDetailW = setupapi.SetupDiGetDeviceInterfaceDetailW
SetupDiGetDeviceInterfaceDetailW.restype = ctypes.c_int
SetupDiGetDeviceInterfaceDetailW.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                                             ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
                                             ctypes.c_void_p]
SetupDiDestroyDeviceInfoList = setupapi.SetupDiDestroyDeviceInfoList
SetupDiDestroyDeviceInfoList.argtypes = [ctypes.c_void_p]

hid.HidD_GetHidGuid.argtypes = [ctypes.c_void_p]
hid.HidD_SetFeature.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
hid.HidD_SetFeature.restype = ctypes.c_int
hid.HidD_GetFeature.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
hid.HidD_GetFeature.restype = ctypes.c_int
hid.HidD_SetOutputReport.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
hid.HidD_SetOutputReport.restype = ctypes.c_int

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_RW = 3
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value or 0


def hid_paths():
    """Enumerate present HID device-interface paths."""
    guid = GUID()
    hid.HidD_GetHidGuid(ctypes.byref(guid))
    hdev = SetupDiGetClassDevsW(ctypes.byref(guid), None, None, 0x12)  # PRESENT|INTERFACE
    paths = []
    idx = 0
    while True:
        did = SP_DEVICE_INTERFACE_DATA()
        did.cbSize = ctypes.sizeof(SP_DEVICE_INTERFACE_DATA)
        if not SetupDiEnumDeviceInterfaces(hdev, None, ctypes.byref(guid), idx, ctypes.byref(did)):
            break
        need = ctypes.c_ulong(0)
        SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did), None, 0, ctypes.byref(need), None)
        buf = ctypes.create_string_buffer(need.value)
        # cbSize of the unicode detail struct is 8 on x64 (4 + 1 wchar + pad)
        ctypes.cast(buf, ctypes.POINTER(ctypes.c_ulong))[0] = 8
        if SetupDiGetDeviceInterfaceDetailW(hdev, ctypes.byref(did), buf, need.value, None, None):
            paths.append(ctypes.wstring_at(ctypes.addressof(buf) + 4))
        idx += 1
    SetupDiDestroyDeviceInfoList(hdev)
    return paths


def open_dev(path):
    handle = kernel32.CreateFileW(path, GENERIC_READ | GENERIC_WRITE,
                                  FILE_SHARE_RW, None, OPEN_EXISTING, 0, None)
    if handle in (INVALID_HANDLE_VALUE, 0) or handle == -1:
        return None
    return ctypes.c_void_p(handle)


def set_feature(handle, data8):
    buf = (ctypes.c_ubyte * 8).from_buffer_copy(data8)
    return bool(hid.HidD_SetFeature(handle, buf, 8))


def get_feature(handle):
    buf = (ctypes.c_ubyte * 8)()
    if hid.HidD_GetFeature(handle, buf, 8):
        return bytes(buf)
    return None


def set_output(handle, data33):
    buf = (ctypes.c_ubyte * 33).from_buffer_copy(data33)
    return bool(hid.HidD_SetOutputReport(handle, buf, 33))


def find_vendor_transport():
    """Find the interface whose feature channel echoes opcode 0x81."""
    for path in hid_paths():
        handle = open_dev(path)
        if not handle:
            continue
        rx = None
        try:
            set_feature(handle, bytes([0x07, 0x81, 0, 0, 0, 0, 0, 0]))
            time.sleep(0.05)
            rx = get_feature(handle)
            if rx and rx[1] == 0x81:
                return path, handle
        finally:
            if rx is None:
                kernel32.CloseHandle(handle)
    return None, None


# Captured factory-default page 0 (six 4-byte slots + zero padding).
PAGE0 = bytes.fromhex("0100f0000100f1000100f2000100f3000100f40007000300") + bytes(8)
assert len(PAGE0) == 32, len(PAGE0)
PAGE1 = bytes(32)


def out33(page_data):
    return bytes([0x07]) + page_data


def main():
    path, handle = find_vendor_transport()
    if not handle:
        print("vendor transport NOT found — is the mouse attached to the VM?")
        return 1
    print(f"vendor transport OK: {path}\n")

    print("This will flash:")
    print("  - button table -> FACTORY (A=left B=right C=middle D=back E=forward F=dpi_cycle)")
    print("  - LED preset   -> 0x00 (Neon)")
    print("  - one 0xFA flash commit")
    answer = input('Type RESTORE to proceed: ').strip().upper()
    if answer != "RESTORE":
        print("aborted — nothing written")
        return 2

    def step(op, payload=b"", sleep=0.05):
        pkt = bytes([0x07, op]) + bytes(payload).ljust(6, b"\x00")[:6]
        assert len(pkt) == 8, len(pkt)
        if not set_feature(handle, pkt):
            raise RuntimeError(f"SetFeature opcode 0x{op:02X} failed")
        time.sleep(sleep)

    step(0xF5, sleep=0.27)                                      # begin edit session
    step(0x05, bytes([0x02, 0x00, 0x20, 0x00, 0x00, 0x00]))     # select page 0
    if not set_output(handle, out33(PAGE0)):
        print("page-0 output write FAILED — aborting before commit")
        return 3
    time.sleep(0.05)
    step(0x05, bytes([0x02, 0x01, 0x20, 0x00, 0x00, 0x00]))     # select page 1
    if not set_output(handle, out33(PAGE1)):
        print("page-1 output write FAILED — aborting before commit")
        return 3
    time.sleep(0.05)
    step(0x03, bytes([0x00, 0, 0, 0, 0, 0]))                    # LED = Neon
    step(0x02, bytes([0x04, 0x04, 0, 0, 0, 0]))
    step(0x04, bytes([0x00, 0x66, 0x66, 0, 0, 0]))
    step(0x01, bytes([0x08, 0, 0, 0, 0, 0]))
    step(0x06, bytes([0, 0, 0, 0, 0, 0]))
    step(0xFA, sleep=0.2)                                       # single flash commit

    time.sleep(0.2)
    set_feature(handle, bytes([0x07, 0x81, 0, 0, 0, 0, 0, 0]))
    time.sleep(0.05)
    print("ping after commit:", get_feature(handle))
    print("DONE — factory bundle applied")
    return 0


if __name__ == "__main__":
    sys.exit(main())