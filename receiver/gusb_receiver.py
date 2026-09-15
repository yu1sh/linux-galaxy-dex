#!/usr/bin/env python3
"""Receive a view-only Android MediaProjection stream over AOA/GUSB.

The program intentionally uses libusb through ctypes instead of requiring a
Python USB package. Linux uses the system libusb-1.0 library; Windows uses a
colocated libusb-1.0.dll and a WinUSB-compatible device driver.
The Android app enters Open Accessory mode, sends GUSB/1 frames over its bulk
IN endpoint, and receives control frames on bulk OUT.
"""

from __future__ import annotations

import argparse
import array
import ctypes
import ctypes.util
import json
import math
import os
import shutil
import subprocess
import sys
import threading
import time
import queue
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Sequence

try:  # Pipe diagnostics are optional and unavailable on Windows.
    import fcntl
    import termios
except ImportError:  # pragma: no cover - exercised by the Windows CI job.
    fcntl = None
    termios = None

try:  # Direct execution from receiver/gusb_receiver.py.
    from protocol import (
        AOA_IDENTIFIER_STRINGS,
        HELLO_PAYLOAD,
        FLAG_CODEC_CONFIG,
        FLAG_END_OF_ACCESS_UNIT,
        FLAG_KEY_FRAME,
        TYPE_ERROR,
        TYPE_HELLO,
        TYPE_INFO,
        TYPE_STOP,
        TYPE_VIDEO,
        Frame,
        FrameError,
        FrameParser,
        encode_frame,
    )
except ImportError:  # ``python -m receiver.gusb_receiver``.
    from .protocol import (
        AOA_IDENTIFIER_STRINGS,
        HELLO_PAYLOAD,
        FLAG_CODEC_CONFIG,
        FLAG_END_OF_ACCESS_UNIT,
        FLAG_KEY_FRAME,
        TYPE_ERROR,
        TYPE_HELLO,
        TYPE_INFO,
        TYPE_STOP,
        TYPE_VIDEO,
        Frame,
        FrameError,
        FrameParser,
        encode_frame,
    )


# Android Open Accessory protocol constants.
AOA_VENDOR_ID = 0x18D1
AOA_DATA_PRODUCT_IDS = frozenset((0x2D00, 0x2D01, 0x2D04, 0x2D05))
AOA_AUDIO_ONLY_PRODUCT_IDS = frozenset((0x2D02, 0x2D03))
AOA_GET_PROTOCOL = 51
AOA_SEND_STRING = 52
AOA_START = 53
AOA_REQUEST_IN = 0xC0
AOA_REQUEST_OUT = 0x40
AOA_STRING_MAX = 255

LIBUSB_ENDPOINT_IN = 0x80
LIBUSB_TRANSFER_TYPE_MASK = 0x03
LIBUSB_TRANSFER_TYPE_BULK = 0x02
LIBUSB_CLASS_VENDOR_SPEC = 0xFF
LIBUSB_SUBCLASS_VENDOR_SPEC = 0xFF
LIBUSB_PROTOCOL_VENDOR_SPEC = 0x00

LIBUSB_ERROR_IO = -1
LIBUSB_ERROR_INVALID_PARAM = -2
LIBUSB_ERROR_ACCESS = -3
LIBUSB_ERROR_NO_DEVICE = -4
LIBUSB_ERROR_NOT_FOUND = -5
LIBUSB_ERROR_BUSY = -6
LIBUSB_ERROR_TIMEOUT = -7
LIBUSB_ERROR_OVERFLOW = -8
LIBUSB_ERROR_PIPE = -9
LIBUSB_ERROR_INTERRUPTED = -10
LIBUSB_ERROR_NO_MEM = -11
LIBUSB_ERROR_NOT_SUPPORTED = -12
LIBUSB_ERROR_OTHER = -99

DEFAULT_TIMEOUT_MS = 2_000
HELLO_TIMEOUT_SECONDS = 120.0
BULK_TIMEOUT_MS = 500
READ_SIZE = 16_384
POLL_INTERVAL_SECONDS = 0.10
DEFAULT_QUEUE_MAX_ITEMS = 3
DEFAULT_QUEUE_MAX_BYTES = 1_500_000
DEFAULT_QUEUE_MAX_AGE_SECONDS = 0.25
MAX_ACCESS_UNIT_BYTES = 8 * 1024 * 1024
MAX_CODEC_CONFIG_BYTES = 1024 * 1024


@dataclass(frozen=True)
class AccessUnit:
    payload: bytes
    flags: int
    created: float


class LatestFrameQueue:
    """Bounded FIFO for complete access units with IDR-based overflow recovery."""

    def __init__(
        self,
        max_items=DEFAULT_QUEUE_MAX_ITEMS,
        max_bytes=DEFAULT_QUEUE_MAX_BYTES,
        max_age_seconds=DEFAULT_QUEUE_MAX_AGE_SECONDS,
    ):
        if max_items < 1 or max_bytes < 1 or max_age_seconds <= 0:
            raise ValueError("queue limits must be positive")
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.max_age_seconds = max_age_seconds
        self._items = deque()
        self._bytes = 0
        self._lock = threading.Lock()
        self.awaiting_keyframe = True
        self.dropped_units = 0

    def put(self, unit: AccessUnit) -> None:
        with self._lock:
            is_keyframe = bool(unit.flags & FLAG_KEY_FRAME)
            if self.awaiting_keyframe:
                if not is_keyframe:
                    self.dropped_units += 1
                    return
                self._items.clear()
                self._bytes = 0
                self.awaiting_keyframe = False

            self._items.append(unit)
            self._bytes += len(unit.payload)
            self._enforce_limits_locked(time.monotonic())

    def _over_limit_locked(self, now: float) -> bool:
        return bool(
            len(self._items) > self.max_items
            or self._bytes > self.max_bytes
            or (self._items and now - self._items[0].created > self.max_age_seconds)
        )

    def _enforce_limits_locked(self, now: float) -> None:
        while self._over_limit_locked(now):
            # A predictive AU can only be decoded after the most recent IDR.
            # Trim complete reference chains, never individual P frames.
            next_keyframe = next(
                (
                    index
                    for index, queued in enumerate(self._items)
                    if index > 0 and queued.flags & FLAG_KEY_FRAME
                ),
                None,
            )
            if next_keyframe is None:
                self.dropped_units += len(self._items)
                self._items.clear()
                self._bytes = 0
                self.awaiting_keyframe = True
                return

            for _ in range(next_keyframe):
                self._bytes -= len(self._items.popleft().payload)
                self.dropped_units += 1

        if self._items:
            self.awaiting_keyframe = False

    def get_for_output(self) -> AccessUnit | None:
        with self._lock:
            if not self._items: return None
            self._enforce_limits_locked(time.monotonic())
            if not self._items:
                return None
            unit = self._items.popleft()
            self._bytes -= len(unit.payload)
            return unit

    def get_latest(self) -> AccessUnit | None:
        """Compatibility helper; live decoding uses FIFO ``get_for_output``."""
        with self._lock:
            if not self._items: return None
            unit = self._items[-1]
            self._items.clear(); self._bytes = 0
            return unit

    def reset_for_new_stream(self) -> None:
        with self._lock:
            self.dropped_units += len(self._items)
            self._items.clear()
            self._bytes = 0
            self.awaiting_keyframe = True

    @property
    def size(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def queued_bytes(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def oldest_age_seconds(self) -> float:
        with self._lock:
            if not self._items:
                return 0.0
            return max(0.0, time.monotonic() - self._items[0].created)


class USBContext(ctypes.Structure):
    pass


class USBDevice(ctypes.Structure):
    pass


class USBDeviceHandle(ctypes.Structure):
    pass


USBContextPtr = ctypes.POINTER(USBContext)
USBDevicePtr = ctypes.POINTER(USBDevice)
USBDeviceHandlePtr = ctypes.POINTER(USBDeviceHandle)


class USBDeviceDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_ubyte),
        ("bDescriptorType", ctypes.c_ubyte),
        ("bcdUSB", ctypes.c_ushort),
        ("bDeviceClass", ctypes.c_ubyte),
        ("bDeviceSubClass", ctypes.c_ubyte),
        ("bDeviceProtocol", ctypes.c_ubyte),
        ("bMaxPacketSize0", ctypes.c_ubyte),
        ("idVendor", ctypes.c_ushort),
        ("idProduct", ctypes.c_ushort),
        ("bcdDevice", ctypes.c_ushort),
        ("iManufacturer", ctypes.c_ubyte),
        ("iProduct", ctypes.c_ubyte),
        ("iSerialNumber", ctypes.c_ubyte),
        ("bNumConfigurations", ctypes.c_ubyte),
    ]


class USBEndpointDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_ubyte),
        ("bDescriptorType", ctypes.c_ubyte),
        ("bEndpointAddress", ctypes.c_ubyte),
        ("bmAttributes", ctypes.c_ubyte),
        ("wMaxPacketSize", ctypes.c_ushort),
        ("bInterval", ctypes.c_ubyte),
        ("bRefresh", ctypes.c_ubyte),
        ("bSynchAddress", ctypes.c_ubyte),
        ("extra", ctypes.POINTER(ctypes.c_ubyte)),
        ("extra_length", ctypes.c_int),
    ]


class USBInterfaceDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_ubyte),
        ("bDescriptorType", ctypes.c_ubyte),
        ("bInterfaceNumber", ctypes.c_ubyte),
        ("bAlternateSetting", ctypes.c_ubyte),
        ("bNumEndpoints", ctypes.c_ubyte),
        ("bInterfaceClass", ctypes.c_ubyte),
        ("bInterfaceSubClass", ctypes.c_ubyte),
        ("bInterfaceProtocol", ctypes.c_ubyte),
        ("iInterface", ctypes.c_ubyte),
        ("endpoint", ctypes.POINTER(USBEndpointDescriptor)),
        ("extra", ctypes.POINTER(ctypes.c_ubyte)),
        ("extra_length", ctypes.c_int),
    ]


class USBInterface(ctypes.Structure):
    _fields_ = [
        ("altsetting", ctypes.POINTER(USBInterfaceDescriptor)),
        ("num_altsetting", ctypes.c_int),
    ]


class USBConfigDescriptor(ctypes.Structure):
    _fields_ = [
        ("bLength", ctypes.c_ubyte),
        ("bDescriptorType", ctypes.c_ubyte),
        ("wTotalLength", ctypes.c_ushort),
        ("bNumInterfaces", ctypes.c_ubyte),
        ("bConfigurationValue", ctypes.c_ubyte),
        ("iConfiguration", ctypes.c_ubyte),
        ("bmAttributes", ctypes.c_ubyte),
        ("MaxPower", ctypes.c_ubyte),
        ("interface", ctypes.POINTER(USBInterface)),
        ("extra", ctypes.POINTER(ctypes.c_ubyte)),
        ("extra_length", ctypes.c_int),
    ]


class ReceiverError(RuntimeError):
    """A user-actionable receiver or USB failure."""


class USBError(ReceiverError):
    def __init__(self, operation: str, code: int, detail: str = "") -> None:
        self.operation = operation
        self.code = code
        self.detail = detail
        message = f"{operation} failed ({libusb_error_name(code)})"
        if detail:
            message += f": {detail}"
        super().__init__(message)


class USBDisconnected(USBError):
    pass


class NoAccessory(ReceiverError):
    """No already re-enumerated AOA data interface was found."""


class StreamStopped(ReceiverError):
    """The downstream display/output ended, so Android should stop capture."""


def libusb_error_name(code: int) -> str:
    return {
        LIBUSB_ERROR_IO: "I/O error",
        LIBUSB_ERROR_INVALID_PARAM: "invalid parameter",
        LIBUSB_ERROR_ACCESS: "access denied",
        LIBUSB_ERROR_NO_DEVICE: "device disconnected",
        LIBUSB_ERROR_NOT_FOUND: "not found",
        LIBUSB_ERROR_BUSY: "device busy",
        LIBUSB_ERROR_TIMEOUT: "timeout",
        LIBUSB_ERROR_OVERFLOW: "overflow",
        LIBUSB_ERROR_PIPE: "pipe error",
        LIBUSB_ERROR_INTERRUPTED: "interrupted",
        LIBUSB_ERROR_NO_MEM: "out of memory",
        LIBUSB_ERROR_NOT_SUPPORTED: "not supported",
        LIBUSB_ERROR_OTHER: "other error",
    }.get(code, f"libusb error {code}")


def _decode_libusb_error(lib: ctypes.CDLL, code: int) -> str:
    try:
        value = lib.libusb_strerror(code)
        if value:
            return value.decode("utf-8", "replace")
    except (AttributeError, OSError):
        pass
    return libusb_error_name(code)


@dataclass(frozen=True)
class USBIdentity:
    """A stable enough USB location for AOA re-enumeration."""

    bus: int
    address: int
    ports: tuple[int, ...]

    def display(self) -> str:
        if self.ports:
            return f"usb:{self.bus}-" + ".".join(str(port) for port in self.ports)
        return f"usb:{self.bus}:{self.address}"


@dataclass(frozen=True)
class USBDeviceInfo:
    identity: USBIdentity
    vendor_id: int
    product_id: int
    device_class: int

    def display(self) -> str:
        return (
            f"{self.identity.display()} "
            f"address={self.identity.bus}:{self.identity.address} "
            f"vid=0x{self.vendor_id:04x} pid=0x{self.product_id:04x}"
        )


@dataclass(frozen=True)
class BulkInterface:
    interface_number: int
    alternate_setting: int
    bulk_in: int
    bulk_out: int


@dataclass
class AccessoryConnection:
    handle: USBDeviceHandlePtr
    interface: BulkInterface
    claimed: bool = False


def _libusb_library_names(platform_name: str | None = None) -> list[str]:
    """Return platform-specific libusb names, preferring a colocated Windows DLL."""
    if platform_name is None:
        platform_name = os.name

    names: list[str] = []
    if platform_name == "nt":
        names.append(os.path.join(os.path.dirname(__file__), "libusb-1.0.dll"))

    discovered = ctypes.util.find_library("usb-1.0")
    if discovered:
        names.append(discovered)

    if platform_name == "nt":
        names.append("libusb-1.0.dll")
    else:
        names.extend(("libusb-1.0.so.0", "libusb-1.0.so"))
    return list(dict.fromkeys(names))


def _executable_available(path: str, platform_name: str | None = None) -> bool:
    if platform_name is None:
        platform_name = os.name
    if platform_name == "nt":
        return os.path.isfile(path)
    return os.access(path, os.X_OK)


