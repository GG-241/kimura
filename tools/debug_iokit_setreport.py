#!/usr/bin/env python3
"""Direct IOKit experiments on the receiver's vendor HID interface.

Enumerates IOHIDDevice services directly (IOHIDManager's matching path
segfaults under ctypes), opens each vendor interface, and tries
IOHIDDeviceSetReport with kIOHIDReportTypeOutput in several shapes,
reporting raw IOReturn codes (with mach_error_string) and timing.
Harmless: payload is 32 zero bytes destined for the page-1 scratch
buffer, and no 0xFA commit is sent.
"""
import ctypes
import ctypes.util
import time

IOKIT = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
CF = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))

CFStringCreateWithCString = CF.CFStringCreateWithCString
CFNumberGetValue = CF.CFNumberGetValue

IOHIDDeviceCreate = IOKIT.IOHIDDeviceCreate
IOHIDDeviceOpen = IOKIT.IOHIDDeviceOpen
IOHIDDeviceGetProperty = IOKIT.IOHIDDeviceGetProperty
IOHIDDeviceSetReport = IOKIT.IOHIDDeviceSetReport
IOHIDDeviceSetReportWithCallback = IOKIT.IOHIDDeviceSetReportWithCallback
IOServiceGetMatchingServices = IOKIT.IOServiceGetMatchingServices
IOServiceMatching = IOKIT.IOServiceMatching
IOIteratorNext = IOKIT.IOIteratorNext
IOObjectRelease = IOKIT.IOObjectRelease
IORegistryEntryCreateCFProperty = IOKIT.IORegistryEntryCreateCFProperty

IOHIDDeviceCreate.restype = ctypes.c_void_p
IOHIDDeviceCreate.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
IOHIDDeviceOpen.restype = ctypes.c_int
IOHIDDeviceOpen.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
IOHIDDeviceGetProperty.restype = ctypes.c_void_p
IOHIDDeviceGetProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
IOHIDDeviceSetReport.restype = ctypes.c_int
IOHIDDeviceSetReport.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                 ctypes.c_void_p, ctypes.c_long]
IOHIDDeviceSetReportWithCallback.restype = ctypes.c_int
IOHIDDeviceSetReportWithCallback.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                             ctypes.c_void_p, ctypes.c_long,
                                             ctypes.c_double, ctypes.c_void_p, ctypes.c_void_p]
IOServiceGetMatchingServices.restype = ctypes.c_int
IOServiceGetMatchingServices.argtypes = [ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p]
IOServiceMatching.restype = ctypes.c_void_p
IOServiceMatching.argtypes = [ctypes.c_char_p]
IOIteratorNext.restype = ctypes.c_void_p
IOIteratorNext.argtypes = [ctypes.c_void_p]
IOObjectRelease.argtypes = [ctypes.c_void_p]
IORegistryEntryCreateCFProperty.restype = ctypes.c_void_p
IORegistryEntryCreateCFProperty.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                            ctypes.c_void_p, ctypes.c_uint32]

CF.CFStringCreateWithCString.restype = ctypes.c_void_p
CF.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
CF.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
CF.CFNumberGetValue.argtypes = [ctypes.c_void_p, ctypes.c_long, ctypes.c_void_p]

kIOHIDReportTypeInput, kIOHIDReportTypeOutput, kIOHIDReportTypeFeature = 0, 1, 2


def cfstr(s):
    return CFStringCreateWithCString(None, s.encode(), 0x08000100)


def get_str(dev, key):
    ref = IOHIDDeviceGetProperty(dev, cfstr(key))
    if not ref:
        return None
    buf = ctypes.create_string_buffer(256)
    CF.CFStringGetCString(ctypes.c_void_p(ref), buf, 256, 0x08000100)
    return buf.value.decode()


def get_int(ref_ptr, key):
    """Read a numeric property from an IOHIDDeviceRef or IOService."""
    ref = ctypes.c_void_p(IORegistryEntryCreateCFProperty(ref_ptr, cfstr(key),
                                                          None, 0))
    if not ref:
        return None
    val = ctypes.c_long()
    CFNumberGetValue(ref, 4, ctypes.byref(val))  # kCFNumberSInt64Type
    return val.value


def errstr(r):
    r &= 0xFFFFFFFF
    IOKIT.mach_error_string.restype = ctypes.c_char_p
    s = IOKIT.mach_error_string(ctypes.c_int(r))
    return f"{r:#x} ({s.decode() if s else '?'})"


def main():
    iter_handle = ctypes.c_void_p()
    IOServiceGetMatchingServices(0, IOServiceMatching(b"IOHIDDevice"),
                                 ctypes.byref(iter_handle))
    service = IOIteratorNext(iter_handle)
    found = []
    while service:
        up = get_int(service, "PrimaryUsagePage")
        vid = get_int(service, "VendorID")
        if vid == 0x248A and up == 0xFF01:
            found.append(service)
        else:
            IOObjectRelease(service)
        service = IOIteratorNext(iter_handle)

    print(f"{len(found)} vendor-interface HID service(s)")
    for i, service in enumerate(found):
        dev = IOHIDDeviceCreate(None, service)
        product = get_str(dev, "Product")
        print(f"\n=== device {i}: {product} ===")
        r = IOHIDDeviceOpen(dev, 0)
        print(f"  open -> {errstr(r)}")
        buf33 = (ctypes.c_ubyte * 33)(7, *([0] * 32))
        buf32 = (ctypes.c_ubyte * 32)(*([0] * 32))

        t0 = time.time()
        r = IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, 7, buf33, 33)
        print(f"  SetReport(Output, id=7, 33B incl id) -> {errstr(r)}  ({time.time()-t0:.2f}s)")

        t0 = time.time()
        r = IOHIDDeviceSetReport(dev, kIOHIDReportTypeOutput, 7, buf32, 32)
        print(f"  SetReport(Output, id=7, 32B no id)   -> {errstr(r)}  ({time.time()-t0:.2f}s)")

        # async variant with 2s timeout — does it complete or time out?
        t0 = time.time()
        r = IOHIDDeviceSetReportWithCallback(dev, kIOHIDReportTypeOutput, 7, buf33, 33, 2.0, None, None)
        print(f"  SetReportWithCallback(Output, 33B, 2s) -> {errstr(r)}  ({time.time()-t0:.2f}s)")

        IOObjectRelease(service)

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())