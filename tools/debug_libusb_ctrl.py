#!/usr/bin/env python3
"""Try a raw HID class SET_REPORT(Output) control transfer to EP0 via
libusb — replicating exactly what Windows' HidD_SetOutputReport does,
which is how the vendor GUI writes the 32-byte page data.
Harmless: payload is report id 7 + 32 zero bytes (page-1 scratch data),
no 0xFA commit is sent.
"""
import ctypes


class DeviceDescriptor(ctypes.Structure):
    _fields_ = [("bLength", ctypes.c_uint8), ("bDescriptorType", ctypes.c_uint8),
                ("bcdUSB", ctypes.c_uint16), ("bDeviceClass", ctypes.c_uint8),
                ("bDeviceSubClass", ctypes.c_uint8), ("bDeviceProtocol", ctypes.c_uint8),
                ("bMaxPacketSize0", ctypes.c_uint8), ("idVendor", ctypes.c_uint16),
                ("idProduct", ctypes.c_uint16), ("bcdDevice", ctypes.c_uint16),
                ("iManufacturer", ctypes.c_uint8), ("iProduct", ctypes.c_uint8),
                ("iSerialNumber", ctypes.c_uint8), ("bNumConfigurations", ctypes.c_uint8)]


lib = ctypes.CDLL("/opt/homebrew/lib/libusb-1.0.dylib")
lib.libusb_init(None)
lib.libusb_get_device_list.restype = ctypes.POINTER(ctypes.c_void_p)
lib.libusb_get_device_list.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))]
lib.libusb_get_device_descriptor.restype = ctypes.c_int
lib.libusb_get_device_descriptor.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
lib.libusb_open.restype = ctypes.c_int
lib.libusb_open.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
lib.libusb_control_transfer.restype = ctypes.c_int
lib.libusb_control_transfer.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint8,
                                        ctypes.c_uint16, ctypes.c_uint16,
                                        ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint]
lib.libusb_close.argtypes = [ctypes.c_void_p]
lib.libusb_get_bus_number.restype = ctypes.c_uint8
lib.libusb_get_device_address.restype = ctypes.c_uint8

ctx = ctypes.c_void_p()
devs = ctypes.POINTER(ctypes.c_void_p)()
lib.libusb_get_device_list(ctx, ctypes.byref(devs))

targets = []
i = 0
while devs[i]:
    desc = DeviceDescriptor()
    if lib.libusb_get_device_descriptor(devs[i], ctypes.byref(desc)) == 0 and desc.idVendor == 0x248A:
        targets.append(devs[i])
    i += 1
print(f"{len(targets)} Kimura USB device(s)")

for dev in targets:
    handle = ctypes.c_void_p()
    if lib.libusb_open(dev, ctypes.byref(handle)) != 0:
        print("  open failed")
        continue
    bus, addr = lib.libusb_get_bus_number(dev), lib.libusb_get_device_address(dev)
    print(f"=== bus {bus} addr {addr} ===")
    # HID class SET_REPORT: bmRequestType 0x21, bRequest 0x09,
    # wValue 0x0207 (Output|report id 7), wIndex = interface 1
    buf = (ctypes.c_ubyte * 33)(7, *([0] * 32))
    n = lib.libusb_control_transfer(handle, 0x21, 0x09, 0x0207, 1, buf, 33, 5000)
    print(f"  SET_REPORT(Output,id7) via EP0 -> {n}")
    if n < 0:
        n = lib.libusb_control_transfer(handle, 0x21, 0x09, 0x0207, 0, buf, 33, 5000)
        print(f"  retry wIndex=0 -> {n}")
    lib.libusb_close(handle)

lib.libusb_free_device_list(devs, 1)
lib.libusb_exit(None)
print("DONE (no commit sent)")