class LibUSB:
    """Small, typed ctypes wrapper around the libusb functions we need."""

    def __init__(self) -> None:
        library_names = _libusb_library_names()
        loaded: ctypes.CDLL | None = None
        last_error: OSError | None = None
        for name in dict.fromkeys(library_names):
            try:
                loaded = ctypes.CDLL(name)
                break
            except OSError as error:
                last_error = error
        if loaded is None:
            if os.name == "nt":
                message = (
                    "Windows用のlibusb-1.0.dllが見つかりません。"
                    "receiverフォルダーに64-bit DLLを配置してください。"
                )
            else:
                message = "libusb-1.0 が見つかりません。libusbをインストールしてください."
            raise ReceiverError(
                message
            ) from last_error
        self.lib = loaded
        self._set_signatures()
        self.context = USBContextPtr()
        code = self.lib.libusb_init(ctypes.byref(self.context))
        if code != 0:
            raise USBError("libusb初期化", code, _decode_libusb_error(self.lib, code))

    def _set_signatures(self) -> None:
        lib = self.lib
        lib.libusb_init.argtypes = [ctypes.POINTER(USBContextPtr)]
        lib.libusb_init.restype = ctypes.c_int
        lib.libusb_exit.argtypes = [USBContextPtr]
        lib.libusb_exit.restype = None
        lib.libusb_get_device_list.argtypes = [
            USBContextPtr,
            ctypes.POINTER(ctypes.POINTER(USBDevicePtr)),
        ]
        lib.libusb_get_device_list.restype = ctypes.c_ssize_t
        lib.libusb_free_device_list.argtypes = [
            ctypes.POINTER(USBDevicePtr),
            ctypes.c_int,
        ]
        lib.libusb_free_device_list.restype = None
        lib.libusb_get_device_descriptor.argtypes = [
            USBDevicePtr,
            ctypes.POINTER(USBDeviceDescriptor),
        ]
        lib.libusb_get_device_descriptor.restype = ctypes.c_int
        lib.libusb_get_bus_number.argtypes = [USBDevicePtr]
        lib.libusb_get_bus_number.restype = ctypes.c_ubyte
        lib.libusb_get_device_address.argtypes = [USBDevicePtr]
        lib.libusb_get_device_address.restype = ctypes.c_ubyte
        lib.libusb_ref_device.argtypes = [USBDevicePtr]
        lib.libusb_ref_device.restype = USBDevicePtr
        lib.libusb_unref_device.argtypes = [USBDevicePtr]
        lib.libusb_unref_device.restype = None
        lib.libusb_get_port_numbers.argtypes = [
            USBDevicePtr,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int,
        ]
        lib.libusb_get_port_numbers.restype = ctypes.c_int
        lib.libusb_open.argtypes = [USBDevicePtr, ctypes.POINTER(USBDeviceHandlePtr)]
        lib.libusb_open.restype = ctypes.c_int
        lib.libusb_close.argtypes = [USBDeviceHandlePtr]
        lib.libusb_close.restype = None
        lib.libusb_control_transfer.argtypes = [
            USBDeviceHandlePtr,
            ctypes.c_ubyte,
            ctypes.c_ubyte,
            ctypes.c_ushort,
            ctypes.c_ushort,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_ushort,
            ctypes.c_uint,
        ]
        lib.libusb_control_transfer.restype = ctypes.c_int
        lib.libusb_bulk_transfer.argtypes = [
            USBDeviceHandlePtr,
            ctypes.c_ubyte,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.c_uint,
        ]
        lib.libusb_bulk_transfer.restype = ctypes.c_int
        lib.libusb_get_active_config_descriptor.argtypes = [
            USBDevicePtr,
            ctypes.POINTER(ctypes.POINTER(USBConfigDescriptor)),
        ]
        lib.libusb_get_active_config_descriptor.restype = ctypes.c_int
        lib.libusb_get_config_descriptor.argtypes = [
            USBDevicePtr,
            ctypes.c_ubyte,
            ctypes.POINTER(ctypes.POINTER(USBConfigDescriptor)),
        ]
        lib.libusb_get_config_descriptor.restype = ctypes.c_int
        lib.libusb_free_config_descriptor.argtypes = [ctypes.POINTER(USBConfigDescriptor)]
        lib.libusb_free_config_descriptor.restype = None
        lib.libusb_claim_interface.argtypes = [USBDeviceHandlePtr, ctypes.c_int]
        lib.libusb_claim_interface.restype = ctypes.c_int
        lib.libusb_release_interface.argtypes = [USBDeviceHandlePtr, ctypes.c_int]
        lib.libusb_release_interface.restype = ctypes.c_int
        lib.libusb_set_interface_alt_setting.argtypes = [
            USBDeviceHandlePtr,
            ctypes.c_int,
            ctypes.c_int,
        ]
        lib.libusb_set_interface_alt_setting.restype = ctypes.c_int
        lib.libusb_set_auto_detach_kernel_driver.argtypes = [
            USBDeviceHandlePtr,
            ctypes.c_int,
        ]
        lib.libusb_set_auto_detach_kernel_driver.restype = ctypes.c_int
        lib.libusb_strerror.argtypes = [ctypes.c_int]
        lib.libusb_strerror.restype = ctypes.c_char_p

    def __enter__(self) -> "LibUSB":
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.close()

    def close(self) -> None:
        if self.context:
            self.lib.libusb_exit(self.context)
            self.context = USBContextPtr()

    @contextmanager
    def devices(self) -> Iterator[list[USBDevicePtr]]:
        device_list = ctypes.POINTER(USBDevicePtr)()
        count = self.lib.libusb_get_device_list(
            self.context, ctypes.byref(device_list)
        )
        if count < 0:
            raise USBError("USBデバイス一覧取得", int(count), _decode_libusb_error(self.lib, int(count)))
        try:
            yield [device_list[index] for index in range(int(count))]
        finally:
            self.lib.libusb_free_device_list(device_list, 1)

    def describe(self, device: USBDevicePtr) -> USBDeviceInfo:
        descriptor = USBDeviceDescriptor()
        code = self.lib.libusb_get_device_descriptor(device, ctypes.byref(descriptor))
        self._check("USBデバイス情報取得", code)
        ports_buffer = (ctypes.c_ubyte * 8)()
        port_count = self.lib.libusb_get_port_numbers(device, ports_buffer, 8)
        ports: tuple[int, ...]
        if port_count > 0:
            ports = tuple(int(ports_buffer[index]) for index in range(port_count))
        else:
            ports = ()
        identity = USBIdentity(
            bus=int(self.lib.libusb_get_bus_number(device)),
            address=int(self.lib.libusb_get_device_address(device)),
            ports=ports,
        )
        return USBDeviceInfo(
            identity=identity,
            vendor_id=int(descriptor.idVendor),
            product_id=int(descriptor.idProduct),
            device_class=int(descriptor.bDeviceClass),
        )

    def open(self, device: USBDevicePtr) -> USBDeviceHandlePtr:
        handle = USBDeviceHandlePtr()
        code = self.lib.libusb_open(device, ctypes.byref(handle))
        if code != 0:
            detail = _decode_libusb_error(self.lib, code)
            if os.name == "nt" and code in (
                LIBUSB_ERROR_ACCESS,
                LIBUSB_ERROR_NOT_SUPPORTED,
            ):
                detail += "; Zadigで対象USB interfaceにWinUSBを設定してください"
            raise USBError("USBデバイスを開く", code, detail)
        return handle

    def close_handle(self, handle: USBDeviceHandlePtr | None) -> None:
        if handle:
            self.lib.libusb_close(handle)

    def release_device_ref(self, device: USBDevicePtr) -> None:
        self.lib.libusb_unref_device(device)

    def _check(self, operation: str, code: int) -> None:
        if code < 0:
            raise USBError(operation, code, _decode_libusb_error(self.lib, code))

    def get_protocol(self, handle: USBDeviceHandlePtr) -> int:
        buffer = (ctypes.c_ubyte * 2)()
        code = self.lib.libusb_control_transfer(
            handle,
            AOA_REQUEST_IN,
            AOA_GET_PROTOCOL,
            0,
            0,
            buffer,
            2,
            DEFAULT_TIMEOUT_MS,
        )
        if code < 0:
            raise USBError("AOA GET_PROTOCOL", code, _decode_libusb_error(self.lib, code))
        if code != 2:
            raise ReceiverError(f"AOA GET_PROTOCOLの応答長が不正です ({code})")
        return int.from_bytes(bytes(buffer), "little")

    def send_identifier(self, handle: USBDeviceHandlePtr, index: int, value: str) -> None:
        encoded = value.encode("utf-8")
        if len(encoded) > AOA_STRING_MAX:
            raise ReceiverError(f"AOA identifier {index} が長すぎます")
        payload = encoded + b"\0"
        buffer = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
        code = self.lib.libusb_control_transfer(
            handle,
            AOA_REQUEST_OUT,
            AOA_SEND_STRING,
            0,
            index,
            buffer,
            len(payload),
            DEFAULT_TIMEOUT_MS,
        )
        if code < 0:
            raise USBError(
                f"AOA SEND_STRING[{index}]", code, _decode_libusb_error(self.lib, code)
            )
        if code not in (0, len(payload)):
            raise ReceiverError(f"AOA SEND_STRING[{index}]の応答長が不正です ({code})")

    def start_accessory(self, handle: USBDeviceHandlePtr) -> None:
        code = self.lib.libusb_control_transfer(
            handle,
            AOA_REQUEST_OUT,
            AOA_START,
            0,
            0,
            None,
            0,
            DEFAULT_TIMEOUT_MS,
        )
        if code < 0:
            raise USBError("AOA START", code, _decode_libusb_error(self.lib, code))

    def find_bulk_interface(self, device: USBDevicePtr) -> BulkInterface | None:
        config = ctypes.POINTER(USBConfigDescriptor)()
        code = self.lib.libusb_get_active_config_descriptor(device, ctypes.byref(config))
        if code != 0:
            code = self.lib.libusb_get_config_descriptor(device, 0, ctypes.byref(config))
        if code != 0 or not config:
            return None
        try:
            config_value = config.contents
            for interface_index in range(int(config_value.bNumInterfaces)):
                interface = config_value.interface[interface_index]
                for alt_index in range(int(interface.num_altsetting)):
                    alt = interface.altsetting[alt_index]
                    if (
                        int(alt.bInterfaceClass) != LIBUSB_CLASS_VENDOR_SPEC
                        or int(alt.bInterfaceSubClass) != LIBUSB_SUBCLASS_VENDOR_SPEC
                        or int(alt.bInterfaceProtocol) != LIBUSB_PROTOCOL_VENDOR_SPEC
                    ):
                        continue
                    bulk_in_candidates: list[int] = []
                    bulk_out_candidates: list[int] = []
                    for endpoint_index in range(int(alt.bNumEndpoints)):
                        endpoint = alt.endpoint[endpoint_index]
                        if (
                            int(endpoint.bmAttributes) & LIBUSB_TRANSFER_TYPE_MASK
                            != LIBUSB_TRANSFER_TYPE_BULK
                        ):
                            continue
                        address = int(endpoint.bEndpointAddress)
                        if address & LIBUSB_ENDPOINT_IN:
                            bulk_in_candidates.append(address)
                        else:
                            bulk_out_candidates.append(address)
                    if len(bulk_in_candidates) == 1 and len(bulk_out_candidates) == 1:
                        return BulkInterface(
                            interface_number=int(alt.bInterfaceNumber),
                            alternate_setting=int(alt.bAlternateSetting),
                            bulk_in=bulk_in_candidates[0],
                            bulk_out=bulk_out_candidates[0],
                        )
        finally:
            self.lib.libusb_free_config_descriptor(config)
        return None

    def claim_accessory(
        self, handle: USBDeviceHandlePtr, interface: BulkInterface
    ) -> AccessoryConnection:
        # AOA's vendor-specific interface normally has no kernel driver.  Let
        # libusb detach/re-attach one when a distribution happens to bind it.
        auto_detach = self.lib.libusb_set_auto_detach_kernel_driver(handle, 1)
        if auto_detach not in (0, LIBUSB_ERROR_NOT_SUPPORTED):
            raise USBError(
                "USB kernel driver設定", auto_detach, _decode_libusb_error(self.lib, auto_detach)
            )
        code = self.lib.libusb_claim_interface(handle, interface.interface_number)
        if code != 0:
            raise USBError("AOA interface確保", code, _decode_libusb_error(self.lib, code))
        if interface.alternate_setting:
            code = self.lib.libusb_set_interface_alt_setting(
                handle, interface.interface_number, interface.alternate_setting
            )
            if code != 0:
                self.lib.libusb_release_interface(handle, interface.interface_number)
                raise USBError(
                    "USB alternate setting選択",
                    code,
                    _decode_libusb_error(self.lib, code),
                )
        return AccessoryConnection(handle=handle, interface=interface, claimed=True)

    def release_accessory(self, connection: AccessoryConnection) -> None:
        if connection.claimed:
            self.lib.libusb_release_interface(
                connection.handle, connection.interface.interface_number
            )
            connection.claimed = False
        self.close_handle(connection.handle)

    def bulk_out(
        self,
        connection: AccessoryConnection,
        data: bytes,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        retry_timeout_seconds: float = 0.0,
    ) -> None:
        if not data:
            return
        offset = 0
        deadline = (
            time.monotonic() + retry_timeout_seconds
            if retry_timeout_seconds > 0
            else None
        )
        while offset < len(data):
            chunk = data[offset:]
            buffer = (ctypes.c_ubyte * len(chunk)).from_buffer_copy(chunk)
            transferred = ctypes.c_int()
            transfer_timeout_ms = timeout_ms
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise USBError(
                        "AOA bulk OUT", LIBUSB_ERROR_TIMEOUT, "送信待ち時間を超えました"
                    )
                transfer_timeout_ms = min(
                    timeout_ms, max(1, int(remaining * 1_000))
                )
            code = self.lib.libusb_bulk_transfer(
                connection.handle,
                connection.interface.bulk_out,
                buffer,
                len(chunk),
                ctypes.byref(transferred),
                transfer_timeout_ms,
            )
            moved = transferred.value
            if moved < 0 or moved > len(chunk):
                raise USBError("AOA bulk OUT", LIBUSB_ERROR_OVERFLOW, "転送長が不正です")
            if code == LIBUSB_ERROR_NO_DEVICE:
                raise USBDisconnected(
                    "AOA bulk OUT", code, _decode_libusb_error(self.lib, code)
                )
            if code == LIBUSB_ERROR_TIMEOUT:
                # libusb may report progress together with a timeout.  Keep
                # the acknowledged prefix out of a retry to avoid duplicates;
                # retry the unsent suffix only.
                if moved > 0:
                    offset += moved
                    continue
                if deadline is not None and time.monotonic() < deadline:
                    continue
                raise USBError("AOA bulk OUT", code, _decode_libusb_error(self.lib, code))
            if code != 0:
                raise USBError("AOA bulk OUT", code, _decode_libusb_error(self.lib, code))
            if moved <= 0:
                raise USBError("AOA bulk OUT", LIBUSB_ERROR_IO, "転送長が0です")
            offset += moved

    def bulk_in(
        self,
        connection: AccessoryConnection,
        size: int = READ_SIZE,
        timeout_ms: int = BULK_TIMEOUT_MS,
    ) -> bytes:
        buffer = (ctypes.c_ubyte * size)()
        transferred = ctypes.c_int()
        code = self.lib.libusb_bulk_transfer(
            connection.handle,
            connection.interface.bulk_in,
            buffer,
            size,
            ctypes.byref(transferred),
            timeout_ms,
        )
        moved = transferred.value
        if moved < 0 or moved > size:
            raise USBError("AOA bulk IN", LIBUSB_ERROR_OVERFLOW, "転送長が不正です")
        if code == LIBUSB_ERROR_TIMEOUT:
            # A synchronous bulk transfer can time out after receiving one or
            # more USB packets.  Return those bytes so FrameParser can retain
            # the partial GUSB frame; only an empty timeout is an idle poll.
            return bytes(buffer[:moved])
        if code == LIBUSB_ERROR_NO_DEVICE:
            raise USBDisconnected(
                "AOA bulk IN", code, _decode_libusb_error(self.lib, code)
            )
        if code != 0:
            raise USBError("AOA bulk IN", code, _decode_libusb_error(self.lib, code))
        return bytes(buffer[:moved])

    def is_accessory(self, info: USBDeviceInfo) -> bool:
        return (
            info.vendor_id == AOA_VENDOR_ID
            and info.product_id in AOA_DATA_PRODUCT_IDS
        )

    def accessory_candidates(
        self, selector: USBIdentity | None = None
    ) -> list[tuple[USBDevicePtr, USBDeviceInfo, BulkInterface]]:
        candidates: list[tuple[USBDevicePtr, USBDeviceInfo, BulkInterface]] = []
        with self.devices() as devices:
            for device in devices:
                try:
                    info = self.describe(device)
                except USBError:
                    continue
                if not self.is_accessory(info):
                    continue
                if selector is not None and not identity_matches(selector, info.identity):
                    continue
                interface = self.find_bulk_interface(device)
                if interface is not None:
                    # free_device_list(..., unref_devices=1) releases the
                    # enumeration's reference.  Keep one while callers open
                    # this device after the context manager returns.
                    referenced = self.lib.libusb_ref_device(device)
                    if referenced:
                        candidates.append((referenced, info, interface))
        return candidates

    def release_accessory_candidates(
        self, candidates: Sequence[tuple[USBDevicePtr, USBDeviceInfo, BulkInterface]]
    ) -> None:
        for device, _info, _interface in candidates:
            self.release_device_ref(device)


