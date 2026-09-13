"""The small wire protocol shared by the USB accessory receiver and app.

GUSB/1 is deliberately a byte stream protocol.  Android accessory bulk
transfers are allowed to split a frame at any USB packet boundary, so the
receiver must feed arbitrary chunks to :class:`FrameParser`.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


MAGIC = b"GUSB"
VERSION = 1
HEADER = struct.Struct(">4sBBHI")
HEADER_SIZE = HEADER.size
MAX_PAYLOAD = 16_000

TYPE_HELLO = 1
TYPE_VIDEO = 2
TYPE_INFO = 3
TYPE_STOP = 4
TYPE_ERROR = 5
FRAME_TYPES = frozenset(
    (TYPE_HELLO, TYPE_VIDEO, TYPE_INFO, TYPE_STOP, TYPE_ERROR)
)

# Android Open Accessory identifiers.  Keep these values in one place: the
# Android app filters on manufacturer/model before accepting an accessory.
AOA_MANUFACTURER = "Omarchy"
AOA_MODEL = "Galaxy USB MediaProjection"
AOA_DESCRIPTION = "USB screen projection"
AOA_VERSION = "1"
AOA_URI = "https://github.com/yu1sh/linux-galaxy-dex"
AOA_SERIAL = "omarchy-galaxy-usb"
AOA_IDENTIFIER_STRINGS = (
    AOA_MANUFACTURER,
    AOA_MODEL,
    AOA_DESCRIPTION,
    AOA_VERSION,
    AOA_URI,
    AOA_SERIAL,
)

# The HELLO payload is intentionally non-secret.  USB permission and the
# manufacturer/model filter are the authentication boundary for AOA.
HELLO_PAYLOAD = (
    b'{"client":"omarchy","protocol":"GUSB/1",'
    b'"mode":"view-only","codec":"h264"}'
)


class FrameError(ValueError):
    """Raised when an untrusted GUSB frame is invalid."""


@dataclass(frozen=True)
class Frame:
    """One decoded GUSB frame."""

    type: int
    flags: int
    payload: bytes


def encode_frame(frame_type: int, payload: bytes = b"", flags: int = 0) -> bytes:
    """Encode a GUSB/1 frame after applying protocol size limits."""

    if frame_type not in FRAME_TYPES:
        raise FrameError(f"unsupported frame type: {frame_type}")
    if not 0 <= flags <= 0xFFFF:
        raise FrameError("frame flags must fit in an unsigned 16-bit value")
    if len(payload) > MAX_PAYLOAD:
        raise FrameError("frame payload exceeds 16000 bytes")
    return HEADER.pack(MAGIC, VERSION, frame_type, flags, len(payload)) + payload


class FrameParser:
    """Incrementally decode frames from arbitrary USB bulk chunks.

    The parser rejects a bad magic/version/type/length instead of attempting
    to resynchronise.  A malformed stream is closed by the caller so bytes
    from a stale or untrusted accessory cannot be mistaken for video.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes | bytearray | memoryview) -> list[Frame]:
        if data:
            self._buffer.extend(data)

        frames: list[Frame] = []
        while True:
            if len(self._buffer) < HEADER_SIZE:
                break

            magic, version, frame_type, flags, payload_len = HEADER.unpack_from(
                self._buffer
            )
            if magic != MAGIC:
                raise FrameError("invalid frame magic")
            if version != VERSION:
                raise FrameError(f"unsupported GUSB version: {version}")
            if frame_type not in FRAME_TYPES:
                raise FrameError(f"unsupported frame type: {frame_type}")
            if payload_len > MAX_PAYLOAD:
                raise FrameError("frame payload exceeds 16000 bytes")

            frame_size = HEADER_SIZE + payload_len
            if len(self._buffer) < frame_size:
                break

            payload = bytes(self._buffer[HEADER_SIZE:frame_size])
            del self._buffer[:frame_size]
            frames.append(Frame(frame_type, flags, payload))

        return frames