def identity_matches(expected: USBIdentity, actual: USBIdentity) -> bool:
    if expected.bus != actual.bus:
        return False
    if expected.ports and actual.ports:
        return expected.ports == actual.ports
    # Some libusb backends cannot provide port numbers.  In that case the bus
    # is the only stable identity available; callers require a unique match.
    return expected.address == actual.address


def parse_device_selector(value: str) -> USBIdentity:
    try:
        bus_text, address_text = value.split(":", 1)
        bus = int(bus_text, 10)
        address = int(address_text, 10)
    except (ValueError, AttributeError):
        raise argparse.ArgumentTypeError("BUS:ADDRESS 形式で指定してください") from None
    if not (0 <= bus <= 255 and 0 <= address <= 255):
        raise argparse.ArgumentTypeError("BUS/ADDRESS は0〜255で指定してください")
    return USBIdentity(bus=bus, address=address, ports=())


def parse_wait(value: str) -> float:
    try:
        wait = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("秒数を指定してください") from None
    if not math.isfinite(wait) or wait <= 0 or wait > 300:
        raise argparse.ArgumentTypeError("待ち時間は0より大きく300以下で指定してください")
    return wait


def usb_selector_matches(selector: USBIdentity, info: USBDeviceInfo) -> bool:
    # --device uses the current bus/address.  Port matching is used only after
    # AOA START, where the USB address is expected to change.
    return selector.bus == info.identity.bus and selector.address == info.identity.address


def select_accessory_connection(
    usb: LibUSB,
    selector: USBIdentity | None,
) -> tuple[AccessoryConnection, USBDeviceInfo]:
    all_candidates = usb.accessory_candidates()
    try:
        candidates = all_candidates
        if selector is not None:
            candidates = [
                item for item in candidates if usb_selector_matches(selector, item[1])
            ]
        if len(candidates) > 1:
            locations = ", ".join(info.identity.display() for _, info, _ in candidates)
            raise ReceiverError(
                "AOA端末が複数あります。--device BUS:ADDRESS を指定してください "
                f"({locations})"
            )
        if not candidates:
            raise NoAccessory("AOA data interfaceが見つかりません")
        device, info, interface = candidates[0]
        handle = usb.open(device)
        try:
            connection = usb.claim_accessory(handle, interface)
        except Exception:
            usb.close_handle(handle)
            raise
        return connection, info
    finally:
        usb.release_accessory_candidates(all_candidates)


def start_aoa_and_wait(
    usb: LibUSB,
    selector: USBIdentity | None,
    wait_seconds: float,
) -> tuple[AccessoryConnection, USBDeviceInfo]:
    """Perform AOA negotiation and wait for the accessory re-enumeration."""

    protocol_candidates: list[tuple[USBDevicePtr, USBDeviceInfo, int]] = []
    inaccessible = 0
    explicit_failure: USBError | None = None
    with usb.devices() as devices:
        for device in devices:
            try:
                info = usb.describe(device)
            except ReceiverError:
                continue
            if info.device_class == 9:  # USB hub; never an Android AOA target.
                continue
            if usb.is_accessory(info):
                continue
            if selector is not None and not usb_selector_matches(selector, info):
                continue
            try:
                handle = usb.open(device)
            except USBError as error:
                inaccessible += 1
                if selector is not None:
                    explicit_failure = error
                continue
            try:
                protocol = usb.get_protocol(handle)
                if protocol >= 1:
                    protocol_candidates.append((device, info, protocol))
            except ReceiverError as error:
                if selector is not None:
                    explicit_failure = error
            finally:
                usb.close_handle(handle)

        if not protocol_candidates:
            if explicit_failure is not None:
                raise ReceiverError(
                    f"指定USB端末でAOAを開始できません: {explicit_failure}"
                ) from explicit_failure
            if inaccessible:
                raise ReceiverError("USB端末へアクセスできません。udev権限を確認してください")
            raise ReceiverError("AOA対応のAndroid端末が見つかりません")
        if len(protocol_candidates) > 1:
            locations = ", ".join(info.identity.display() for _, info, _ in protocol_candidates)
            raise ReceiverError(
                f"AOA対応端末が複数あります。--device BUS:ADDRESS を指定してください ({locations})"
            )

        device, info, _protocol = protocol_candidates[0]
        handle = usb.open(device)
        try:
            # The values are constants, never user credentials.  Android's
            # manufacturer/model filter and the OS USB permission gate access.
            for index, identifier in enumerate(AOA_IDENTIFIER_STRINGS):
                usb.send_identifier(handle, index, identifier)
            usb.start_accessory(handle)
        except Exception:
            usb.close_handle(handle)
            raise
        usb.close_handle(handle)
        target = info.identity

    deadline = time.monotonic() + wait_seconds
    last_seen_audio = False
    while time.monotonic() < deadline:
        candidates = usb.accessory_candidates()
        try:
            matching = [
                item
                for item in candidates
                if identity_matches(target, item[1].identity)
            ]
            # If port information is unavailable, only accept a unique
            # accessory on the original bus.  This prevents a different
            # phone from winning.
            if not matching and not target.ports:
                same_bus = [
                    item for item in candidates if item[1].identity.bus == target.bus
                ]
                if len(same_bus) == 1:
                    matching = same_bus
            if matching:
                connection, accessory_info = _claim_candidate(usb, matching[0])
                return connection, accessory_info
        finally:
            usb.release_accessory_candidates(candidates)

        with usb.devices() as devices:
            for device in devices:
                try:
                    info = usb.describe(device)
                except USBError:
                    continue
                if (
                    info.vendor_id == AOA_VENDOR_ID
                    and info.product_id in AOA_AUDIO_ONLY_PRODUCT_IDS
                    and info.identity.bus == target.bus
                ):
                    last_seen_audio = True
        time.sleep(POLL_INTERVAL_SECONDS)

    if last_seen_audio:
        raise ReceiverError("AOA audio-only PIDを検出しました。映像用data interfaceではありません")
    raise ReceiverError("AOA START後のdata interface再列挙がタイムアウトしました")


def _claim_candidate(
    usb: LibUSB,
    candidate: tuple[USBDevicePtr, USBDeviceInfo, BulkInterface],
) -> tuple[AccessoryConnection, USBDeviceInfo]:
    device, info, interface = candidate
    handle = usb.open(device)
    try:
        connection = usb.claim_accessory(handle, interface)
    except Exception:
        usb.close_handle(handle)
        raise
    return connection, info


class FfplaySink:
    def __init__(self, executable: str, title: str = "Android USB Display") -> None:
        self.executable = executable
        self.title = title
        self.process: subprocess.Popen[bytes] | None = None

    def start(self, fps: float | None = None) -> None:
        command = [
            self.executable,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "h264",
        ]
        if fps is not None:
            # Raw H.264 needs a nominal rate to configure its demuxer. GUSB/1
            # has no timestamps, so this does not represent actual cadence.
            command.extend(("-framerate", f"{fps:g}"))
        # Timestamp demuxed AUs at ingress and sync to real time so -framedrop
        # can shed stale frames when decode or presentation falls behind.
        command.extend(
            (
                "-use_wallclock_as_timestamps",
                "1",
                "-probesize",
                "32",
                "-analyzeduration",
                "0",
                "-fflags",
                "nobuffer",
                "-flags",
                "low_delay",
                "-framedrop",
                "-sync",
                "ext",
                "-window_title",
                self.title,
                "-i",
                "pipe:0",
            )
        )
        try:
            self.process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=None,
            )
        except OSError as error:
            raise ReceiverError(f"ffplayを起動できません: {error}") from error

    def write(self, payload: bytes) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise StreamStopped("ffplayが開始されていません")
        if process.poll() is not None:
            raise StreamStopped("ffplayが終了しました")
        try:
            process.stdin.write(payload)
            process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as error:
            raise StreamStopped("ffplayへの出力が切断されました") from error

    def pipe_pending_bytes(self) -> int | None:
        """Return unread bytes queued for ffplay, when the platform exposes it."""
        process = self.process
        if process is None or process.stdin is None or fcntl is None or termios is None:
            return None
        pending = array.array("i", [0])
        try:
            fcntl.ioctl(process.stdin.fileno(), termios.FIONREAD, pending, True)
        except (AttributeError, OSError, ValueError):
            return None
        return int(pending[0])

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)


@dataclass(frozen=True)
class StreamInfo:
    width: int
    height: int
    fps: float
    codec: str


def parse_stream_info(payload: bytes) -> StreamInfo:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FrameError("INFO JSONが不正です") from error
    if not isinstance(value, dict):
        raise FrameError("INFO JSONはオブジェクトである必要があります")
    width = value.get("width")
    height = value.get("height")
    fps_value = value.get("fps")
    codec = value.get("codec")
    if isinstance(width, bool) or not isinstance(width, int) or not 1 <= width <= 8_192:
        raise FrameError("INFO widthが不正です")
    if isinstance(height, bool) or not isinstance(height, int) or not 1 <= height <= 8_192:
        raise FrameError("INFO heightが不正です")
    if not isinstance(codec, str) or codec.lower() != "h264":
        raise FrameError("INFO codecはh264である必要があります")
    if isinstance(fps_value, bool):
        raise FrameError("INFO fpsが不正です")
    try:
        if isinstance(fps_value, str) and "/" in fps_value:
            numerator, denominator = fps_value.split("/", 1)
            fps = float(numerator) / float(denominator)
        else:
            fps = float(fps_value)
    except (TypeError, ValueError, ZeroDivisionError):
        raise FrameError("INFO fpsが不正です") from None
    if not math.isfinite(fps) or not 0 < fps <= 240:
        raise FrameError("INFO fpsが不正です")
    return StreamInfo(width=width, height=height, fps=fps, codec="h264")


class StreamSession:
    def __init__(
        self,
        usb: LibUSB,
        connection: AccessoryConnection,
        *,
        raw_stdout: bool,
        ffplay_path: str | None,
        hello_timeout_seconds: float = HELLO_TIMEOUT_SECONDS,
        ffplay_window_title: str = "Mirror (MediaProjection) - Galaxy USB",
    ) -> None:
        self.usb = usb
        self.connection = connection
        self.raw_stdout = raw_stdout
        self.ffplay_path = ffplay_path
        self.hello_timeout_seconds = hello_timeout_seconds
        self.ffplay_window_title = ffplay_window_title
        self.parser = FrameParser()
        self.info: StreamInfo | None = None
        self.sink: FfplaySink | None = None
        self._stop_sent = False
        self._remote_stop = False
        self._video_queue = LatestFrameQueue()
        self._au_parts = bytearray()
        self._au_flags = 0
        self._config = b""
        self._in_run = False
        self._reader_stop = threading.Event()
        self._reader_thread: threading.Thread | None = None
        self._wake = threading.Event()
        self._reader_error: BaseException | None = None
        self._stats_lock = threading.Lock()
        self._stats_started = time.monotonic()
        self._stats_input_aus = 0
        self._stats_input_bytes = 0
        self._stats_output_aus = 0
        self._stats_output_bytes = 0
        self._stats_write_seconds = 0.0
        self._stats_last_dropped = 0

    def send(self, frame_type: int, payload: bytes = b"") -> None:
        retry_timeout_seconds = (
            self.hello_timeout_seconds if frame_type == TYPE_HELLO else 0.0
        )
        self.usb.bulk_out(
            self.connection,
            encode_frame(frame_type, payload),
            timeout_ms=DEFAULT_TIMEOUT_MS,
            retry_timeout_seconds=retry_timeout_seconds,
        )

    def send_stop_best_effort(self) -> None:
        if self._stop_sent or self._remote_stop:
            return
        self._stop_sent = True
        try:
            self.send(TYPE_STOP)
        except (ReceiverError, OSError):
            pass

    def send_error_best_effort(self, message: str) -> None:
        # Keep protocol diagnostics bounded and free of arbitrary binary data.
        safe = " ".join(message.replace("\x00", " ").split())[:240]
        try:
            self.send(TYPE_ERROR, json.dumps({"error": safe}, separators=(",", ":")).encode())
        except (ReceiverError, OSError):
            pass

    def run(self) -> None:
        self.send(TYPE_HELLO, HELLO_PAYLOAD)
        events: queue.Queue[tuple[Frame, threading.Event | None]] = queue.Queue(maxsize=16)

        self._reader_stop.clear()
        self._wake.clear()
        self._reader_error = None

        def publish_event(event: Frame, wait_for_writer: bool = False) -> threading.Event | None:
            handled = threading.Event() if wait_for_writer else None
            try:
                events.put_nowait((event, handled))
            except queue.Full as error:
                raise FrameError("control frame queueが上限を超えました") from error
            self._wake.set()
            return handled

        def reader() -> None:
            parts = bytearray()
            flags = 0
            config = bytearray()
            try:
                while not self._reader_stop.is_set():
                    chunk = self.usb.bulk_in(self.connection)
                    if chunk:
                        for frame in self.parser.feed(chunk):
                            if frame.type == TYPE_VIDEO:
                                parts.extend(frame.payload)
                                flags |= frame.flags
                                if flags & FLAG_CODEC_CONFIG and frame.flags & FLAG_END_OF_ACCESS_UNIT:
                                    if len(config) + len(parts) > MAX_CODEC_CONFIG_BYTES:
                                        raise FrameError("codec configが大きすぎます")
                                    config.extend(parts)
                                if len(parts) > MAX_ACCESS_UNIT_BYTES:
                                    raise FrameError("VIDEO access unitが大きすぎます")
                                if frame.flags & FLAG_END_OF_ACCESS_UNIT:
                                    payload = bytes(parts)
                                    au_flags = flags
                                    parts.clear()
                                    flags = 0
                                    if au_flags & FLAG_KEY_FRAME and config and not (au_flags & FLAG_CODEC_CONFIG):
                                        payload = bytes(config) + payload
                                    unit = AccessUnit(payload, au_flags, time.monotonic())
                                    self._video_queue.put(unit)
                                    if not au_flags & FLAG_CODEC_CONFIG:
                                        self._record_input(unit)
                                    self._wake.set()
                            elif frame.type == TYPE_INFO:
                                # Rotation announces a new encoder sequence;
                                # reset queued video and decoder config before
                                # the new size's following keyframe can arrive.
                                parts.clear()
                                flags = 0
                                config.clear()
                                self._video_queue.reset_for_new_stream()
                                # Keep sink changes on the writer and block the
                                # next AU until this INFO has been applied.
                                handled = publish_event(frame, wait_for_writer=True)
                                assert handled is not None
                                while not handled.wait(0.1):
                                    if self._reader_stop.is_set():
                                        return
                            else:
                                # Other control traffic wakes the writer;
                                # terminal frames also end the reader.
                                publish_event(frame)
                                if frame.type in (TYPE_STOP, TYPE_ERROR):
                                    return
            except BaseException as error:
                self._reader_error = error
                self._wake.set()

        thread = threading.Thread(target=reader, name="gusb-bulk-reader", daemon=True)
        self._reader_thread = thread
        self._in_run = True
        try:
            thread.start()
            while True:
                self._wake.wait()
                self._wake.clear()

                if self._reader_error is not None:
                    raise self._reader_error

                stopped = False
                while True:
                    try:
                        event, handled = events.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        if self.handle(event):
                            stopped = True
                            break
                    except BaseException:
                        self._reader_stop.set()
                        raise
                    finally:
                        if handled is not None:
                            handled.set()

                # A control barrier is applied before its subsequent video:
                # the reader waits for the writer to process each INFO frame.
                while True:
                    unit = self._video_queue.get_for_output()
                    if unit is None:
                        break
                    self._write_video(unit.payload)

                if stopped: return
        except FrameError as error:
            self.send_error_best_effort(str(error))
            raise
        finally:
            self._in_run = False
            self._reader_stop.set()
            if thread.is_alive(): thread.join(timeout=1.0)

    def _write_video(self, payload: bytes) -> None:
        started = time.monotonic()
        try:
            if self.raw_stdout:
                sys.stdout.buffer.write(payload); sys.stdout.buffer.flush()
            elif self.sink is not None:
                self.sink.write(payload)
        except (BrokenPipeError, OSError) as error:
            self.send_stop_best_effort()
            raise StreamStopped("H264 stdoutが切断されました") from error
        except StreamStopped:
            self.send_stop_best_effort()
            raise
        finally:
            self._record_output(len(payload), time.monotonic() - started)

    def _record_input(self, unit: AccessUnit) -> None:
        with self._stats_lock:
            self._stats_input_aus += 1
            self._stats_input_bytes += len(unit.payload)
        self._maybe_log_receiver_stats()

    def _record_output(self, size: int, duration: float) -> None:
        with self._stats_lock:
            self._stats_output_aus += 1
            self._stats_output_bytes += size
            self._stats_write_seconds += duration
        self._maybe_log_receiver_stats()

    def _maybe_log_receiver_stats(self) -> None:
        now = time.monotonic()
        with self._stats_lock:
            elapsed = now - self._stats_started
            if elapsed < 1.0:
                return
            input_aus = self._stats_input_aus
            input_bytes = self._stats_input_bytes
            output_aus = self._stats_output_aus
            output_bytes = self._stats_output_bytes
            write_seconds = self._stats_write_seconds
            self._stats_input_aus = 0
            self._stats_input_bytes = 0
            self._stats_output_aus = 0
            self._stats_output_bytes = 0
            self._stats_write_seconds = 0.0
            self._stats_started = now

        dropped_total = self._video_queue.dropped_units
        dropped = dropped_total - self._stats_last_dropped
        self._stats_last_dropped = dropped_total
        pipe_pending = self.sink.pipe_pending_bytes() if self.sink is not None else None
        pipe_text = "n/a" if pipe_pending is None else str(pipe_pending)
        print(
            "receiverStats "
            f"inputFps={input_aus / elapsed:.1f} "
            f"inputMbps={input_bytes * 8 / elapsed / 1_000_000:.2f} "
            f"outputFps={output_aus / elapsed:.1f} "
            f"outputMbps={output_bytes * 8 / elapsed / 1_000_000:.2f} "
            f"queuedAUs={self._video_queue.size} "
            f"queuedBytes={self._video_queue.queued_bytes} "
            f"oldestMs={self._video_queue.oldest_age_seconds * 1000:.1f} "
            f"droppedAUs={dropped} ffplayPipeBytes={pipe_text} "
            f"meanWriteMs={write_seconds * 1000 / max(1, output_aus):.2f}",
            file=sys.stderr,
        )

    def handle(self, frame: Frame) -> bool:
        if frame.type == TYPE_INFO:
            stream_info = parse_stream_info(frame.payload)
            # Rotation and density changes recreate the encoder and announce a
            # new size.  H.264 carries SPS/PPS in the following VIDEO bytes;
            # ffplay can continue decoding the same pipe across that change.
            self.info = stream_info
            self._au_parts.clear()
            self._au_flags = 0
            self._config = b""
            if not self.raw_stdout and self.sink is None:
                if not self.ffplay_path:
                    raise ReceiverError("ffplayが見つかりません。--stdoutを使用してください")
                self.sink = FfplaySink(self.ffplay_path, title=self.ffplay_window_title)
                self.sink.start(stream_info.fps)
            print(
                f"INFO {stream_info.width}x{stream_info.height} {stream_info.fps:g}fps h264",
                file=sys.stderr,
            )
            return False
        if frame.type == TYPE_VIDEO:
            if self.info is None:
                raise FrameError("INFOより前にVIDEOを受信しました")
            self._au_parts.extend(frame.payload)
            self._au_flags |= frame.flags
            if self._au_flags & FLAG_CODEC_CONFIG:
                # csd-0 and csd-1 are sent as separate config AUs; retain
                # both in wire order for recovery after a queue drop.
                if frame.flags & FLAG_END_OF_ACCESS_UNIT:
                    self._config += bytes(self._au_parts)
            if len(self._au_parts) > MAX_ACCESS_UNIT_BYTES:
                self._au_parts.clear(); self._au_flags = 0
                raise FrameError("VIDEO access unitが大きすぎます")
            # Android marks the final bulk frame of every complete AU.
            if frame.flags & FLAG_END_OF_ACCESS_UNIT:
                payload = bytes(self._au_parts)
                flags = self._au_flags
                self._au_parts.clear(); self._au_flags = 0
                if flags & FLAG_KEY_FRAME and self._config and not (flags & FLAG_CODEC_CONFIG):
                    payload = self._config + payload
                self._video_queue.put(AccessUnit(payload, flags, time.monotonic()))
            elif not self._in_run:
                # Preserve the direct handle() API; live USB input always uses
                # the complete-access-unit reader above.
                self._write_video(frame.payload)
            return False
        if frame.type == TYPE_STOP:
            if self._au_parts:
                payload = bytes(self._au_parts)
                self._au_parts.clear(); self._au_flags = 0
                self._write_video(payload)
            self._remote_stop = True
            return True
        if frame.type == TYPE_ERROR:
            message = frame.payload.decode("utf-8", "replace")
            message = " ".join(message.replace("\x00", " ").split())[:240]
            raise ReceiverError(f"Android側エラー: {message}")
        if frame.type == TYPE_HELLO:
            raise FrameError("AndroidからHELLOを受信しました")
        raise FrameError("未対応のframe typeです")

    def close(self, send_stop: bool = True) -> None:
        if send_stop:
            self.send_stop_best_effort()
        self._reader_stop.set()
        if self._reader_thread is not None and self._reader_thread.is_alive():
            self._reader_thread.join(timeout=1.0)
        self._reader_thread = None
        if self.sink is not None:
            self.sink.close()
            self.sink = None


def list_accessories(usb: LibUSB, selector: USBIdentity | None = None) -> int:
    """List both initial AOA responders and already-accessory data devices.

    GET_PROTOCOL is a harmless probe used for selection.  This command never
    sends identifier strings or AOA START, so unrelated USB devices cannot be
    switched into accessory mode by ``--list``.
    """

    found = False
    accessories = usb.accessory_candidates(selector)
    try:
        for _device, info, interface in accessories:
            found = True
            print(
                f"mode=accessory {info.display()} interface={interface.interface_number} "
                f"bulk-in=0x{interface.bulk_in:02x} bulk-out=0x{interface.bulk_out:02x}"
            )
    finally:
        usb.release_accessory_candidates(accessories)

    with usb.devices() as devices:
        for device in devices:
            try:
                info = usb.describe(device)
            except ReceiverError:
                continue
            if info.device_class == 9 or usb.is_accessory(info):
                continue
            if selector is not None and not usb_selector_matches(selector, info):
                continue
            try:
                handle = usb.open(device)
            except USBError:
                continue
            try:
                protocol = usb.get_protocol(handle)
            except ReceiverError:
                continue
            finally:
                usb.close_handle(handle)
            if protocol >= 1:
                found = True
                print(f"mode=normal {info.display()} aoa_protocol={protocol}")

    if found:
        return 0
    print(
        "AOA候補が見つかりません。Androidアプリを起動してUSBを接続し、"
        "必要なら--device BUS:ADDRESSを指定してください",
        file=sys.stderr,
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gusb-receiver",
        description="Android MediaProjectionをUSB AOA/GUSBで受信して表示します（view-only）。",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="通常USBのAOA候補と再列挙済みdata interfaceを一覧表示して終了",
    )
    parser.add_argument(
        "--device",
        type=parse_device_selector,
        metavar="BUS:ADDRESS",
        help="初期USB端末を明示選択（例: 1:7）",
    )
    parser.add_argument(
        "--wait",
        type=parse_wait,
        default=15.0,
        metavar="SECONDS",
        help="AOA START後の再列挙待ち時間（既定15）",
    )
    parser.add_argument(
        "--hello-timeout",
        type=parse_wait,
        default=HELLO_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help="HELLO送信を再試行する総時間（既定120。画面共有同意待ち用）",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="ffplayを起動せず、VIDEO payloadのAnnex-B H264だけをstdoutへ出力",
    )
    parser.add_argument(
        "--ffplay",
        default="ffplay",
        metavar="PATH",
        help="表示に使うffplay実行ファイル（既定: PATHのffplay）",
    )
    parser.add_argument(
        "--window-title",
        default="Mirror (MediaProjection) - Galaxy USB",
        metavar="TITLE",
        help="ffplay表示ウィンドウのタイトル（既定: Mirror (MediaProjection) - Galaxy USB）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ffplay_path: str | None = None
    if not args.list and not args.stdout:
        ffplay_path = shutil.which(args.ffplay) if os.path.basename(args.ffplay) == args.ffplay else args.ffplay
        ffplay_available = bool(ffplay_path) and _executable_available(ffplay_path)
        if not ffplay_available:
            print("gusb-receiver: ffplayが見つかりません。--stdoutを使用してください", file=sys.stderr)
            return 2

    try:
        with LibUSB() as usb:
            if args.list:
                return list_accessories(usb, args.device)
            try:
                connection, info = select_accessory_connection(usb, args.device)
            except NoAccessory:
                # If the phone is still in normal USB mode, negotiate AOA.  An
                # already-accessory device uses the direct path above.
                if args.device is not None:
                    normal_selector = args.device
                else:
                    normal_selector = None
                connection, info = start_aoa_and_wait(usb, normal_selector, args.wait)
            session = StreamSession(
                usb,
                connection,
                raw_stdout=args.stdout,
                ffplay_path=ffplay_path,
                hello_timeout_seconds=args.hello_timeout,
                ffplay_window_title=args.window_title,
            )
            try:
                print(f"接続: {info.display()}（view-only）", file=sys.stderr)
                session.run()
            except KeyboardInterrupt:
                session.send_stop_best_effort()
                print("停止しました", file=sys.stderr)
            except StreamStopped as error:
                session.send_stop_best_effort()
                print(f"gusb-receiver: {error}", file=sys.stderr)
                return 0
            except USBDisconnected:
                print("Android USB接続が切断されました", file=sys.stderr)
                return 0
            except ReceiverError as error:
                session.send_stop_best_effort()
                print(f"gusb-receiver: {error}", file=sys.stderr)
                return 1
            finally:
                session.close(send_stop=True)
                usb.release_accessory(connection)
    except KeyboardInterrupt:
        print("停止しました", file=sys.stderr)
        return 0
    except ReceiverError as error:
        print(f"gusb-receiver: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
